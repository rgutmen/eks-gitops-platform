"""SFTP user domain: model, validation rules and a local JSON catalog.

A user sees one virtual SFTP tree built from mappings. Each mapping joins a
path the user sees (entry) to a real location in S3 (bucket + prefix), which
matches AWS Transfer Family LOGICAL home directories.

PGP decryption is resolved per mapping:
  - "off":      never decrypt files in this folder (default)
  - "inherit":  use the user's default key (pgp_secret_ref)
  - "sftp/...": use a folder-specific key from Secrets Manager

Validation is pure and IO-free so every backend (local catalog, DynamoDB,
AWS Transfer Family) enforces the same rules before applying a change.
"""

import base64
import json
import re
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path

USERNAME_RE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{2,31}")
PATH_RE = re.compile(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*")
ENTRY_RE = re.compile(r"/(?:[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*)?")
SECRET_REF_RE = re.compile(r"sftp/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*")
RESERVED_PREFIXES = {"_output"}
ACCESS_LEVELS = {"read", "read-write"}
PGP_OFF = "off"
PGP_INHERIT = "inherit"


@dataclass
class Mapping:
    entry: str
    bucket: str
    prefix: str
    access: str = "read-write"
    pgp: str = PGP_OFF


@dataclass
class SFTPUser:
    username: str
    ssh_public_key: str
    mappings: list[Mapping] = field(default_factory=list)
    pgp_secret_ref: str = ""
    status: str = "local"


def user_from_dict(data: dict) -> SFTPUser:
    fields = dict(data)
    fields["mappings"] = [Mapping(**m) for m in fields.get("mappings", [])]
    return SFTPUser(**fields)


def resolve_pgp(user: SFTPUser, mapping: Mapping) -> str | None:
    """Return the secret reference used to decrypt files in this mapping, or None."""
    if mapping.pgp == PGP_OFF:
        return None
    if mapping.pgp == PGP_INHERIT:
        return user.pgp_secret_ref or None
    return mapping.pgp


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


def overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _validate_mapping(user: SFTPUser, m: Mapping, buckets: list[str]) -> None:
    if not ENTRY_RE.fullmatch(m.entry):
        raise ValueError(f"Invalid entry path: {m.entry}")
    if m.bucket not in buckets:
        raise ValueError(f"Bucket not in allowlist: {m.bucket}")
    if not PATH_RE.fullmatch(m.prefix):
        raise ValueError(f"Invalid prefix: {m.prefix}")
    if m.prefix.split("/")[0] in RESERVED_PREFIXES:
        raise ValueError(f"Prefix is reserved: {m.prefix}")
    if m.access not in ACCESS_LEVELS:
        raise ValueError(f"Invalid access level: {m.access}")
    if m.pgp == PGP_INHERIT and not user.pgp_secret_ref:
        raise ValueError(f"Mapping {m.entry} inherits PGP but the user has no default key")
    if m.pgp not in (PGP_OFF, PGP_INHERIT) and not SECRET_REF_RE.fullmatch(m.pgp):
        raise ValueError(f"PGP secret reference outside the sftp/ namespace: {m.entry}")


def validate(user: SFTPUser, buckets: list[str]) -> None:
    if not USERNAME_RE.fullmatch(user.username):
        raise ValueError("Invalid username")
    if not valid_key(user.ssh_public_key):
        raise ValueError("Invalid SSH public key (Ed25519 required)")
    if user.pgp_secret_ref and not SECRET_REF_RE.fullmatch(user.pgp_secret_ref):
        raise ValueError("Default PGP secret reference outside the sftp/ namespace")
    if not user.mappings:
        raise ValueError("User needs at least one mapping")
    for m in user.mappings:
        _validate_mapping(user, m, buckets)
    for i, a in enumerate(user.mappings):
        for b in user.mappings[i + 1 :]:
            ea, eb = a.entry.strip("/"), b.entry.strip("/")
            if not ea or not eb or overlaps(ea, eb):
                raise ValueError(f"Entries overlap: {a.entry} and {b.entry}")
            if a.bucket == b.bucket and overlaps(a.prefix, b.prefix):
                raise ValueError(f"Mappings {a.entry} and {b.entry} point to overlapping S3 paths")


def check_constraints(user: SFTPUser, users: dict[str, SFTPUser], edit: bool) -> None:
    """Uniqueness, edit target and no S3 path shared with another user."""
    if user.username in users and not edit:
        raise ValueError("Username already exists")
    if edit and user.username not in users:
        raise ValueError("User does not exist")
    for other in users.values():
        if other.username == user.username:
            continue
        for mine in user.mappings:
            for theirs in other.mappings:
                if mine.bucket == theirs.bucket and overlaps(mine.prefix, theirs.prefix):
                    raise ValueError(f"Mapping {mine.entry} overlaps with another user")


class Catalog:
    """Local JSON user catalog, written atomically with 0600 permissions."""

    def __init__(self, path, buckets: list[str]):
        self.path = Path(path)
        self.buckets = buckets
        self.users: dict[str, SFTPUser] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.users = {name: user_from_dict(fields) for name, fields in data.items()}

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
