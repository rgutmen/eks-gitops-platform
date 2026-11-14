"""SFTP admin console (FastAPI, server-rendered with HTMX).

Configuration (environment variables):
  AWS_REGION            AWS region
  CATALOG_TABLE         DynamoDB catalog table
  ALLOWED_BUCKETS       comma-separated buckets that mappings may target
  TRANSFER_SERVER_ID    Transfer Family server
  TRANSFER_USER_ROLE    IAM role assigned to every SFTP user
  TRANSFER_VPC_ID       VPC of the SFTP endpoint
  TRANSFER_ENDPOINT_ID  VPC endpoint of the server (security groups attach here)
  SFTP_HOSTNAMES        comma-separated public IPs or hostnames shown to operators
  OPERATORS             name:scrypt$... entries (see auth.py)
  SESSION_SECRET        at least 32 random characters
  COOKIE_SECURE         "true" when served over HTTPS (default false for port-forward)
  SG_RULES_PER_GROUP    inbound rules per security group (default 60)
  SG_MAX_GROUPS         security groups per endpoint (default 5)
"""

import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from sftp_platform.domain import ACCESS_LEVELS, PGP_INHERIT, PGP_OFF, SFTPUser
from sftp_platform.dynamo import Conflict
from sftp_platform.sgpool import PoolExhausted
from sftp_platform.web.auth import SESSION_COOKIE, Authenticator
from sftp_platform.web.console import Console, Draft

HERE = Path(__file__).parent
MAPPING_FIELD = re.compile(r"^mappings-(\d+)-(entry|bucket|prefix|access|pgp|pgp_ref)$")
STATUS_LABEL = {"applied": "Cleared", "pending": "Pending", "failed": "Held", "local": "Draft"}


@dataclass
class Settings:
    hostnames: list[str]
    cookie_secure: bool


def create_app(console: Console, auth: Authenticator, settings: Settings) -> FastAPI:
    # Every state-changing route except /login depends on this. A dependency (not
    # middleware) reads the form, so the route can still read the same body.
    async def csrf_protect(request: Request) -> None:
        form = await request.form()
        token = form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(str(token), request.state.session["csrf"]):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    protected = APIRouter(dependencies=[Depends(csrf_protect)])
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(status_label=STATUS_LABEL, access_levels=sorted(ACCESS_LEVELS),
                                 pgp_off=PGP_OFF, pgp_inherit=PGP_INHERIT)

    def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
        session = request.state.session
        context.update(operator=session["op"] if session else None,
                       csrf=session["csrf"] if session else "",
                       hostnames=settings.hostnames, buckets=console.buckets)
        return templates.TemplateResponse(request, template, context, status_code=status_code)

    def redirect(url: str) -> Response:
        return RedirectResponse(url, status_code=303)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        request.state.session = auth.session(request.cookies.get(SESSION_COOKIE))
        public = request.url.path in ("/login", "/healthz") or request.url.path.startswith("/static/")
        if not public and request.state.session is None:
            if request.headers.get("HX-Request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return redirect("/login")
        response = await call_next(request)
        response.headers.update({
            "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                        "script-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                                        "base-uri 'none'"),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "Cache-Control": response.headers.get("Cache-Control", "no-store"),
        })
        return response

    # --- auth ----------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    @app.get("/login")
    def login_page(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    def login(request: Request, name: str = Form(""), password: str = Form("")):
        token = auth.login(name.strip(), password)
        if token is None:
            return render(request, "login.html", status_code=401, error="Unknown operator or wrong password.",
                          name=name)
        response = redirect("/")
        response.set_cookie(SESSION_COOKIE, token, max_age=auth.max_age, httponly=True,
                            secure=settings.cookie_secure, samesite="strict")
        return response

    @protected.post("/logout")
    def logout():
        response = redirect("/login")
        response.delete_cookie(SESSION_COOKIE)
        return response

    # --- clients -------------------------------------------------------------

    @app.get("/")
    def clients(request: Request, q: str = ""):
        users = console.users()
        query = q.strip().lower()
        shown = [u for u in users if not query or query in u.username.lower()
                 or any(query in m.prefix.lower() or query in m.entry.lower() for m in u.mappings)
                 or any(query in c for c in u.allowed_cidrs)]
        template = "_register.html" if request.headers.get("HX-Request") else "clients.html"
        return render(request, template, users=shown, total=len(users), q=q,
                      capacity=console.capacity(), counts=_counts(users))

    @app.get("/clients/new")
    def new_client(request: Request):
        draft = Draft(mappings=[{"entry": "/inbox", "bucket": console.buckets[0] if console.buckets else "",
                                 "prefix": "", "access": "read-write", "pgp": PGP_OFF}])
        return render(request, "edit.html", draft=draft, edit=False, revision=console.revision())

    @app.get("/clients/{username}")
    def client(request: Request, username: str):
        user = console.user(username)
        if user is None:
            return render(request, "missing.html", status_code=404, username=username)
        return render(request, "client.html", user=user, revision=console.revision())

    @app.get("/clients/{username}/edit")
    def edit_client(request: Request, username: str):
        user = console.user(username)
        if user is None:
            return render(request, "missing.html", status_code=404, username=username)
        return render(request, "edit.html", draft=Draft.from_user(user), edit=True,
                      revision=console.revision())

    @protected.post("/clients")
    async def create_client(request: Request):
        return await _save(request, edit=False)

    @protected.post("/clients/{username}")
    async def update_client(request: Request, username: str):
        return await _save(request, edit=True, username=username)

    @protected.post("/clients/{username}/retry")
    def retry_client(request: Request, username: str):
        try:
            console.retry(username)
        except (ValueError, Conflict) as exc:
            user = console.user(username)
            return render(request, "client.html", status_code=409, user=user, revision=console.revision(),
                          error=str(exc))
        return redirect(f"/clients/{username}")

    @protected.post("/clients/{username}/delete")
    async def delete_client(request: Request, username: str):
        form = await request.form()
        if form.get("confirm", "") != username:
            user = console.user(username)
            return render(request, "client.html", status_code=400, user=user, revision=console.revision(),
                          error="Type the client name to confirm removal.")
        try:
            console.delete(username, _int(form.get("revision")))
        except (ValueError, Conflict) as exc:
            user = console.user(username)
            return render(request, "client.html", status_code=409, user=user, revision=console.revision(),
                          error=str(exc))
        except Exception:
            user = console.user(username)
            return render(request, "client.html", status_code=502, user=user, revision=console.revision(),
                          error="AWS rejected the removal. The client is marked held; retry once AWS recovers.")
        return redirect("/")

    # --- HTMX fragments ------------------------------------------------------

    @protected.post("/fragments/mapping")
    async def mapping_row(request: Request):
        form = await request.form()
        index = _int(form.get("index")) or 0
        return render(request, "_mapping.html", i=index,
                      m={"entry": "", "bucket": console.buckets[0] if console.buckets else "", "prefix": "",
                         "access": "read-write", "pgp": PGP_OFF})

    @protected.post("/fragments/check")
    async def check(request: Request):
        form = await request.form()
        edit = form.get("edit") == "1"
        try:
            user = _draft(form).to_user()
            console.check(user, edit=edit)
        except ValueError as exc:
            return render(request, "_check.html", ok=False, message=str(exc))
        return render(request, "_check.html", ok=True, message=_summary(user))

    async def _save(request: Request, edit: bool, username: str | None = None) -> Response:
        form = await request.form()
        draft = _draft(form)
        if edit:
            draft.username = username
        try:
            user = draft.to_user()
            console.check(user, edit=edit)
            saved = console.save(user, edit=edit, expected_revision=_int(form.get("revision")))
        except Conflict as exc:
            return render(request, "edit.html", status_code=409, draft=draft, edit=edit,
                          revision=console.revision(), error=f"{exc}. Your changes are below; save again.")
        except PoolExhausted as exc:
            return render(request, "edit.html", status_code=409, draft=draft, edit=edit,
                          revision=console.revision(), error=str(exc))
        except ValueError as exc:
            return render(request, "edit.html", status_code=422, draft=draft, edit=edit,
                          revision=console.revision(), error=str(exc))
        return redirect(f"/clients/{saved.username}?saved=1")

    app.include_router(protected)
    return app


def _draft(form: FormData) -> Draft:
    rows: dict[int, dict] = {}
    for key, value in form.multi_items():
        match = MAPPING_FIELD.match(key)
        if match:
            rows.setdefault(int(match.group(1)), {})[match.group(2)] = str(value)
    return Draft(
        username=str(form.get("username", "")),
        ssh_public_keys=_lines(form.get("ssh_public_keys", "")),
        allowed_cidrs=[c for c in re.split(r"[\s,]+", str(form.get("allowed_cidrs", ""))) if c],
        pgp_secret_ref=str(form.get("pgp_secret_ref", "")),
        mappings=[rows[i] for i in sorted(rows)],
    )


def _lines(value) -> list[str]:
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _int(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _counts(users: list[SFTPUser]) -> dict[str, int]:
    counts = {"applied": 0, "pending": 0, "failed": 0}
    for u in users:
        counts[u.status] = counts.get(u.status, 0) + 1
    return counts


def _summary(user: SFTPUser) -> str:
    folders = len(user.mappings)
    decrypting = sum(1 for m in user.mappings if m.pgp != PGP_OFF)
    return (f"{folders} folder{'s' * (folders != 1)}, {len(user.ssh_public_keys)} key"
            f"{'s' * (len(user.ssh_public_keys) != 1)}, {len(user.allowed_cidrs)} source address"
            f"{'es' * (len(user.allowed_cidrs) != 1)}, {decrypting} decrypting. No overlap with other clients.")


def app_from_env() -> FastAPI:
    import boto3

    from sftp_platform.dynamo import DynamoCatalog
    from sftp_platform.sgpool import SecurityGroupPool
    from sftp_platform.transfer import TransferUsers
    from sftp_platform.web.auth import parse_operators

    env = os.environ
    session = boto3.Session(region_name=env["AWS_REGION"])
    buckets = [b for b in env["ALLOWED_BUCKETS"].split(",") if b]
    console = Console(
        catalog=DynamoCatalog(session.resource("dynamodb").Table(env["CATALOG_TABLE"]), buckets),
        transfer=TransferUsers(session.client("transfer"), env["TRANSFER_SERVER_ID"], env["TRANSFER_USER_ROLE"]),
        pool=SecurityGroupPool(session.client("ec2"), env["TRANSFER_VPC_ID"], env["TRANSFER_ENDPOINT_ID"],
                               name=env.get("NAME_PREFIX", "sftp-platform"),
                               rules_per_group=int(env.get("SG_RULES_PER_GROUP", "60")),
                               max_groups=int(env.get("SG_MAX_GROUPS", "5"))),
        buckets=buckets,
    )
    auth = Authenticator(parse_operators(env["OPERATORS"]), env["SESSION_SECRET"],
                         max_age=int(env.get("SESSION_MAX_AGE", str(8 * 3600))))
    settings = Settings(hostnames=[h for h in env.get("SFTP_HOSTNAMES", "").split(",") if h],
                        cookie_secure=env.get("COOKIE_SECURE", "false").lower() == "true")
    return create_app(console, auth, settings)
