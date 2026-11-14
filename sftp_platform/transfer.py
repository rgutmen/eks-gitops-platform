"""Apply SFTP users to AWS Transfer Family.

Each user is a SERVICE_MANAGED Transfer user with a LOGICAL home directory:
every mapping becomes one HomeDirectoryMappings entry, so the client sees one
virtual tree. A per-user session policy limits S3 access to exactly the
mapped prefixes, and grants write only on read-write mappings.
"""

import json

from sftp_platform.domain import SFTPUser


def home_directory_mappings(user: SFTPUser) -> list[dict]:
    return [
        {"Entry": m.entry, "Target": f"/{m.bucket}/{m.prefix}"}
        for m in sorted(user.mappings, key=lambda m: m.entry)
    ]


def session_policy(user: SFTPUser) -> str:
    """Least-privilege session policy scoped to the user's mapped prefixes."""
    statements = []
    by_bucket: dict[str, list[str]] = {}
    for m in user.mappings:
        by_bucket.setdefault(m.bucket, []).append(m.prefix)
    for bucket, prefixes in sorted(by_bucket.items()):
        statements.append({
            "Sid": f"List{_sid(bucket)}",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": f"arn:aws:s3:::{bucket}",
            "Condition": {"StringLike": {"s3:prefix": [p for prefix in sorted(prefixes) for p in (prefix, f"{prefix}/*")]}},
        })
    read = sorted(f"arn:aws:s3:::{m.bucket}/{m.prefix}/*" for m in user.mappings)
    write = sorted(f"arn:aws:s3:::{m.bucket}/{m.prefix}/*" for m in user.mappings if m.access == "read-write")
    statements.append({
        "Sid": "ReadObjects",
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:GetObjectVersion"],
        "Resource": read,
    })
    if write:
        statements.append({
            "Sid": "WriteObjects",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"],
            "Resource": write,
        })
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


def _sid(bucket: str) -> str:
    return "".join(part.capitalize() for part in bucket.replace(".", "-").split("-"))


class TransferUsers:
    """Create or update Transfer users and reconcile their SSH keys."""

    def __init__(self, client, server_id: str, role_arn: str):
        self.client = client
        self.server_id = server_id
        self.role_arn = role_arn

    def apply(self, user: SFTPUser) -> None:
        args = {
            "ServerId": self.server_id,
            "UserName": user.username,
            "Role": self.role_arn,
            "HomeDirectoryType": "LOGICAL",
            "HomeDirectoryMappings": home_directory_mappings(user),
            "Policy": session_policy(user),
        }
        existing = self._describe(user.username)
        if existing is None:
            self.client.create_user(**args, SshPublicKeyBody=user.ssh_public_keys[0])
            existing = self._describe(user.username)
        else:
            self.client.update_user(**args)
        self._reconcile_keys(user, existing)

    def delete(self, username: str) -> None:
        if self._describe(username) is not None:
            self.client.delete_user(ServerId=self.server_id, UserName=username)

    def _describe(self, username: str) -> dict | None:
        try:
            return self.client.describe_user(ServerId=self.server_id, UserName=username)["User"]
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("ResourceNotFoundException", "UserNotFound"):
                return None
            raise

    def _reconcile_keys(self, user: SFTPUser, existing: dict) -> None:
        current = {_key_identity(k["SshPublicKeyBody"]): k["SshPublicKeyId"]
                   for k in existing.get("SshPublicKeys", [])}
        wanted = {_key_identity(k): k for k in user.ssh_public_keys}
        # Import new keys before removing old ones so rotation never locks the client out.
        for identity, body in wanted.items():
            if identity not in current:
                self.client.import_ssh_public_key(
                    ServerId=self.server_id, UserName=user.username, SshPublicKeyBody=body)
        for identity, key_id in current.items():
            if identity not in wanted:
                self.client.delete_ssh_public_key(
                    ServerId=self.server_id, UserName=user.username, SshPublicKeyId=key_id)


def _key_identity(body: str) -> str:
    """Key type and material, ignoring the trailing comment."""
    return " ".join(body.split()[:2])
