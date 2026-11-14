"""Operator authentication, sessions and CSRF protection.

Operators are defined by the OPERATORS setting: comma-separated
`name:scrypt$<n>$<r>$<p>$<salt-b64>$<hash-b64>` entries, produced with
`python -m sftp_platform.web.auth hash`. Passwords never live in the image or
in Git; in the cluster they come from a Kubernetes Secret.

Sessions are signed (itsdangerous), expire after SESSION_MAX_AGE seconds and
carry a per-session CSRF token that every state-changing request must echo.
"""

import base64
import getpass
import hashlib
import hmac
import secrets
import sys
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SESSION_COOKIE = "sftp_console_session"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                                p=int(p), maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def parse_operators(value: str) -> dict[str, str]:
    operators = {}
    for entry in filter(None, (e.strip() for e in value.split(","))):
        name, _, encoded = entry.partition(":")
        if not name or not encoded.startswith("scrypt$"):
            raise ValueError("OPERATORS entries must be name:scrypt$... (see auth.py)")
        operators[name] = encoded
    return operators


# A fixed hash compared against when the operator is unknown, so a wrong
# username costs the same time as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class Authenticator:
    def __init__(self, operators: dict[str, str], secret_key: str, max_age: int = 8 * 3600):
        if len(secret_key) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        self.operators = operators
        self.max_age = max_age
        self.serializer = URLSafeTimedSerializer(secret_key, salt="sftp-console-session")

    def login(self, name: str, password: str) -> str | None:
        """Return a signed session token, or None for bad credentials."""
        encoded = self.operators.get(name, _DUMMY_HASH)
        if not verify_password(password, encoded) or name not in self.operators:
            return None
        return self.serializer.dumps({"op": name, "csrf": secrets.token_urlsafe(32), "iat": int(time.time())})

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        try:
            data = self.serializer.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return None
        return data if data.get("op") in self.operators else None


def _main() -> None:
    if sys.argv[1:] != ["hash"]:
        sys.exit("usage: python -m sftp_platform.web.auth hash")
    password = getpass.getpass("Operator password: ")
    if len(password) < 12:
        sys.exit("Use at least 12 characters.")
    if password != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match.")
    print(hash_password(password))


if __name__ == "__main__":
    _main()
