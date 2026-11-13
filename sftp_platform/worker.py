"""PGP decryption worker for files uploaded through AWS Transfer Family.

Scans each PGP-enabled user's S3 prefix, decrypts new .pgp/.gpg objects with
the user's private key from Secrets Manager and writes the plaintext under
_output/<username>/, outside any user's SFTP home. Re-runs are idempotent:
the output key is derived from the source key and ETag.
"""

import json
import subprocess
import tempfile
import time
from hashlib import sha256
from pathlib import Path

from sftp_platform.domain import SFTPUser, validate

MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 20
GPG_TIMEOUT = 20
DEADLINE_MARGIN = 50
OUTPUT_ROOT = "_output"
SECRET_NAMESPACE = "sftp/"


class S3Store:
    def __init__(self, client):
        self.client = client

    def pending(self, bucket: str, prefix: str):
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        for page in pages:
            yield from page.get("Contents", [])

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def read(self, bucket: str, key: str, maximum: int) -> bytes:
        response = self.client.get_object(Bucket=bucket, Key=key)
        with response["Body"] as body:
            data = body.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("size limit")
        return data

    def write(self, bucket: str, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=bucket, Key=key, Body=data, ServerSideEncryption="AES256", IfNoneMatch="*"
        )


class SecretStore:
    def __init__(self, client):
        self.client = client

    def get(self, reference: str) -> dict:
        if not reference.startswith(SECRET_NAMESPACE):
            raise ValueError("secret outside namespace")
        return json.loads(self.client.get_secret_value(SecretId=reference)["SecretString"])


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
            subprocess.run(base + ["--import"], input=material["private_key"].encode(), **quiet)
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


def output_key(user: SFTPUser, key: str, etag: str) -> str:
    identity = sha256((key + "\0" + etag).encode()).hexdigest()
    return f"{OUTPUT_ROOT}/{user.username}/{identity}.plain"


def _is_candidate(user: SFTPUser, key: str) -> bool:
    return key.startswith(user.prefix + "/") and key.endswith((".pgp", ".gpg"))


def _process(user: SFTPUser, item: dict, store: S3Store, secrets: SecretStore) -> str:
    destination = output_key(user, item["Key"], item.get("ETag", ""))
    if store.exists(user.bucket, destination):
        return "skipped"
    try:
        if item["Size"] > MAX_BYTES:
            raise ValueError("size limit")
        encrypted = store.read(user.bucket, item["Key"], MAX_BYTES)
        store.write(user.bucket, destination, decrypt(encrypted, secrets.get(user.secret_ref)))
        return "processed"
    except Exception:
        return "failed"


def scan(users, store, secrets, buckets, emit=print, deadline=None) -> dict:
    """Process pending files for applied, PGP-enabled users within time and count budgets."""
    counts = {"processed": 0, "failed": 0, "skipped": 0}
    remaining = MAX_FILES
    deadline = deadline or time.monotonic() + 180
    for user in users:
        validate(user, buckets)
        if not user.pgp_enabled or user.status != "applied":
            continue
        for item in store.pending(user.bucket, user.prefix + "/"):
            if remaining <= 0 or time.monotonic() > deadline - DEADLINE_MARGIN:
                emit(json.dumps(counts))
                return counts
            if not _is_candidate(user, item["Key"]):
                continue
            remaining -= 1
            event = _process(user, item, store, secrets)
            counts[event] += 1
            if event != "skipped":
                emit(json.dumps({"event": event, "user": user.username,
                                 "object_id": sha256(item["Key"].encode()).hexdigest()}))
    emit(json.dumps(counts))
    return counts
