"""Operations behind the admin console.

`Console` turns one operator request into every change AWS needs: the catalog
record, the Transfer Family user and the source-IP allowlist. The catalog
records the user as pending first, then applied or failed, so the console can
always show whether a change actually reached AWS.
"""

from dataclasses import dataclass, field, replace

from sftp_platform.domain import (
    Mapping,
    SFTPUser,
    check_constraints,
    normalize_cidr,
    validate,
)
from sftp_platform.dynamo import Conflict, DynamoCatalog
from sftp_platform.sgpool import SecurityGroupPool
from sftp_platform.transfer import TransferUsers


@dataclass
class Capacity:
    rules_used: int
    rules_total: int
    groups: int
    max_groups: int


@dataclass
class Draft:
    """Raw form input, kept so a rejected form can be shown back unchanged."""

    username: str = ""
    ssh_public_keys: list[str] = field(default_factory=list)
    allowed_cidrs: list[str] = field(default_factory=list)
    pgp_secret_ref: str = ""
    mappings: list[dict] = field(default_factory=list)

    @classmethod
    def from_user(cls, user: SFTPUser) -> "Draft":
        return cls(
            username=user.username,
            ssh_public_keys=list(user.ssh_public_keys),
            allowed_cidrs=list(user.allowed_cidrs),
            pgp_secret_ref=user.pgp_secret_ref,
            mappings=[vars(m).copy() for m in user.mappings],
        )

    def to_user(self) -> SFTPUser:
        """Build a validated-shape user; raises ValueError naming the bad field."""
        cidrs = [normalize_cidr(c) for c in self.allowed_cidrs if c.strip()]
        return SFTPUser(
            username=self.username.strip(),
            ssh_public_keys=[k.strip() for k in self.ssh_public_keys if k.strip()],
            allowed_cidrs=sorted(set(cidrs), key=cidrs.index),
            pgp_secret_ref=self.pgp_secret_ref.strip(),
            mappings=[
                Mapping(
                    entry=m.get("entry", "").strip(),
                    bucket=m.get("bucket", "").strip(),
                    prefix=m.get("prefix", "").strip().strip("/"),
                    access=m.get("access", "read-write"),
                    pgp=(m.get("pgp_ref", "").strip() if m.get("pgp") == "custom" else m.get("pgp", "off")),
                )
                for m in self.mappings
                if any(str(v).strip() for k, v in m.items() if k in ("entry", "prefix"))
            ],
        )


class Console:
    def __init__(self, catalog: DynamoCatalog, transfer: TransferUsers, pool: SecurityGroupPool,
                 buckets: list[str]):
        self.catalog = catalog
        self.transfer = transfer
        self.pool = pool
        self.buckets = buckets

    # --- reads ---------------------------------------------------------------

    def users(self) -> list[SFTPUser]:
        self.catalog.refresh()
        return sorted(self.catalog.users.values(), key=lambda u: u.username)

    def user(self, username: str) -> SFTPUser | None:
        self.catalog.refresh()
        return self.catalog.users.get(username)

    def revision(self) -> int:
        return self.catalog.revision

    def capacity(self) -> Capacity:
        usage = self.pool.usage()
        return Capacity(
            rules_used=sum(usage.values()),
            rules_total=self.pool.rules_per_group * self.pool.max_groups,
            groups=len(usage),
            max_groups=self.pool.max_groups,
        )

    # --- writes --------------------------------------------------------------

    def check(self, user: SFTPUser, edit: bool) -> None:
        """Validate without writing: shape, uniqueness and isolation."""
        validate(user, self.buckets)
        self.catalog.refresh()
        check_constraints(user, self.catalog.users, edit)

    def save(self, user: SFTPUser, edit: bool, expected_revision: int | None) -> SFTPUser:
        return self.catalog.save(user, self._apply, edit=edit, expected_revision=expected_revision)

    def retry(self, username: str) -> SFTPUser:
        """Re-apply a failed user without changing its configuration."""
        user = self.user(username)
        if user is None:
            raise ValueError("User does not exist")
        if user.status != "failed":
            raise Conflict("Only a failed client can be retried")
        return self.catalog.save(replace(user, status="local"), self._apply, edit=True,
                                 expected_revision=self.catalog.revision)

    def delete(self, username: str, expected_revision: int | None) -> None:
        self.catalog.delete(username, self._remove, expected_revision=expected_revision)

    def _apply(self, user: SFTPUser) -> None:
        self.transfer.apply(user)
        self.pool.apply(user.username, user.allowed_cidrs)

    def _remove(self, username: str) -> None:
        # Revoke network access first, then the identity.
        self.pool.remove(username)
        self.transfer.delete(username)
