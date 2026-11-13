import base64
import struct

import pytest

from sftp_platform.domain import Catalog, Mapping, SFTPUser, overlaps, valid_key, validate

BUCKETS = ["sftp-inbound"]
KEY = "ssh-ed25519 " + base64.b64encode(
    struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"x" * 32
).decode()


def make_user(username="alba", prefix="alba/inbox", pgp="off", **overrides):
    mapping = Mapping("/inbox", "sftp-inbound", prefix, pgp=pgp)
    return SFTPUser(username, KEY, [mapping], **overrides)


def test_valid_user_passes():
    validate(make_user(), BUCKETS)


@pytest.mark.parametrize("username", ["ab", "-alba", "al ba", "a" * 33, "alba!"])
def test_rejects_invalid_username(username):
    with pytest.raises(ValueError, match="username"):
        validate(make_user(username=username), BUCKETS)


@pytest.mark.parametrize("key", ["", "ssh-ed25519 bad", "ssh-rsa AAAA", KEY.replace("ssh-ed25519 ", "ssh-rsa ")])
def test_rejects_invalid_key(key):
    assert not valid_key(key)


def test_rejects_bucket_outside_allowlist():
    with pytest.raises(ValueError, match="allowlist"):
        validate(make_user(), ["other-bucket"])


@pytest.mark.parametrize("prefix", ["../escape", "/absolute", "a//b", "a/../b", "a/*", "a/"])
def test_rejects_unsafe_prefix(prefix):
    with pytest.raises(ValueError):
        validate(make_user(prefix=prefix), BUCKETS)


def test_pgp_requires_scoped_secret_ref():
    with pytest.raises(ValueError, match="default key"):
        validate(make_user(pgp="inherit"), BUCKETS)
    with pytest.raises(ValueError, match="namespace"):
        validate(make_user(pgp="other/key"), BUCKETS)
    validate(make_user(pgp="inherit", pgp_secret_ref="sftp/alba"), BUCKETS)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [("team/alba", "team", True), ("team/alba", "team/alba/x", True),
     ("team/alba", "team/alba", True), ("team/alba", "team/albert", False)],
)
def test_overlaps(a, b, expected):
    assert overlaps(a, b) is expected


def test_catalog_create_edit_duplicate_and_reload(tmp_path):
    catalog = Catalog(tmp_path / "users.json", BUCKETS)
    catalog.save(make_user())
    with pytest.raises(ValueError, match="exists"):
        catalog.save(make_user())
    catalog.save(make_user(prefix="alba/new"), edit=True)
    assert Catalog(catalog.path, BUCKETS).users["alba"].mappings[0].prefix == "alba/new"
    assert (catalog.path.stat().st_mode & 0o777) == 0o600


def test_catalog_rejects_overlapping_prefix(tmp_path):
    catalog = Catalog(tmp_path / "users.json", BUCKETS)
    catalog.save(make_user(prefix="team/alba"))
    with pytest.raises(ValueError, match="overlaps"):
        catalog.save(make_user("bruno", prefix="team/alba/child"))
    assert "bruno" not in catalog.users


def test_failed_apply_is_marked_without_leaking_details(tmp_path):
    catalog = Catalog(tmp_path / "users.json", BUCKETS)

    def fail(_user):
        raise RuntimeError("sensitive backend detail")

    catalog.save(make_user(), apply=fail)
    assert catalog.users["alba"].status == "failed"
    assert "sensitive" not in catalog.path.read_text()
