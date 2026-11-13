"""SFTP user domain: model, validation rules and a local JSON catalog.

Validation is pure and IO-free so every backend (local catalog, DynamoDB,
AWS Transfer Family) enforces the same rules before applying a change.
"""

import base64
import json
import re
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

USERNAME_RE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{2,31}")
PATH_RE = re.compile(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*")
SECRET_REF_RE = re.compile(r"sftp/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*")
RESERVED_PREFIXES = {"_output"}


@dataclass
class SFTPUser:
    username: str
    ssh_public_key: str
    bucket: str
    prefix: str
    pgp_enabled: bool = False
    secret_ref: str = ""
    status: str = "local"


def valid_key(key: str) -> bool:
    """Accept only well-formed OpenSSH Ed25519 public keys."""
    try:
        kind, encoded, *_ = key.split()
        raw = base64.b64decode(encoded, validate=True)
        size = struct.unpack(">I", raw[:4])[0]
        if raw[4 : 4 + size].decode() != kind or kind != "ssh-ed25519":
            return False
        rest = raw[4 + size :]
        return len(rest) == 36 and struct.unpack(">I", rest[:4])[0] == 32
    except (ValueError, struct.error, UnicodeError):
        return False


def validate(user: SFTPUser, buckets: list[str]) -> None:
    if not USERNAME_RE.fullmatch(user.username):
        raise ValueError("Invalid username")
    if not valid_key(user.ssh_public_key):
        raise ValueError("Invalid SSH public key (Ed25519 required)")
    if user.bucket not in buckets:
        raise ValueError("Bucket not in allowlist")
    if not PATH_RE.fullmatch(user.prefix):
        raise ValueError("Invalid prefix")
    if user.prefix.split("/")[0] in RESERVED_PREFIXES:
        raise ValueError("Prefix is reserved")
    if user.secret_ref and not SECRET_REF_RE.fullmatch(user.secret_ref):
        raise ValueError("Secret reference outside the sftp/ namespace")
    if user.pgp_enabled and not user.secret_ref:
        raise ValueError("PGP requires a secret reference")


def overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def check_constraints(user: SFTPUser, users: dict[str, SFTPUser], edit: bool) -> None:
    """Uniqueness, edit target and non-overlapping prefixes within a bucket."""
    if user.username in users and not edit:
        raise ValueError("Username already exists")
    if edit and user.username not in users:
        raise ValueError("User does not exist")
    for other in users.values():
        if (
            other.username != user.username
            and other.bucket == user.bucket
            and overlaps(other.prefix, user.prefix)
        ):
            raise ValueError("Prefix overlaps with another user")


class Catalog:
    """Local JSON user catalog, written atomically with 0600 permissions."""

    def __init__(self, path, buckets: list[str]):
        self.path = Path(path)
        self.buckets = buckets
        self.users: dict[str, SFTPUser] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.users = {name: SFTPUser(**fields) for name, fields in data.items()}

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".new")
        temp.write_text(json.dumps({k: asdict(v) for k, v in self.users.items()}, indent=2))
        temp.chmod(0o600)
        temp.replace(self.path)

    def save(self, user: SFTPUser, edit: bool = False, apply=None) -> SFTPUser:
        validate(user, self.buckets)
        check_constraints(user, self.users, edit)
        user.status = "pending" if apply else "local"
        self.users[user.username] = user
        self.persist()
        if apply:
            try:
                apply(user)
                user.status = "applied"
            except Exception:
                user.status = "failed"
            self.persist()
        return user
