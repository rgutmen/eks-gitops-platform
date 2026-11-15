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
import ipaddress
import json
import re
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path

USERNAME_RE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{2,31}")
PATH_RE = re.compile(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*")
ENTRY_RE = re.compile(r"/(?:[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*)?")
SECRET_REF_RE = re.compile(r"sftp/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*")
ACCESS_LEVELS = {"read", "read-write"}
PGP_OFF = "off"
PGP_INHERIT = "inherit"
MAX_SSH_KEYS = 10
MAX_CIDRS = 50
MIN_CIDR_PREFIX = 16


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
    ssh_public_keys: list[str] = field(default_factory=list)
    mappings: list[Mapping] = field(default_factory=list)
    allowed_cidrs: list[str] = field(default_factory=list)
    pgp_secret_ref: str = ""
    status: str = "local"


def user_from_dict(data: dict) -> SFTPUser:
    fields = dict(data)
    # Records written before key rotation existed carry a single key.
    legacy_key = fields.pop("ssh_public_key", None)
    if legacy_key and not fields.get("ssh_public_keys"):
        fields["ssh_public_keys"] = [legacy_key]
    fields["mappings"] = [Mapping(**m) for m in fields.get("mappings", [])]
    return SFTPUser(**fields)


def normalize_cidr(value: str) -> str:
    """Return a canonical public IPv4 CIDR, or raise ValueError.

    A bare address becomes /32. Private, reserved and catch-all ranges are
    rejected because the allowlist must name real client egress addresses.
    """
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        raise ValueError(f"Not an IPv4 address or CIDR: {value}") from None
    if net.version != 4:
        raise ValueError(f"Only IPv4 is supported: {value}")
    if net.prefixlen < MIN_CIDR_PREFIX:
        raise ValueError(f"Range too wide (minimum /{MIN_CIDR_PREFIX}): {value}")
    if not net.is_global:
        raise ValueError(f"Not a public address range: {value}")
    return str(net)


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
    if m.access not in ACCESS_LEVELS:
        raise ValueError(f"Invalid access level: {m.access}")
    if m.pgp == PGP_INHERIT and not user.pgp_secret_ref:
        raise ValueError(f"Mapping {m.entry} inherits PGP but the user has no default key")
    if m.pgp not in (PGP_OFF, PGP_INHERIT) and not SECRET_REF_RE.fullmatch(m.pgp):
        raise ValueError(f"PGP secret reference outside the sftp/ namespace: {m.entry}")


def validate(user: SFTPUser, buckets: list[str]) -> None:
    if not USERNAME_RE.fullmatch(user.username):
        raise ValueError("Invalid username")
    if not user.ssh_public_keys:
        raise ValueError("User needs at least one SSH public key")
    if len(user.ssh_public_keys) > MAX_SSH_KEYS:
        raise ValueError(f"At most {MAX_SSH_KEYS} SSH public keys per user")
    for key in user.ssh_public_keys:
        if not valid_key(key):
            raise ValueError("Invalid SSH public key (Ed25519 required)")
    if len(set(user.ssh_public_keys)) != len(user.ssh_public_keys):
        raise ValueError("Duplicate SSH public key")
    if len(user.allowed_cidrs) > MAX_CIDRS:
        raise ValueError(f"At most {MAX_CIDRS} allowed source addresses per user")
    for cidr in user.allowed_cidrs:
        if normalize_cidr(cidr) != cidr:
            raise ValueError(f"Source address must be in canonical CIDR form: {cidr}")
    if len(set(user.allowed_cidrs)) != len(user.allowed_cidrs):
        raise ValueError("Duplicate source address")
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


def shared_addresses(user: SFTPUser, users: dict[str, SFTPUser]) -> dict[str, list[str]]:
    """Addresses of `user` that other clients are also allowed from.

    Sharing is permitted (several clients behind one NAT is common), but each
    of those clients can then reach port 22 from that address, so the
    operator must acknowledge it.
    """
    shared: dict[str, list[str]] = {}
    for cidr in user.allowed_cidrs:
        mine = ipaddress.ip_network(cidr)
        owners = sorted(o.username for o in users.values() if o.username != user.username
                        and any(mine.overlaps(ipaddress.ip_network(c)) for c in o.allowed_cidrs))
        if owners:
            shared[cidr] = owners
    return shared


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
                    raise ValueError(
                        f"{mine.entry} would share s3://{mine.bucket}/{mine.prefix}/ with client "
                        f"{other.username} ({theirs.entry} -> {theirs.prefix}/). Choose a prefix outside it.")


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
