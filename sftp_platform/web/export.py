"""Export the client register as an Excel workbook.

Two sheets: one row per client, and one row per folder mapping. Values that a
spreadsheet could execute as a formula are written as plain text.
"""

from datetime import datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from sftp_platform.domain import SFTPUser


def _safe(value) -> object:
    # Neutralise CSV/formula injection: a leading =, +, - or @ becomes text.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _sheet(ws, header: list[str], rows: list[list]) -> None:
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([_safe(v) for v in row])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for index, column in enumerate(ws.columns, start=1):
        width = max(len(str(c.value or "")) for c in column)
        ws.column_dimensions[get_column_letter(index)].width = min(max(width + 2, 10), 60)


def workbook(users: list[SFTPUser], hostnames: list[str], labels: dict[str, str]) -> bytes:
    wb = Workbook()
    clients = wb.active
    clients.title = "Clients"
    _sheet(clients,
           ["Username", "Status", "Host", "Port", "Folders", "Source addresses", "SSH keys", "Default PGP key"],
           [[u.username, labels.get(u.status, u.status), ", ".join(hostnames), 22,
             ", ".join(m.entry for m in u.mappings), "\n".join(u.allowed_cidrs),
             len(u.ssh_public_keys), u.pgp_secret_ref or ""] for u in users])

    folders = wb.create_sheet("Folders")
    _sheet(folders,
           ["Username", "Client sees", "Bucket", "S3 prefix", "Access", "Decryption"],
           [[u.username, m.entry, m.bucket, m.prefix, "Read only" if m.access == "read" else "Read & write",
             _pgp(m.pgp, u.pgp_secret_ref)] for u in users for m in u.mappings])

    wb.properties.creator = "SFTP Console"
    wb.properties.created = datetime.now(timezone.utc).replace(tzinfo=None)
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def _pgp(value: str, default: str | None) -> str:
    if value == "off":
        return "Off"
    if value == "inherit":
        return f"Default key ({default})" if default else "Default key (not set)"
    return value
