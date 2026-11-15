"""PGP decryption worker for files uploaded through AWS Transfer Family.

Event flow: S3 Object Created -> EventBridge rule -> SQS -> worker pods (KEDA).
Each event names one object. The worker finds which user mapping owns it,
resolves the PGP key for that mapping and writes the plaintext to a separate
decrypted bucket under <username>/. Plaintext never shares a bucket with
customer uploads, so it is never archived and never visible over SFTP.

Error contract with SQS:
  - Objects that can never be processed (no owner, PGP off, not .pgp/.gpg,
    user not applied) are ignored and the message is acknowledged.
  - Every other failure raises, so SQS retries the message and finally moves
    it to the dead-letter queue.

Re-delivery is safe: the plaintext keeps the uploaded name (minus .pgp/.gpg)
and records the source ETag, so the same upload is skipped and a re-upload
under the same name replaces it.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path

from sftp_platform.domain import Mapping, SFTPUser, resolve_pgp

MAX_BYTES = 10 * 1024 * 1024
GPG_TIMEOUT = 20
SECRET_NAMESPACE = "sftp/"
PGP_SUFFIXES = (".pgp", ".gpg")


class S3Store:
    def __init__(self, client):
        self.client = client

    def source_etag(self, bucket: str, key: str) -> str | None:
        """ETag of the upload a plaintext object was decrypted from, or None if absent."""
        try:
            head = self.client.head_object(Bucket=bucket, Key=key)
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise
        return head.get("Metadata", {}).get("source-etag", "")

    def read(self, bucket: str, key: str, maximum: int) -> bytes:
        response = self.client.get_object(Bucket=bucket, Key=key)
        with response["Body"] as body:
            data = body.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("size limit")
        return data

    def write(self, bucket: str, key: str, data: bytes, source_etag: str) -> None:
        # A re-upload with the same name replaces the previous plaintext.
        self.client.put_object(
            Bucket=bucket, Key=key, Body=data, ServerSideEncryption="AES256",
            Metadata={"source-etag": source_etag.strip('"')},
        )


class SecretStore:
    def __init__(self, client):
        self.client = client

    def get(self, reference: str) -> dict:
        if not reference.startswith(SECRET_NAMESPACE):
            raise ValueError("secret outside namespace")
        return json.loads(self.client.get_secret_value(SecretId=reference)["SecretString"])


ARMOR_RE = re.compile(r"(-----BEGIN PGP PRIVATE KEY BLOCK-----)(.*?)(-----END PGP PRIVATE KEY BLOCK-----)", re.S)


def normalize_armor(key: str) -> str:
    """Rebuild an ASCII-armored key whose line breaks were lost.

    Pasting a key into a single-line field (the Secrets Manager console, a
    JSON value) often turns newlines into spaces or literal "\\n". The base64
    body and its "=" checksum survive, so the original layout can be restored.
    A key that already has line breaks is returned unchanged.
    """
    key = key.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")
    match = ARMOR_RE.search(key)
    if match is None or "\n" in match.group(2).strip():
        return key
    tokens = match.group(2).split()
    headers = [t for t in tokens if ":" in t]
    if headers:  # "Version: x" style headers cannot be rebuilt reliably
        return key
    body = [t for t in tokens if not t.startswith("=")]
    checksum = [t for t in tokens if t.startswith("=")]
    return "\n".join([match.group(1), "", *body, *checksum, match.group(3), ""])


def decrypt(ciphertext: bytes, material: dict) -> bytes:
    """Decrypt with a throwaway GnuPG home so no key material outlives the call."""
    if len(ciphertext) > MAX_BYTES:
        raise ValueError("size limit")
    with tempfile.TemporaryDirectory(prefix="g") as folder:
        home = Path(folder)
        home.chmod(0o700)
        source, output = home / "input.pgp", home / "output"
        source.write_bytes(ciphertext)
        base = ["gpg", "--homedir", folder, "--batch", "--yes", "--no-tty"]
        quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=GPG_TIMEOUT)
        try:
            subprocess.run(base + ["--import"], input=normalize_armor(material["private_key"]).encode(), **quiet)
            subprocess.run(
                base + ["--pinentry-mode", "loopback", "--passphrase-fd", "0",
                        "--max-output", str(MAX_BYTES), "--output", str(output),
                        "--decrypt", str(source)],
                input=(material["passphrase"] + "\n").encode(),
                **quiet,
            )
            if output.stat().st_size > MAX_BYTES:
                raise ValueError("size limit")
            return output.read_bytes()
        finally:
            subprocess.run(["gpgconf", "--homedir", folder, "--kill", "gpg-agent"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)


def output_key(user: SFTPUser, mapping: Mapping, key: str) -> str:
    """Plaintext path as the client sees it, without the .pgp/.gpg suffix.

    alba uploading /inbox/2026/invoice.pdf.pgp -> alba/inbox/2026/invoice.pdf
    """
    relative = key[len(mapping.prefix) + 1:]
    for suffix in PGP_SUFFIXES:
        if relative.lower().endswith(suffix):
            relative = relative[: -len(suffix)]
            break
    folder = mapping.entry.strip("/")
    return "/".join(p for p in (user.username, folder, relative) if p)


def find_owner(users, bucket: str, key: str) -> tuple[SFTPUser, Mapping] | None:
    """Return the user and mapping whose S3 location contains this object."""
    for user in users:
        for mapping in user.mappings:
            if mapping.bucket == bucket and key.startswith(mapping.prefix + "/"):
                return user, mapping
    return None


def process_object(bucket: str, key: str, etag: str, size: int, users, store, secrets,
                   output_bucket: str) -> str:
    """Decrypt one uploaded object. Returns 'processed', 'skipped' or 'ignored'; raises on failure."""
    if not key.lower().endswith(PGP_SUFFIXES):
        return "ignored"
    owner = find_owner(users, bucket, key)
    if owner is None:
        return "ignored"
    user, mapping = owner
    secret_ref = resolve_pgp(user, mapping)
    if user.status != "applied" or secret_ref is None:
        return "ignored"
    destination = output_key(user, mapping, key)
    if store.source_etag(output_bucket, destination) == etag.strip('"'):
        return "skipped"
    if size > MAX_BYTES:
        raise ValueError("size limit")
    encrypted = store.read(bucket, key, MAX_BYTES)
    store.write(output_bucket, destination, decrypt(encrypted, secrets.get(secret_ref)), etag)
    return "processed"
