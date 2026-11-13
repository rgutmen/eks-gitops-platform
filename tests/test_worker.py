import json
import subprocess
import tempfile

import boto3
import pytest
from moto import mock_aws

from sftp_platform.worker import MAX_BYTES, S3Store, SecretStore, decrypt, output_key, scan
from tests.test_domain import make_user

BUCKET = "sftp-inbound"
SECRET_REF = "sftp/alba/pgp"
PLAINTEXT = b"invoice 2026-001\n"


@pytest.fixture(scope="module")
def material():
    """Generate a throwaway GPG key pair and one encrypted message."""
    with tempfile.TemporaryDirectory(prefix="g") as home:
        base = ["gpg", "--homedir", home, "--batch", "--yes", "--pinentry-mode", "loopback"]

        def run(args, data):
            return subprocess.run(base + args, input=data, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=30, check=True).stdout

        try:
            run(["--passphrase-fd", "0", "--quick-generate-key",
                 "alba <alba@example.invalid>", "rsa2048", "encr", "0"], b"test-pass\n")
            private = run(["--passphrase-fd", "0", "--armor", "--export-secret-keys"], b"test-pass\n")
            encrypted = run(["--trust-model", "always", "--recipient", "alba@example.invalid",
                             "--encrypt"], PLAINTEXT)
        finally:
            subprocess.run(["gpgconf", "--homedir", home, "--kill", "gpg-agent"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return {"private_key": private.decode(), "passphrase": "test-pass"}, encrypted


@pytest.fixture
def aws(monkeypatch):
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        yield s3, boto3.client("secretsmanager")


def applied_user(**overrides):
    fields = dict(pgp_enabled=True, secret_ref=SECRET_REF, status="applied")
    return make_user(**(fields | overrides))


def test_decrypt_with_real_gpg(material):
    secret, encrypted = material
    assert decrypt(encrypted, secret) == PLAINTEXT
    with pytest.raises(subprocess.CalledProcessError):
        decrypt(encrypted, dict(secret, passphrase="wrong"))


def test_scan_decrypts_isolates_and_is_idempotent(aws, material):
    s3, sm = aws
    secret, encrypted = material
    sm.create_secret(Name=SECRET_REF, SecretString=json.dumps(secret))
    s3.put_object(Bucket=BUCKET, Key="alba/inbox/valid.pgp", Body=encrypted)
    s3.put_object(Bucket=BUCKET, Key="alba/inbox/corrupt.pgp", Body=b"not pgp")
    s3.put_object(Bucket=BUCKET, Key="alba/inbox/notes.txt", Body=b"ignored")
    s3.put_object(Bucket=BUCKET, Key="bruno/inbox/valid.pgp", Body=encrypted)
    store, secrets, logs = S3Store(s3), SecretStore(sm), []
    users = [applied_user(), make_user("bruno", "bruno/inbox", status="applied")]

    assert scan(users, store, secrets, [BUCKET], logs.append) == {"processed": 1, "failed": 1, "skipped": 0}
    outputs = s3.list_objects_v2(Bucket=BUCKET, Prefix="_output/")["Contents"]
    assert [o["Key"].split("/")[1] for o in outputs] == ["alba"]
    assert s3.get_object(Bucket=BUCKET, Key=outputs[0]["Key"])["Body"].read() == PLAINTEXT

    assert scan(users, store, secrets, [BUCKET], logs.append) == {"processed": 0, "failed": 1, "skipped": 1}
    assert "test-pass" not in "".join(logs)


def test_skips_users_not_applied_or_without_pgp(aws):
    s3, sm = aws
    s3.put_object(Bucket=BUCKET, Key="alba/inbox/x.pgp", Body=b"x")
    store, secrets = S3Store(s3), SecretStore(sm)
    for user in [applied_user(status="pending"), applied_user(pgp_enabled=False, secret_ref="")]:
        assert scan([user], store, secrets, [BUCKET], lambda _: None)["failed"] == 0


def test_oversized_object_fails_without_fetching_secret(aws):
    s3, _ = aws

    class Secrets:
        requests = []

        def get(self, ref):
            self.requests.append(ref)

    class LargeListing(S3Store):
        def pending(self, bucket, prefix):
            yield {"Key": prefix + "large.pgp", "Size": MAX_BYTES + 1, "ETag": "1"}

    secrets = Secrets()
    assert scan([applied_user()], LargeListing(s3), secrets, [BUCKET], lambda _: None)["failed"] == 1
    assert secrets.requests == []


def test_secret_store_rejects_foreign_namespace(aws):
    with pytest.raises(ValueError):
        SecretStore(aws[1]).get("other/key")


def test_output_key_changes_with_etag():
    user = applied_user()
    assert output_key(user, "alba/inbox/a.pgp", "1") != output_key(user, "alba/inbox/a.pgp", "2")
    assert output_key(user, "alba/inbox/a.pgp", "1").startswith("_output/alba/")
