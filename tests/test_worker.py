import json
import subprocess
import tempfile

import boto3
import pytest
from moto import mock_aws

from sftp_platform.worker import MAX_BYTES, S3Store, SecretStore, decrypt, output_key, process_object
from tests.test_domain import make_user

BUCKET = "sftp-inbound"
OUTPUT = "sftp-decrypted"
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
        s3.create_bucket(Bucket=OUTPUT, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        yield s3, boto3.client("secretsmanager")


def applied_user(username="alba", prefix="alba/inbox", pgp="inherit", **overrides):
    fields = dict(pgp_secret_ref=SECRET_REF, status="applied") | overrides
    return make_user(username, prefix, pgp=pgp, **fields)


def put(s3, key, body):
    s3.put_object(Bucket=BUCKET, Key=key, Body=body)
    head = s3.head_object(Bucket=BUCKET, Key=key)
    return BUCKET, key, head["ETag"], head["ContentLength"]


def test_decrypt_with_real_gpg(material):
    secret, encrypted = material
    assert decrypt(encrypted, secret) == PLAINTEXT
    with pytest.raises(subprocess.CalledProcessError):
        decrypt(encrypted, dict(secret, passphrase="wrong"))


def test_process_decrypts_isolates_and_is_idempotent(aws, material):
    s3, sm = aws
    secret, encrypted = material
    sm.create_secret(Name=SECRET_REF, SecretString=json.dumps(secret))
    users = [applied_user(), applied_user("bruno", "bruno/inbox", pgp="off")]
    store, secrets = S3Store(s3), SecretStore(sm)

    valid = put(s3, "alba/inbox/valid.pgp", encrypted)
    assert process_object(*valid, users, store, secrets, OUTPUT) == "processed"
    outputs = s3.list_objects_v2(Bucket=OUTPUT)["Contents"]
    assert [o["Key"].split("/")[0] for o in outputs] == ["alba"]
    assert s3.get_object(Bucket=OUTPUT, Key=outputs[0]["Key"])["Body"].read() == PLAINTEXT
    assert process_object(*valid, users, store, secrets, OUTPUT) == "skipped"

    assert process_object(*put(s3, "bruno/inbox/valid.pgp", encrypted), users, store, secrets, OUTPUT) == "ignored"
    assert process_object(*put(s3, "alba/inbox/notes.txt", b"x"), users, store, secrets, OUTPUT) == "ignored"
    assert process_object(*put(s3, "nobody/x.pgp", encrypted), users, store, secrets, OUTPUT) == "ignored"
    with pytest.raises(subprocess.CalledProcessError):
        process_object(*put(s3, "alba/inbox/corrupt.pgp", b"not pgp"), users, store, secrets, OUTPUT)


def test_ignores_users_not_applied(aws):
    s3, sm = aws
    obj = put(s3, "alba/inbox/x.pgp", b"x")
    assert process_object(*obj, [applied_user(status="pending")], S3Store(s3), SecretStore(sm), OUTPUT) == "ignored"


def test_oversized_object_raises_without_fetching_secret(aws):
    s3, _ = aws

    class Secrets:
        requests = []

        def get(self, ref):
            self.requests.append(ref)

    secrets = Secrets()
    with pytest.raises(ValueError, match="size"):
        process_object(BUCKET, "alba/inbox/large.pgp", "1", MAX_BYTES + 1, [applied_user()], S3Store(s3), secrets, OUTPUT)
    assert secrets.requests == []


def test_secret_store_rejects_foreign_namespace(aws):
    with pytest.raises(ValueError):
        SecretStore(aws[1]).get("other/key")


def test_output_key_changes_with_etag():
    user = applied_user()
    assert output_key(user, "alba/inbox/a.pgp", "1") != output_key(user, "alba/inbox/a.pgp", "2")
    assert output_key(user, "alba/inbox/a.pgp", "1").startswith("alba/")
