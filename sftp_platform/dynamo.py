"""DynamoDB-backed SFTP user catalog.

The whole catalog lives in a single item guarded by a global revision number.
Every write is a conditional put on that revision, so concurrent operators can
never overwrite each other and prefix isolation is checked against a
consistent snapshot of all users.

Only secret references are stored (user default and per-mapping). The PGP private key and passphrase
live in AWS Secrets Manager and are never written to this table.

A user in "pending" state blocks further changes until the Transfer Family
mutation finishes or an operator recovers it manually. There is no automatic
lock expiry, so a stalled process can never resume after losing its lock.
"""

import json
from dataclasses import asdict, replace

from sftp_platform.domain import SFTPUser, check_constraints, user_from_dict, validate

CATALOG_KEY = "catalog"
MAX_DOCUMENT_BYTES = 300 * 1024


class Conflict(ValueError):
    pass


def load_catalog(table) -> tuple[dict[str, SFTPUser], int, bool]:
    item = table.get_item(Key={"pk": CATALOG_KEY}, ConsistentRead=True).get("Item")
    if item is None:
        return {}, 0, False
    users = {name: user_from_dict(fields) for name, fields in json.loads(item["document"]).items()}
    return users, int(item["revision"]), True


class DynamoCatalog:
    def __init__(self, table, buckets: list[str]):
        self.table = table
        self.buckets = buckets
        self.refresh()

    def refresh(self) -> None:
        self.users, self.revision, self.exists = load_catalog(self.table)
        for user in self.users.values():
            validate(user, self.buckets)

    def _write(self, users: dict[str, SFTPUser]) -> None:
        document = json.dumps({k: asdict(v) for k, v in users.items()}, ensure_ascii=True)
        if len(document.encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("Catalog exceeds the 300 KiB document limit")
        args = {"Item": {"pk": CATALOG_KEY, "revision": self.revision + 1, "document": document}}
        if self.exists:
            args |= {
                "ConditionExpression": "#revision = :expected",
                "ExpressionAttributeNames": {"#revision": "revision"},
                "ExpressionAttributeValues": {":expected": self.revision},
            }
        else:
            args["ConditionExpression"] = "attribute_not_exists(pk)"
        try:
            self.table.put_item(**args)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise Conflict("Catalog revision changed, reload and retry") from None
            raise
        self.users = users
        self.revision += 1
        self.exists = True

    def check(self, user: SFTPUser, edit: bool) -> None:
        validate(user, self.buckets)
        if any(u.status == "pending" for u in self.users.values()):
            raise Conflict("Catalog has a pending operation or needs manual recovery")
        check_constraints(user, self.users, edit)

    def save(self, user: SFTPUser, apply, edit: bool = False, expected_revision: int | None = None) -> SFTPUser:
        """Record the user as pending, apply it to Transfer Family, then record the outcome."""
        self.refresh()
        if expected_revision is not None and expected_revision != self.revision:
            raise Conflict("Catalog revision changed, reload and retry")
        self.check(user, edit)
        pending = replace(user, status="pending")
        self._write(self.users | {user.username: pending})
        try:
            apply(pending)
            status = "applied"
        except Exception:
            status = "failed"
        final = replace(pending, status=status)
        self._write(self.users | {user.username: final})
        return final
