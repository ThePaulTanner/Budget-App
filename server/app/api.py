"""The HTTP API. Shapes match the fixtures in api-fixtures/ (see CLAUDE.md).

Every `/api/*` and `/auth/*` route except login and refresh needs a bearer token.
`GET /health` and the bank-linking page at `/` are public, but the page only holds a
sign-in form: it can show nothing until you log in.
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field, condecimal

from . import backup as backup_module
from . import passkeys
from . import version as version_module
from . import cashflow as cashflow_mod
from . import merchants as merchants_mod
from . import rules as rules_mod
from .auth import AuthContext, AuthError
from .bootstrap import Services
from .crypto import seal
from .db import utcnow
from .plaid import PlaidError
from .quotes import FRESH_SECONDS, LIVE_TYPES, PERIODS, Wanted, reference_prices
from .rules import (
    EFFECTIVE_CATEGORY_SQL,
    PRIMARY_CATEGORIES,
    all_primaries,
    is_primary,
    set_custom_primaries,
    is_excluded,
    normalize_category,
    parent_category,
    split_category,
)
from .positions import bank_price_and_value, effective_currency
from .sync import PlaidNotConfigured, SyncBusy, compute_networth, store_institution, today_str
from .transfers import match_transfers

STATIC_DIR = Path(__file__).parent / "static"
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

LINK_PAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.plaid.com; "
    "frame-src https://cdn.plaid.com; "
    "connect-src 'self' https://*.plaid.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://*.plaid.com; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

# The web app has no inline script, never loads anything at start-up from another site,
# and cannot be framed. The one exception is Plaid's own Link script, fetched from
# cdn.plaid.com only when you press "Link a bank". These are the same allowances the
# bank-linking page at `/` has always had (Plaid Link needs them).
APP_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.plaid.com; "
    "frame-src https://cdn.plaid.com; "
    "connect-src 'self' https://*.plaid.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://*.plaid.com; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# `__Host-` makes browsers insist on Secure, Path=/ and no Domain, so a sibling site
# cannot plant or overwrite it.
WEB_COOKIE = "__Host-hf_rt"
WEB_HEADER = "x-requested-with"
WEB_HEADER_VALUE = "HomeFinance"


def _norm_search(text: str | None) -> str:
    return " ".join((text or "").casefold().split())


def origin_of(request: Request) -> str:
    """What the proxy says about the caller. Logged for the owner to read, never trusted for
    any decision (the real peer behind Tailscale Serve is always the NAS itself)."""
    parts = []
    for label, header in (("tailscale-user", "tailscale-user-login"), ("forwarded-for", "x-forwarded-for")):
        value = request.headers.get(header)
        if value:
            parts.append(f"{label}={value}")
    return "; ".join(parts)[:120]


def num(value: Any) -> float | None:
    """Money out as a JSON number (the app decodes it exactly; see CLAUDE.md)."""
    return None if value is None else float(Decimal(str(value)))


# -- request bodies ----------------------------------------------------------


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1000)
    code: str = Field(min_length=1, max_length=64, description="Authenticator or recovery code")
    device_name: str = Field(default="Unknown device", max_length=80)


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=200)


class WebLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1000)
    code: str = Field(min_length=1, max_length=64)


class PasskeyAssertionBody(BaseModel):
    handle: str = Field(min_length=1, max_length=100)
    credential_id: str = Field(min_length=1, max_length=2048)
    client_data: str = Field(min_length=1, max_length=8192)
    authenticator_data: str = Field(min_length=1, max_length=8192)
    signature: str = Field(min_length=1, max_length=8192)


class PasskeyAddBody(BaseModel):
    handle: str = Field(min_length=1, max_length=100)
    name: str = Field(default="", max_length=100)
    password: str = Field(min_length=1, max_length=1000)
    client_data: str = Field(min_length=1, max_length=8192)
    attestation: str = Field(min_length=1, max_length=65536)


class HiddenBody(BaseModel):
    hidden: bool


class PersonBody(BaseModel):
    name: str


class OwnersBody(BaseModel):
    person_ids: list[int]


class CategoryBody(BaseModel):
    # Send either or both. `category: null` resets to automatic; `cashflow: null` lets the transfer
    # matcher decide again. A field you leave out is not changed.
    category: str | None = None
    cashflow: Literal["include", "exclude"] | None = None


class NewCategoryBody(BaseModel):
    name: str
    parent: str | None = None


class BudgetBody(BaseModel):
    monthly_limit: condecimal(gt=0, max_digits=12, decimal_places=2)  # type: ignore[valid-type]


class RuleBody(BaseModel):
    match_field: Literal["merchant_name", "name"]
    match_type: Literal["equals", "contains"]
    pattern: str = Field(min_length=1, max_length=200)
    account_id: str | None = None
    category: str = Field(min_length=1, max_length=60)


class RuleCreateBody(RuleBody):
    apply_to_past: bool = False


class RulePatchBody(BaseModel):
    match_field: Literal["merchant_name", "name"] | None = None
    match_type: Literal["equals", "contains"] | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=200)
    account_id: str | None = None
    category: str | None = Field(default=None, min_length=1, max_length=60)


class MerchantNameBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)  # saved with the merchant; null clears


class MerchantPatchBody(BaseModel):
    # Send either or both. `category: null` clears the merchant's saved category.
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)


class AliasBody(BaseModel):
    match_field: Literal["merchant_name", "name"]
    match_type: Literal["equals", "contains"]
    pattern: str = Field(min_length=1, max_length=200)


class AssignBody(AliasBody):
    name: str | None = Field(default=None, max_length=200)  # a merchant name, new or existing
    merchant_id: int | None = None  # or an existing merchant's id
    category: str | None = Field(default=None, max_length=100)  # saved on the merchant when sent


class MergeBody(BaseModel):
    into_id: int


class LinkTokenBody(BaseModel):
    item_id: str | None = Field(default=None, max_length=200)


class ExchangeBody(BaseModel):
    public_token: str = Field(min_length=1, max_length=500)


class CompleteBody(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)


# -- helpers -----------------------------------------------------------------


def parse_month(month: str | None) -> str:
    if month is None:
        return today_str()[:7]
    if not MONTH_RE.match(month):
        raise HTTPException(400, "month must look like 2026-10")
    return month


def parse_day(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, f"{name} must look like 2026-10-01") from None


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:])
    nxt = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    return f"{month}-01", nxt.isoformat()


def add_months(month: str, delta: int) -> str:
    index = int(month[:4]) * 12 + int(month[5:]) - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def category_source(row: Any) -> str:
    if row["override_category"] is not None:
        return "manual"
    if row["rule_category"] is not None:
        return "rule" if row["rule_id"] is not None else "merchant"
    return "plaid"


def tx_json(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "date": row["date"],
        "authorized_date": row["authorized_date"],
        "name": row["name"],
        "merchant_name": row["merchant_name"],
        "amount": num(row["amount"]),
        "currency": row["currency"],
        "pending": bool(row["pending"]),
        "category": row["category"],
        "plaid_category": row["plaid_category"],
        "category_detailed": row["category_detailed"],
        "subcategory": split_category(row)[1],
        "category_overridden": row["override_category"] is not None,
        "logo_url": row["logo_url"],
        "category_source": category_source(row),
        "rule_id": row["rule_id"] if row["rule_category"] is not None else None,
        "merchant_id": row["merchant_id"],
        "merchant": row["merchant_display"],
        "display_name": row["merchant_display"] or row["merchant_name"] or row["name"],
        "cashflow_override": row["cashflow_override"],
        "transfer_matched": row["transfer_pair_id"] is not None,
    }


def account_json(row: Any, institution: str | None, owner_ids: list[int] | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "institution": institution,
        "name": row["name"],
        "official_name": row["official_name"],
        "mask": row["mask"],
        "type": row["type"],
        "subtype": row["subtype"],
        "current_balance": num(row["current_balance"]),
        "available_balance": num(row["available_balance"]),
        "credit_limit": num(row["credit_limit"]),
        "currency": row["currency"],
        "hidden": bool(row["hidden"]),
        "owner_ids": owner_ids or [],
        "updated_at": row["updated_at"],
    }


def rule_json(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "match_field": row["match_field"],
        "match_type": row["match_type"],
        "pattern": row["pattern"],
        "account_id": row["account_id"],
        "category": row["category"],
        "created_at": row["created_at"],
    }


def ineligible_note(row) -> str | None:
    """Why a held security is not looked up for a live price at all."""
    if (row["type"] or "").lower() == "cash":
        return None  # cash has no price to look up
    if not row["ticker"]:
        return "the bank gives no ticker for it"
    if (row["type"] or "").lower() not in LIVE_TYPES:
        return f"the bank lists it as type '{row['type'] or 'unknown'}', which is not priced live"
    return None


def create_api_app(services: Services) -> FastAPI:
    db, auth, settings = services.db, services.auth, services.settings
    base = settings.base_currency

    def scope_sql(person_id: int | None, column: str = "a.id") -> tuple[str, list[Any]]:
        """The condition that limits a query to one person's accounts (empty for the household).
        Nothing is joined, so an account with several owners is still one row."""
        if person_id is None:
            return "", []
        if not db.one("SELECT 1 FROM people WHERE id = ?", (person_id,)):
            raise HTTPException(404, "person not found")
        return f"{column} IN (SELECT account_id FROM account_owners WHERE person_id = ?)", [person_id]

    def owners_by_account() -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for r in db.query("SELECT account_id, person_id FROM account_owners ORDER BY person_id"):
            out.setdefault(r["account_id"], []).append(r["person_id"])
        return out

    app = FastAPI(title="HomeFinance", docs_url=None, redoc_url=None, openapi_url=None)

    # -- errors and headers --------------------------------------------------

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, exc: AuthError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        parts = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"][1:])
            parts.append(f"{where}: {err['msg']}" if where else str(err["msg"]))
        return JSONResponse({"detail": "; ".join(parts) or "Invalid request"}, status_code=422)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        path = request.url.path
        if path == "/app" or path.startswith(("/api/", "/auth/", "/web/")):
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/web/") and response.status_code == 200:
            # The page asks for these as ?v=<release>, so a given address never changes: keep it for a year and
            # skip the round trip. Without ?v= the browser re-checks (a cheap 304 while the file is unchanged).
            versioned = "v" in request.query_params
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return response

    # Text (the page, its script and style, and the JSON) shrinks to about a quarter of its size.
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    def require_auth(request: Request) -> AuthContext:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthError(401, "Invalid or missing credentials", {"WWW-Authenticate": "Bearer"})
        return auth.authenticate(token.strip())

    Auth = Annotated[AuthContext, Depends(require_auth)]

    def need_plaid():
        if services.plaid is None:
            raise HTTPException(503, "Plaid is not configured on this server")
        return services.plaid

    # -- public --------------------------------------------------------------

    @app.get("/health")
    def health():
        return {"ok": True, "plaid_env": settings.plaid_env}

    @app.get("/", include_in_schema=False)
    def link_page():
        return FileResponse(
            STATIC_DIR / "index.html", headers={"Content-Security-Policy": LINK_PAGE_CSP}
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- login ---------------------------------------------------------------

    @app.post("/auth/login")
    def login(body: LoginBody, request: Request):
        ip = request.client.host if request.client else "-"
        return auth.login(body.username, body.password, body.code, body.device_name, ip, origin_of(request))

    @app.post("/auth/refresh")
    def refresh(body: RefreshBody):
        return auth.refresh(body.refresh_token)

    @app.post("/auth/logout")
    def logout(ctx: Auth):
        auth.revoke(ctx, ctx.session_id)
        return {"ok": True}

    @app.get("/auth/login-log")
    def login_log(ctx: Auth, limit: Annotated[int, Query(ge=1, le=500)] = 50):
        return auth.login_log(limit)

    @app.get("/auth/users")
    def logins(ctx: Auth):
        """Everyone who can sign in. Each has their own password and authenticator, and all of them
        see everything. A login is added on the NAS with `python -m app.cli create-user`."""
        return auth.logins(ctx)

    @app.delete("/auth/users/{user_id}", status_code=204)
    def remove_login(user_id: int, ctx: Auth):
        outcome = auth.remove_login(ctx, user_id)
        if outcome == "self":
            raise HTTPException(409, "You cannot remove the login you are signed in with.")
        if outcome == "missing":
            raise HTTPException(404, "login not found")

    @app.get("/auth/sessions")
    def sessions(ctx: Auth):
        return auth.sessions(ctx)

    @app.delete("/auth/sessions/{session_id}")
    def revoke_session(session_id: str, ctx: Auth):
        if not auth.revoke(ctx, session_id):
            raise HTTPException(404, "session not found")
        return {"ok": True}

    # -- web page sign-in (cookie session) ------------------------------------
    # The refresh token lives only in an HttpOnly cookie that page script cannot read.
    # The page keeps the short-lived access token in memory and sends it as a bearer
    # token, exactly like the iPhone app, so /api/* needs no changes and is not exposed
    # to cross-site request forgery (a browser never adds a bearer header by itself).
    # These three routes are the only ones that accept the cookie, so they also demand a
    # custom header and refuse requests the browser marks as cross-site.

    def require_same_site(request: Request) -> None:
        site = request.headers.get("sec-fetch-site")
        if request.headers.get(WEB_HEADER) != WEB_HEADER_VALUE or site not in (None, "same-origin"):
            raise AuthError(403, "Cross-site request refused")

    def web_tokens(tokens: dict) -> JSONResponse:
        response = JSONResponse(
            {
                "access_token": tokens["access_token"],
                "token_type": tokens["token_type"],
                "expires_in": tokens["expires_in"],
                "session_id": tokens["session_id"],
                **auth.web_info(tokens["session_id"]),
            }
        )
        response.set_cookie(
            WEB_COOKIE,
            tokens["refresh_token"],
            path="/",  # no max-age: a session cookie, gone when the browser closes; the server enforces the time limits
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    def web_signed_out(status: int, detail: str) -> JSONResponse:
        response = JSONResponse({"detail": detail}, status_code=status)
        response.delete_cookie(WEB_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.post("/web/login")
    def web_login(body: WebLoginBody, request: Request):
        require_same_site(request)
        ip = request.client.host if request.client else "-"
        return web_tokens(auth.login(body.username, body.password, body.code, "Web browser", ip, origin_of(request), web=True))

    @app.post("/web/passkey/options")
    def web_passkey_options(request: Request):
        require_same_site(request)
        try:
            return auth.passkey_login_options(request.headers.get("host", ""))
        except passkeys.PasskeyError as exc:
            raise AuthError(400, str(exc)) from None

    @app.post("/web/passkey/login")
    def web_passkey_login(body: PasskeyAssertionBody, request: Request):
        require_same_site(request)
        ip = request.client.host if request.client else "-"
        return web_tokens(
            auth.passkey_login(
                body.handle, body.credential_id, body.client_data, body.authenticator_data, body.signature,
                request.headers.get("host", ""), ip, origin_of(request),
            )
        )

    @app.post("/web/activity")
    def web_activity(ctx: Auth, request: Request):
        """The page tells the server you are still there (a tap, a key, a scroll). Resets the idle clock."""
        require_same_site(request)
        return auth.web_activity(ctx)

    @app.post("/web/refresh")
    def web_refresh(request: Request):
        require_same_site(request)
        token = request.cookies.get(WEB_COOKIE)
        if not token:
            raise AuthError(401, "Not signed in")
        try:
            return web_tokens(auth.refresh(token))
        except AuthError as exc:
            if exc.status == 401:
                return web_signed_out(401, exc.detail)
            raise

    @app.post("/web/logout")
    def web_logout(request: Request):
        require_same_site(request)
        token = request.cookies.get(WEB_COOKIE)
        if token:
            auth.revoke_refresh(token)
        return web_signed_out(200, "Signed out")

    @app.get("/app", include_in_schema=False)
    def web_app():
        # The script and style addresses carry the release number, so a new release is fetched once and an
        # unchanged one comes straight from the browser's cache.
        page = (STATIC_DIR / "web" / "index.html").read_text(encoding="utf-8")
        page = page.replace('<meta name="release" content="">', f'<meta name="release" content="{version_module.VERSION}">')
        for name in ("theme.js", "app.js", "app.css"):
            page = page.replace(f'/static/web/{name}"', f'/static/web/{name}?v={version_module.VERSION}"')
        return Response(page, media_type="text/html", headers={"Content-Security-Policy": APP_CSP})

    # -- summary -------------------------------------------------------------

    def category_totals(
        start: str, end: str, person_id: int | None = None
    ) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]]]:
        """Spending by top-level category, and inside each one by sub-category. A transaction is
        added once to its top-level category and, when it has one, once to its sub-category, so
        adding up the top level never counts anything twice."""
        sql = (
            "SELECT t.override_category, t.rule_category, t.plaid_category, t.category_detailed, t.amount "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE a.hidden = 0 AND t.currency = ? AND t.date >= ? AND t.date < ?"
        )
        condition, extra = scope_sql(person_id, "t.account_id")
        clause = f" AND {condition}" if condition else ""
        tops: dict[str, Decimal] = {}
        subs: dict[str, dict[str, Decimal]] = {}
        for row in db.query(sql + clause, (base, start, end, *extra)):
            top, sub = split_category(row)
            amount = Decimal(row["amount"])
            tops[top] = tops.get(top, Decimal(0)) + amount
            if sub:
                inner = subs.setdefault(top, {})
                inner[sub] = inner.get(sub, Decimal(0)) + amount
        return tops, subs

    def totals_for(start: str, end: str, person_id: int | None = None) -> dict[str, Decimal]:
        return category_totals(start, end, person_id)[0]

    def spend_total(totals: dict[str, Decimal]) -> Decimal:
        return sum(
            (v for k, v in totals.items() if not is_excluded(k)), Decimal(0)
        )

    def budget_entries(month: str) -> list[dict[str, Any]]:
        tops, subs = category_totals(*month_bounds(month))
        by_sub = {sub: v for inner in subs.values() for sub, v in inner.items()}
        entries = []
        for row in db.query("SELECT category, monthly_limit FROM budgets ORDER BY category"):
            limit = Decimal(row["monthly_limit"])
            parent = parent_category(row["category"])
            spent = (by_sub if parent else tops).get(row["category"], Decimal(0))
            percent = (spent / limit * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)
            entries.append(
                {
                    "category": row["category"],
                    "parent": parent,
                    "monthly_limit": num(limit),
                    "spent": num(spent),
                    "remaining": num(limit - spent),
                    "percent": float(percent),
                }
            )
        return entries

    def budgeted_total(budgets: list[dict[str, Any]]) -> Decimal:
        """Total of the budgets. A sub-category budget under a parent that is budgeted too is a
        share of the parent's money, so it is not added a second time."""
        budgeted = {b["category"] for b in budgets}
        return sum(
            (Decimal(str(b["monthly_limit"])) for b in budgets if b["parent"] not in budgeted),
            Decimal(0),
        )

    @app.get("/api/summary")
    def summary(ctx: Auth, person_id: int | None = None):
        month = today_str()[:7]
        start, end = month_bounds(month)
        scope_sql(person_id)  # 404 for an unknown person
        # Budgets belong to the household, so they are left out of one person's view.
        budgets = budget_entries(month) if person_id is None else []
        worth = compute_networth(db, person_id)
        currencies = sorted(worth, key=lambda c: (c != base, c))
        items = db.query("SELECT institution, needs_reauth, last_synced_at FROM items")
        synced = [r["last_synced_at"] for r in items if r["last_synced_at"]]
        return {
            "as_of": today_str(),
            "base_currency": base,
            "net_worth": [
                {
                    "currency": c,
                    "assets": num(worth[c]["assets"]),
                    "liabilities": num(worth[c]["liabilities"]),
                    "investments": num(worth[c]["investments"]),
                    "net_worth": num(worth[c]["net_worth"]),
                }
                for c in currencies
            ],
            "month": month,
            "spent_this_month": num(spend_total(totals_for(start, end, person_id))),
            "budgeted_this_month": None if person_id is not None else num(budgeted_total(budgets)),
            "over_budget": [b for b in budgets if b["spent"] > b["monthly_limit"]],
            "items_needing_attention": [
                r["institution"] or "Unknown bank" for r in items if r["needs_reauth"]
            ],
            "last_synced_at": max(synced) if synced else None,
        }

    # -- accounts ------------------------------------------------------------

    @app.get("/api/accounts")
    def accounts(ctx: Auth, include_hidden: bool = True, person_id: int | None = None):
        sql = (
            "SELECT a.*, i.institution AS institution FROM accounts a "
            "JOIN items i ON i.id = a.item_id WHERE 1=1"
        )
        if not include_hidden:
            sql += " AND a.hidden = 0"
        condition, params = scope_sql(person_id)
        if condition:
            sql += f" AND {condition}"
        sql += " ORDER BY a.type, a.name"
        owners = owners_by_account()
        return [account_json(r, r["institution"], owners.get(r["id"])) for r in db.query(sql, params)]

    @app.patch("/api/accounts/{account_id}")
    def patch_account(account_id: str, body: HiddenBody, ctx: Auth):
        if db.run("UPDATE accounts SET hidden=? WHERE id=?", (int(body.hidden), account_id)) == 0:
            raise HTTPException(404, "account not found")
        row = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        return account_json(row, None, owners_by_account().get(account_id))  # institution is null here, as in the spec

    def set_owners(account_ids: list[str], person_ids: list[int]) -> list[int]:
        wanted = sorted(set(person_ids))
        known = {r["id"] for r in db.query("SELECT id FROM people")}
        if not set(wanted) <= known:
            raise HTTPException(404, "person not found")
        with db.transaction():
            for account_id in account_ids:
                db.run("DELETE FROM account_owners WHERE account_id=?", (account_id,))
                for person in wanted:
                    db.run("INSERT INTO account_owners(account_id, person_id) VALUES (?,?)", (account_id, person))
        return wanted

    @app.put("/api/accounts/{account_id}/owners")
    def put_account_owners(account_id: str, body: OwnersBody, ctx: Auth):
        if not db.one("SELECT 1 FROM accounts WHERE id=?", (account_id,)):
            raise HTTPException(404, "account not found")
        return {"account_id": account_id, "owner_ids": set_owners([account_id], body.person_ids)}

    @app.put("/api/items/{item_id}/owners")
    def put_item_owners(item_id: str, body: OwnersBody, ctx: Auth):
        """Give every account at one bank the same owners (a shortcut for the usual case)."""
        ids = [r["id"] for r in db.query("SELECT id FROM accounts WHERE item_id=?", (item_id,))]
        if not ids and not db.one("SELECT 1 FROM items WHERE id=?", (item_id,)):
            raise HTTPException(404, "bank not found")
        return {"item_id": item_id, "accounts": len(ids), "owner_ids": set_owners(ids, body.person_ids)}

    # -- people ---------------------------------------------------------------

    def person_json(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "accounts": row["accounts"]}

    def clean_person(name: str) -> str:
        value = " ".join(name.split())
        if not 1 <= len(value) <= 40:
            raise HTTPException(400, "A name is 1 to 40 characters.")
        return value

    people_sql = (
        "SELECT p.id, p.name, (SELECT count(*) FROM account_owners o WHERE o.person_id = p.id) AS accounts "
        "FROM people p"
    )

    @app.get("/api/people")
    def people(ctx: Auth):
        return [person_json(r) for r in db.query(people_sql + " ORDER BY lower(p.name)")]

    @app.post("/api/people", status_code=201)
    def add_person(body: PersonBody, ctx: Auth):
        name = clean_person(body.name)
        if db.one("SELECT 1 FROM people WHERE lower(name) = lower(?)", (name,)):
            raise HTTPException(409, "Someone with that name already exists.")
        db.run("INSERT INTO people(name, created_at) VALUES (?,?)", (name, utcnow()))
        return person_json(db.one(people_sql + " WHERE lower(p.name) = lower(?)", (name,)))

    @app.patch("/api/people/{person_id}")
    def rename_person(person_id: int, body: PersonBody, ctx: Auth):
        name = clean_person(body.name)
        if not db.one("SELECT 1 FROM people WHERE id = ?", (person_id,)):
            raise HTTPException(404, "person not found")
        if db.one("SELECT 1 FROM people WHERE lower(name) = lower(?) AND id != ?", (name, person_id)):
            raise HTTPException(409, "Someone with that name already exists.")
        db.run("UPDATE people SET name=? WHERE id=?", (name, person_id))
        return person_json(db.one(people_sql + " WHERE p.id = ?", (person_id,)))

    @app.delete("/api/people/{person_id}")
    def delete_person(person_id: int, ctx: Auth):
        """Their accounts are kept; they just lose this owner. Their saved net-worth history goes."""
        with db.transaction():
            if db.run("DELETE FROM people WHERE id = ?", (person_id,)) == 0:
                raise HTTPException(404, "person not found")
            db.run("DELETE FROM networth WHERE person_id = ?", (person_id,))
        return {"ok": True}

    # -- transactions --------------------------------------------------------

    @app.get("/api/transactions")
    def transactions(
        ctx: Auth,
        account_id: str | None = None,
        category: str | None = None,
        search: str | None = None,
        merchant_id: int | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        person_id: int | None = None,
    ):
        where, params = ["1=1"], []
        condition, scope_params = scope_sql(person_id, "t.account_id")
        if condition:
            where.append(condition)
            params += scope_params
        if account_id:
            where.append("t.account_id = ?")
            params.append(account_id)
        else:
            where.append("a.hidden = 0")
        if category:
            try:
                wanted = normalize_category(category)
            except ValueError:
                wanted = category.strip().upper()  # not a valid name: matches nothing
            if parent_category(wanted):
                # A sub-category: transactions Plaid files under it, or that were put in it by hand.
                where.append(
                    f"({EFFECTIVE_CATEGORY_SQL} = ? OR (override_category IS NULL AND rule_category IS NULL "
                    "AND t.category_detailed = ?))"
                )
                params += [wanted, wanted]
            elif is_primary(wanted):
                # A top-level category includes everything in its sub-categories.
                where.append(f"({EFFECTIVE_CATEGORY_SQL} = ? OR {EFFECTIVE_CATEGORY_SQL} LIKE ? ESCAPE '\\')")
                params += [wanted, wanted.replace("_", "\\_") + "\\_%"]
            else:
                where.append(f"{EFFECTIVE_CATEGORY_SQL} = ?")
                params.append(wanted)
        if search and search.strip():
            like = "%" + re.sub(r"([\\%_])", r"\\\1", search.strip()) + "%"
            where.append("(t.name LIKE ? ESCAPE '\\' OR t.merchant_name LIKE ? ESCAPE '\\' OR m.name LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        if merchant_id is not None:
            where.append("t.merchant_id = ?")
            params.append(merchant_id)
        if (s := parse_day(start, "start")) is not None:
            where.append("t.date >= ?")
            params.append(s)
        if (e := parse_day(end, "end")) is not None:
            where.append("t.date <= ?")
            params.append(e)
        clause = " AND ".join(where)
        join = (
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "LEFT JOIN merchants m ON m.id = t.merchant_id"
        )
        total = db.one(f"SELECT count(*) AS n {join} WHERE {clause}", params)["n"]
        rows = db.query(
            f"SELECT t.*, {EFFECTIVE_CATEGORY_SQL} AS category, m.name AS merchant_display {join} WHERE {clause} "
            "ORDER BY t.date DESC, t.id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "transactions": [tx_json(r) for r in rows],
        }

    @app.patch("/api/transactions/{tx_id}")
    def patch_transaction(tx_id: str, body: CategoryBody, ctx: Auth):
        sent = body.model_fields_set
        if not sent:
            raise HTTPException(400, "Send category and/or cashflow.")
        try:
            category = None if body.category is None else normalize_category(body.category)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if not db.one("SELECT 1 FROM transactions WHERE id=?", (tx_id,)):
            raise HTTPException(404, "transaction not found")
        with db.transaction():
            if "category" in sent:
                db.run("UPDATE transactions SET override_category=? WHERE id=?", (category, tx_id))
            if "cashflow" in sent:
                db.run("UPDATE transactions SET cashflow_override=? WHERE id=?", (body.cashflow, tx_id))
        if "category" in sent:
            rematch()
        row = db.one(
            f"SELECT t.*, {EFFECTIVE_CATEGORY_SQL} AS category, m.name AS merchant_display "
            "FROM transactions t LEFT JOIN merchants m ON m.id = t.merchant_id WHERE t.id=?",
            (tx_id,),
        )
        return tx_json(row)

    def load_custom_primaries() -> None:
        set_custom_primaries(r["code"] for r in db.query("SELECT code FROM custom_categories WHERE parent IS NULL"))

    load_custom_primaries()

    def rematch() -> None:
        """Re-pair transfers between your own accounts. A category or rule change can make or break a
        pair, so this runs right after one (it takes milliseconds) rather than waiting for the next
        sync. A failure is logged and never fails the request, or the server's start-up."""
        try:
            match_transfers(db)
        except Exception:
            logging.getLogger("homefinance.transfers").exception("Transfer matching failed")

    rematch()  # also fills in pairs for data from before this feature

    @app.get("/api/categories")
    def categories(ctx: Auth):
        found: set[str] = set()
        for col in ("override_category", "rule_category", "plaid_category", "category_detailed"):
            found.update(r[0] for r in db.query(f"SELECT DISTINCT {col} FROM transactions") if r[0])
        found.update(r[0] for r in db.query("SELECT category FROM budgets"))
        found.update(r[0] for r in db.query("SELECT category FROM rules"))
        found.update(r[0] for r in db.query("SELECT code FROM custom_categories"))
        return sorted(found)

    def custom_json(row: Any) -> dict[str, Any]:
        code = row["code"]
        return {
            "code": code,
            "parent": row["parent"],
            "transactions": db.one(
                f"SELECT count(*) AS n FROM transactions WHERE {EFFECTIVE_CATEGORY_SQL} = ?", (code,)
            )["n"],
            "budgets": db.one("SELECT count(*) AS n FROM budgets WHERE category = ?", (code,))["n"],
            "rules": db.one("SELECT count(*) AS n FROM rules WHERE category = ?", (code,))["n"],
        }

    @app.get("/api/categories/custom")
    def custom_categories(ctx: Auth):
        """The categories and sub-categories you added, with how much uses each."""
        rows = db.query("SELECT code, parent FROM custom_categories ORDER BY code")
        return {"primaries": list(all_primaries()), "categories": [custom_json(r) for r in rows]}

    @app.post("/api/categories", status_code=201)
    def add_category(body: NewCategoryBody, ctx: Auth):
        """Add a top-level category, or a sub-category when `parent` is given. A sub-category's
        code is its parent's code, an underscore and its own name (FOOD_AND_DRINK_COFFEE)."""
        try:
            name = normalize_category(body.name)
            parent = normalize_category(body.parent) if body.parent else None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if parent is not None:
            if not is_primary(parent):
                raise HTTPException(400, "A sub-category goes under a top-level category.")
            code = f"{parent}_{name}"
        else:
            code = name
            for primary in all_primaries():
                if code.startswith(primary + "_") or primary.startswith(code + "_"):
                    raise HTTPException(
                        400, f"That name overlaps the category {primary}. Add it as a sub-category instead."
                    )
        if len(code) > 60:
            raise HTTPException(400, "That name is too long.")
        if is_primary(code) or db.one("SELECT 1 FROM custom_categories WHERE code = ?", (code,)):
            raise HTTPException(409, "That category already exists.")
        if parent is None and db.one(
            "SELECT 1 FROM custom_categories WHERE parent IS NOT NULL AND code LIKE ? ESCAPE '\\'",
            (code.replace("_", "\\_") + "\\_%",),
        ):
            raise HTTPException(400, "That name overlaps a sub-category you already added.")
        db.run("INSERT INTO custom_categories(code, parent, created_at) VALUES (?, ?, ?)", (code, parent, utcnow()))
        load_custom_primaries()
        return custom_json(db.one("SELECT code, parent FROM custom_categories WHERE code = ?", (code,)))

    @app.delete("/api/categories/{code}", status_code=204)
    def delete_category(code: str, ctx: Auth):
        row = db.one("SELECT code, parent FROM custom_categories WHERE code = ?", (code,))
        if not row:
            raise HTTPException(404, "That is not a category you added.")
        if db.one("SELECT 1 FROM custom_categories WHERE parent = ?", (code,)):
            raise HTTPException(409, "Remove its sub-categories first.")
        used = custom_json(row)
        if used["transactions"] or used["budgets"] or used["rules"]:
            raise HTTPException(
                409,
                f"In use by {used['transactions']} transaction(s), {used['budgets']} budget(s) and {used['rules']} rule(s). "
                "Move them to another category first.",
            )
        db.run("DELETE FROM custom_categories WHERE code = ?", (code,))
        load_custom_primaries()

    # -- spending and budgets ------------------------------------------------

    @app.get("/api/spending")
    def spending(ctx: Auth, month: str | None = None, person_id: int | None = None):
        month = parse_month(month)
        tops, subs = category_totals(*month_bounds(month), person_id)
        totals = {k: v for k, v in tops.items() if not is_excluded(k) and v != 0}
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        return {
            "month": month,
            "currency": base,
            "total": num(sum(totals.values(), Decimal(0))),
            "categories": [
                {
                    "category": k,
                    "spent": num(v),
                    "subcategories": [
                        {"category": sub, "spent": num(amount)}
                        for sub, amount in sorted(subs.get(k, {}).items(), key=lambda kv: kv[1], reverse=True)
                        if amount != 0
                    ],
                }
                for k, v in ranked
            ],
        }

    @app.get("/api/spending/monthly")
    def spending_monthly(
        ctx: Auth, months: Annotated[int, Query(ge=1, le=60)] = 12, person_id: int | None = None
    ):
        current = today_str()[:7]
        first = add_months(current, -(months - 1))
        sql = (
            f"SELECT {EFFECTIVE_CATEGORY_SQL} AS category, t.amount, t.date FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id "
            "WHERE a.hidden = 0 AND t.currency = ? AND t.date >= ? AND t.date < ?"
        )
        condition, extra = scope_sql(person_id, "t.account_id")
        clause = f" AND {condition}" if condition else ""
        sums: dict[str, Decimal] = {}
        for row in db.query(sql + clause, (base, f"{first}-01", month_bounds(current)[1], *extra)):
            if is_excluded(row["category"]):
                continue
            key = row["date"][:7]
            sums[key] = sums.get(key, Decimal(0)) + Decimal(row["amount"])
        return [
            {"month": m, "total": num(sums.get(m, Decimal(0)))}
            for m in (add_months(first, i) for i in range(months))
        ]

    # -- cash flow -------------------------------------------------------------

    def cashflow_params(
        range: str = "mtd",
        start: str | None = None,
        end: str | None = None,
        group: str | None = None,
        week_start: str = "mon",
        accounts: str | None = None,
        exclude_categories: str | None = None,
        include_pending: bool = True,
        include_transfers: bool = False,
        person_id: int | None = None,
    ) -> cashflow_mod.Params:
        scope_sql(person_id)  # 404 for an unknown person
        s, e = parse_day(start, "start"), parse_day(end, "end")
        split = lambda text: [v.strip() for v in text.split(",") if v.strip()]  # noqa: E731
        excluded = set()
        for code in split(exclude_categories or ""):
            try:
                excluded.add(normalize_category(code))
            except ValueError:
                raise HTTPException(400, f"Not a category: {code}") from None
        return cashflow_mod.Params(
            range=range.lower(), start=date.fromisoformat(s) if s else None, end=date.fromisoformat(e) if e else None,
            group=group.lower() if group else None, week_start=week_start.lower(),
            accounts=split(accounts) if accounts is not None else None, exclude_categories=excluded,
            include_pending=include_pending, include_transfers=include_transfers, person_id=person_id,
        )

    CashflowParams = Annotated[cashflow_mod.Params, Depends(cashflow_params)]

    @app.get("/api/cashflow")
    def cashflow(ctx: Auth, p: CashflowParams):
        """Income and expenses per day, week or month, running totals and the net worth of the
        accounts in view, for the cash-flow chart."""
        try:
            return cashflow_mod.compute(db, p, base, date.fromisoformat(today_str()))
        except cashflow_mod.CashflowError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/cashflow/transactions")
    def cashflow_transactions(ctx: Auth, p: CashflowParams, period_start: str, period_end: str, side: str = "all"):
        """Every transaction behind one bar: the ones counted, and the ones left out with the reason."""
        s, e = parse_day(period_start, "period_start"), parse_day(period_end, "period_end")
        try:
            return cashflow_mod.transactions_in(db, p, base, date.fromisoformat(s), date.fromisoformat(e), side.lower())
        except cashflow_mod.CashflowError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/budgets")
    def budgets(ctx: Auth, month: str | None = None):
        month = parse_month(month)
        return {"month": month, "currency": base, "budgets": budget_entries(month)}

    @app.put("/api/budgets/{category}")
    def put_budget(category: str, body: BudgetBody, ctx: Auth):
        try:
            name = normalize_category(category)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        db.run(
            "INSERT INTO budgets(category, monthly_limit) VALUES (?,?) "
            "ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit",
            (name, str(body.monthly_limit)),
        )
        return {"category": name, "monthly_limit": num(body.monthly_limit)}

    @app.delete("/api/budgets/{category}")
    def delete_budget(category: str, ctx: Auth):
        try:
            name = normalize_category(category)  # same cleanup as PUT, so "Eating Out" matches
        except ValueError:
            raise HTTPException(404, "budget not found") from None
        if db.run("DELETE FROM budgets WHERE category=?", (name,)) == 0:
            raise HTTPException(404, "budget not found")
        return {"ok": True}

    # -- credit card payments ------------------------------------------------

    @app.get("/api/payments-due")
    def payments_due(ctx: Auth, person_id: int | None = None):
        today = date.fromisoformat(today_str())
        condition, extra = scope_sql(person_id)
        clause = f" AND {condition}" if condition else ""
        rows = db.query(
            "SELECT a.*, i.institution AS institution, d.account_id AS dues_id, d.due_date, "
            "d.minimum_payment, d.statement_balance, d.statement_date, d.last_payment_amount, "
            "d.last_payment_date, d.is_overdue, d.updated_at AS dues_updated_at FROM accounts a "
            "JOIN items i ON i.id = a.item_id LEFT JOIN card_dues d ON d.account_id = a.id "
            "WHERE a.type = 'credit' AND a.hidden = 0" + clause,
            extra,
        )
        cards: list[dict[str, Any]] = []
        totals: dict[str, dict[str, Decimal]] = {}
        for r in rows:
            owed = Decimal(r["current_balance"] or "0")
            statement = None if r["statement_balance"] is None else Decimal(r["statement_balance"])
            minimum = None if r["minimum_payment"] is None else Decimal(r["minimum_payment"])
            last_paid = None if r["last_payment_amount"] is None else Decimal(r["last_payment_amount"])
            due = r["due_date"]
            days = (date.fromisoformat(due) - today).days if due else None
            statement_paid = statement is not None and (
                statement <= 0
                or (
                    last_paid is not None
                    and r["last_payment_date"]
                    and r["statement_date"]
                    and r["last_payment_date"] >= r["statement_date"]
                    and last_paid >= statement
                )
            )
            # Past the due date with nothing refreshed since then, we cannot know it was not paid.
            stale = days is not None and days < 0 and (r["dues_updated_at"] or "")[:10] <= due
            if owed <= 0:
                status = "nothing_owed"
            elif bool(r["is_overdue"]) and not statement_paid:
                status = "overdue"
            elif statement_paid:
                status = "paid"
            elif days is not None and days < 0:
                status = "unknown" if stale else "overdue"
            elif days is not None and days <= 7:
                status = "due_soon"
            elif days is not None:
                status = "upcoming"
            else:
                status = "unknown"
            limit = None if r["credit_limit"] is None else Decimal(r["credit_limit"])
            cards.append(
                {
                    "account_id": r["id"],
                    "institution": r["institution"],
                    "name": r["name"],
                    "mask": r["mask"],
                    "currency": r["currency"],
                    "current_balance": num(owed),
                    "credit_limit": num(limit),
                    "available_credit": num(None if limit is None else limit - owed),
                    "statement_balance": num(statement),
                    "statement_date": r["statement_date"],
                    "minimum_payment": num(minimum),
                    "due_date": due,
                    "days_until_due": days,
                    "status": status,
                    "statement_paid": statement_paid,
                    "last_payment_amount": num(last_paid),
                    "last_payment_date": r["last_payment_date"],
                    "dues_known": r["dues_id"] is not None,
                    "dues_updated_at": r["dues_updated_at"],
                }
            )
            t = totals.setdefault(r["currency"], {"owed": Decimal(0), "statement_due": Decimal(0), "minimum_due": Decimal(0)})
            t["owed"] += max(owed, Decimal(0))
            if statement is not None and not statement_paid:
                t["statement_due"] += statement
            if minimum is not None and not statement_paid:
                t["minimum_due"] += minimum
        order = {"overdue": 0, "due_soon": 1, "upcoming": 2, "unknown": 3, "paid": 4, "nothing_owed": 5}
        cards.sort(key=lambda c: (order[c["status"]], c["due_date"] or "9999-12-31", c["name"]))
        return {
            "as_of": today.isoformat(),
            "base_currency": base,
            "dues_available": any(c["dues_known"] for c in cards),
            "totals": {cur: {k: num(v) for k, v in t.items()} for cur, t in totals.items()},
            "cards": cards,
        }

    # -- investments, net worth, banks ---------------------------------------

    @app.get("/api/holdings")
    def holdings(ctx: Auth, person_id: int | None = None):
        condition, extra = scope_sql(person_id)
        clause = f" AND {condition}" if condition else ""
        rows = db.query(
            "SELECT h.*, a.name AS account_name, a.mask AS account_mask FROM holdings h "
            "JOIN accounts a ON a.id = h.account_id WHERE a.hidden = 0" + clause,
            extra,
        )
        rows.sort(key=lambda r: Decimal(r["value"]) if r["value"] is not None else Decimal("-Infinity"), reverse=True)
        totals: dict[str, Decimal] = {}
        out = []
        for r in rows:
            price, value = bank_price_and_value(r)
            cost = Decimal(r["cost_basis"]) if r["cost_basis"] is not None else None
            cur = effective_currency(r)
            if value is not None:
                totals[cur] = totals.get(cur, Decimal(0)) + value
            out.append(
                {
                    "account_id": r["account_id"],
                    "account_name": r["account_name"],
                    "account_mask": r["account_mask"],
                    "security_id": r["security_id"],
                    "name": r["name"],
                    "ticker": r["ticker"],
                    "type": r["type"],
                    "quantity": num(r["quantity"]),
                    "price": num(price),
                    "value": num(value),
                    "cost_basis": num(cost),
                    "gain": num(value - cost) if value is not None and cost is not None else None,
                    "currency": cur,
                }
            )
        return {"totals": {c: num(v) for c, v in sorted(totals.items())}, "holdings": out}

    @app.get("/api/holdings/live")
    def holdings_live(ctx: Auth, person_id: int | None = None):
        """Positions valued at live prices (Yahoo Finance), with the change since the open,
        the previous close, a week, a month, three months, the start of the year and a year
        ago. Anything without a live price keeps Plaid's last price and says so. Changes
        assume today's quantity was held throughout the period."""
        condition, extra = scope_sql(person_id)
        clause = f" AND {condition}" if condition else ""
        rows = db.query(
            "SELECT h.*, a.name AS account_name, a.mask AS account_mask FROM holdings h "
            "JOIN accounts a ON a.id = h.account_id WHERE a.hidden = 0" + clause,
            extra,
        )
        service = services.quotes
        wanted = {
            r["security_id"]: Wanted(r["ticker"], r["mic"], effective_currency(r))
            for r in rows
            if r["ticker"] and (r["type"] or "").lower() in LIVE_TYPES and r["quantity"] is not None
        }
        found = service.lookup(wanted.values()) if service is not None and wanted else None
        now_epoch = service.clock() if service is not None else 0.0
        cent, pct = Decimal("0.01"), Decimal("0.01")
        out, totals = [], {}
        for r in rows:
            cur = effective_currency(r)
            qty = Decimal(r["quantity"]) if r["quantity"] is not None else None
            price, plaid_value = bank_price_and_value(r)
            cost = Decimal(r["cost_basis"]) if r["cost_basis"] is not None else None
            live = found.live.get(wanted[r["security_id"]]) if found is not None and r["security_id"] in wanted else None
            value, changes = plaid_value, {p: None for p in PERIODS}
            if live is not None and qty is not None:
                price = live.quote.price
                value = (qty * price).quantize(cent, ROUND_HALF_UP)
                for period, ref in reference_prices(live.quote, live.bars).items():
                    if ref is not None and ref > 0:
                        changes[period] = {
                            "amount": (qty * (price - ref)).quantize(cent, ROUND_HALF_UP),
                            "percent": ((price / ref - 1) * 100).quantize(pct, ROUND_HALF_UP),
                            "_then": qty * ref,
                        }
            # A position whose currency was changed by effective_currency and that has no value yet starts no total
            # of its own, so no empty USD total appears. Any other position keeps its (possibly zero) total.
            if value is not None or cur == r["currency"]:
                bucket = totals.setdefault(cur, {"value": Decimal(0), "periods": {p: [Decimal(0), Decimal(0), 0] for p in PERIODS}})
                if value is not None:
                    bucket["value"] += value
                for period, change in changes.items():
                    if change is not None:
                        acc = bucket["periods"][period]
                        acc[0] += change["amount"]
                        acc[1] += change["_then"]
                        acc[2] += 1
            out.append(
                {
                    "account_id": r["account_id"],
                    "account_name": r["account_name"],
                    "account_mask": r["account_mask"],
                    "security_id": r["security_id"],
                    "name": r["name"],
                    "ticker": r["ticker"],
                    "type": r["type"],
                    "quantity": num(qty),
                    "price": num(price),
                    "value": num(value),
                    "cost_basis": num(cost),
                    "gain": num(value - cost) if value is not None and cost is not None else None,
                    "currency": cur,
                    "live": live is not None,
                    "symbol": live.symbol if live is not None else None,
                    "live_note": (
                        found.why.get(wanted[r["security_id"]]) if found is not None and r["security_id"] in wanted
                        else None if live is not None or found is None else ineligible_note(r)),
                    "price_time": datetime.fromtimestamp(live.quote.time, UTC).isoformat().replace("+00:00", "Z") if live is not None else None,
                    "market_open": live is not None and now_epoch - live.quote.time <= FRESH_SECONDS,
                    "open": num(live.quote.open) if live is not None else None,
                    "previous_close": num(live.quote.previous_close) if live is not None else None,
                    "changes": {
                        p: None if c is None else {"amount": num(c["amount"]), "percent": float(c["percent"])}
                        for p, c in changes.items()
                    },
                }
            )
        out.sort(key=lambda h: h["value"] if h["value"] is not None else float("-inf"), reverse=True)
        live_count = sum(1 for h in out if h["live"])
        return {
            "status": {
                "source": "yahoo" if service is not None else None,
                "enabled": service is not None,
                "live_positions": live_count,
                "positions": len(out),
                "stale": bool(found.stale) if found is not None else False,
                "message": found.unavailable if found is not None else (None if service is not None else "Live prices are turned off on the server."),
            },
            "totals": {
                c: {
                    "value": num(b["value"]),
                    "changes": {
                        p: None
                        if acc[2] == 0
                        else {
                            "amount": num(acc[0].quantize(cent, ROUND_HALF_UP)),
                            "percent": float((acc[0] / acc[1] * 100).quantize(pct, ROUND_HALF_UP)) if acc[1] > 0 else None,
                            "positions": acc[2],
                        }
                        for p, acc in b["periods"].items()
                    },
                }
                for c, b in sorted(totals.items())
            },
            "holdings": out,
        }

    @app.get("/api/investments/transactions")
    def investment_transactions(
        ctx: Auth,
        account_id: str | None = None,
        security_id: str | None = None,
        type: str | None = None,  # noqa: A002 - the query parameter is called `type`
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        person_id: int | None = None,
    ):
        """Buys, sells, dividends and fees, newest first. `amount` follows Plaid: positive is cash
        leaving the account (a buy or a fee), negative is cash coming in (a sale or a dividend).
        `quantity` is positive for a buy and negative for a sale."""
        where, params = ["a.hidden = 0"], []
        condition, scope_params = scope_sql(person_id, "it.account_id")
        if condition:
            where.append(condition)
            params += scope_params
        if account_id:
            where.append("it.account_id = ?")
            params.append(account_id)
        if security_id:
            where.append("it.security_id = ?")
            params.append(security_id)
        if type:
            where.append("it.type = ?")
            params.append(type.strip().lower())
        if (s_ := parse_day(start, "start")) is not None:
            where.append("it.date >= ?")
            params.append(s_)
        if (e_ := parse_day(end, "end")) is not None:
            where.append("it.date <= ?")
            params.append(e_)
        clause = " AND ".join(where)
        join = "FROM investment_transactions it JOIN accounts a ON a.id = it.account_id"
        total = db.one(f"SELECT count(*) AS n {join} WHERE {clause}", params)["n"]
        rows = db.query(
            f"SELECT it.*, a.name AS account_name, a.mask AS account_mask {join} WHERE {clause} "
            "ORDER BY it.date DESC, it.id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "transactions": [
                {
                    "id": r["id"],
                    "account_id": r["account_id"],
                    "account_name": r["account_name"],
                    "account_mask": r["account_mask"],
                    "security_id": r["security_id"],
                    "date": r["date"],
                    "name": r["name"],
                    "ticker": r["ticker"],
                    "type": r["type"],
                    "subtype": r["subtype"],
                    "quantity": num(r["quantity"]),
                    "price": num(r["price"]),
                    "amount": num(r["amount"]),
                    "fees": num(r["fees"]),
                    "currency": r["currency"],
                }
                for r in rows
            ],
        }

    @app.get("/api/passkeys")
    def passkey_list(ctx: Auth):
        """Your passkeys (Face ID sign-in), never anyone else's."""
        return auth.passkeys_for(ctx)

    @app.post("/api/passkeys/options")
    def passkey_options(ctx: Auth, request: Request):
        try:
            return auth.passkey_register_options(ctx, request.headers.get("host", ""))
        except passkeys.PasskeyError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/passkeys", status_code=201)
    def passkey_add(body: PasskeyAddBody, ctx: Auth, request: Request):
        try:
            return auth.passkey_register(
                ctx, body.handle, body.name, body.password, body.client_data, body.attestation, request.headers.get("host", "")
            )
        except passkeys.PasskeyError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete("/api/passkeys/{passkey_id}", status_code=204)
    def passkey_remove(passkey_id: int, ctx: Auth):
        if not auth.remove_passkey(ctx, passkey_id):
            raise HTTPException(404, "Passkey not found")
        return Response(status_code=204)

    @app.get("/api/version")
    def version(ctx: Auth):
        """Which release the server is running: `version` and the short commit `build` (null from source)."""
        return version_module.info()

    # -- backups --------------------------------------------------------------

    @app.get("/api/backups")
    def backups(ctx: Auth):
        """Copies of the database kept on the server, newest first. `last_daily` is when the timer
        last took one (null if it never has)."""
        found = backup_module.list_backups(db)
        last = backup_module.last_daily(db)
        return {
            "keep": backup_module.KEEP,
            "last_daily": last.isoformat(timespec="seconds").replace("+00:00", "Z") if last else None,
            "backups": found,
        }

    @app.post("/api/backup", status_code=201)
    def make_backup(ctx: Auth):
        try:
            return backup_module.create_backup(db, "manual")
        except Exception:
            logging.getLogger("homefinance.backup").exception("A manual backup failed")
            raise HTTPException(500, "The backup could not be written. Check the free space on the NAS.") from None

    @app.get("/api/backups/{name}")
    def download_backup(name: str, ctx: Auth):
        """One copy, as a file. It is encrypted: it opens only with your master key."""
        path = backup_module.find_backup(db, name)
        if path is None:
            raise HTTPException(404, "backup not found")
        return FileResponse(path, media_type="application/octet-stream", filename=name)

    @app.get("/api/networth")
    def networth(
        ctx: Auth,
        days: Annotated[int, Query(ge=1, le=36500)] = 365,
        currency: str | None = None,
        person_id: int | None = None,
    ):
        since = (date.fromisoformat(today_str()) - timedelta(days=days)).isoformat()
        scope_sql(person_id)  # 404 for an unknown person
        rows = db.query(
            "SELECT * FROM networth WHERE currency = ? AND date >= ? AND person_id = ? ORDER BY date",
            ((currency or base).upper(), since, person_id or 0),
        )
        return [
            {
                "date": r["date"],
                "assets": num(r["assets"]),
                "liabilities": num(r["liabilities"]),
                "investments": num(r["investments"]),
                "net_worth": num(r["net_worth"]),
                "estimated": bool(r["estimated"]),
            }
            for r in rows
        ]

    @app.get("/api/items")
    def items(ctx: Auth):
        return [
            {
                "id": r["id"],
                "institution": r["institution"],
                "error_code": r["error_code"],
                "needs_reauth": bool(r["needs_reauth"]),
                "has_investments": bool(r["has_investments"]),
                "last_synced_at": r["last_synced_at"],
                "logo": r["logo"],
                "color": r["color"],
            }
            for r in db.query("SELECT * FROM items ORDER BY created_at")
        ]

    @app.delete("/api/items/{item_id}")
    def remove_item(item_id: str, ctx: Auth):
        from .sync import item_token

        row = db.one("SELECT * FROM items WHERE id=?", (item_id,))
        if row is None:
            raise HTTPException(404, "bank not found")
        plaid = need_plaid()
        try:
            plaid.item_remove(item_token(services.keys, row))
        except PlaidError as exc:
            if exc.error_code != "ITEM_NOT_FOUND":
                raise HTTPException(502, exc.message) from None
        db.run("DELETE FROM items WHERE id=?", (item_id,))  # accounts, transactions cascade
        return {"ok": True}

    @app.post("/api/sync")
    def sync(ctx: Auth):
        try:
            return services.sync.sync_items(blocking=False, force_history=True)
        except SyncBusy:
            raise HTTPException(409, "A sync is already running") from None
        except PlaidNotConfigured:
            raise HTTPException(503, "Plaid is not configured on this server") from None

    # -- bank linking (used by the page at /) --------------------------------

    @app.post("/api/link/token")
    def link_token(body: LinkTokenBody, ctx: Auth):
        from .sync import item_token

        plaid = need_plaid()
        access_token = None
        if body.item_id:
            row = db.one("SELECT * FROM items WHERE id=?", (body.item_id,))
            if row is None:
                raise HTTPException(404, "bank not found")
            access_token = item_token(services.keys, row)
        try:
            data = plaid.link_token_create(
                str(ctx.user_id), settings.plaid_country_codes, settings.public_webhook_url, access_token
            )
        except PlaidError as exc:
            raise HTTPException(502, exc.message) from None
        return {"link_token": data["link_token"], "expiration": data.get("expiration")}

    @app.post("/api/link/exchange")
    def link_exchange(body: ExchangeBody, ctx: Auth):
        plaid = need_plaid()
        try:
            exchanged = plaid.exchange_public_token(body.public_token)
            item_id, access_token = exchanged["item_id"], exchanged["access_token"]
            institution_id = plaid.item_get(access_token).get("item", {}).get("institution_id")
        except PlaidError as exc:
            raise HTTPException(502, exc.message) from None
        details = plaid.institution_details(institution_id, settings.plaid_country_codes) if institution_id else None
        name = (details or {}).get("name") or "Bank"
        sealed = seal(access_token.encode(), services.keys.kek, b"item:" + item_id.encode())
        db.run(
            "INSERT INTO items(id, institution_id, institution, access_token, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token, "
            "institution=excluded.institution, institution_id=excluded.institution_id, "
            "needs_reauth=0, error_code=NULL",
            (item_id, institution_id, name, sealed, utcnow()),
        )
        store_institution(db, item_id, details)
        services.sync.request(item_id)  # first sync happens in the background
        return {"item_id": item_id, "institution": name}

    @app.post("/api/link/complete")
    def link_complete(body: CompleteBody, ctx: Auth):
        """After a reconnect (update mode) the bank keeps its id; clear the warning and sync."""
        if db.run(
            "UPDATE items SET needs_reauth=0, error_code=NULL WHERE id=?", (body.item_id,)
        ) == 0:
            raise HTTPException(404, "bank not found")
        services.sync.request(body.item_id)
        return {"ok": True}

    # -- categorization rules ------------------------------------------------

    def checked_rule(body: RuleBody | RulePatchBody, current: Any = None) -> dict[str, Any]:
        merged = {k: v for k, v in (dict(current) if current else {}).items()}
        merged.update({k: v for k, v in body.model_dump(exclude_unset=True).items() if k != "apply_to_past"})
        merged.setdefault("account_id", None)
        try:
            merged["category"] = normalize_category(merged["category"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if merged.get("account_id") and db.one(
            "SELECT 1 FROM accounts WHERE id=?", (merged["account_id"],)
        ) is None:
            raise HTTPException(400, "account_id does not exist")
        return merged

    @app.get("/api/rules")
    def list_rules(ctx: Auth):
        return [rule_json(r) for r in db.query("SELECT * FROM rules ORDER BY id")]

    @app.post("/api/rules/preview")
    def preview_rule(body: RuleBody, ctx: Auth):
        return rules_mod.preview(db, checked_rule(body))

    @app.post("/api/rules", status_code=201)
    def create_rule(body: RuleCreateBody, ctx: Auth):
        rule = checked_rule(body)
        rule_id = db.run(
            "INSERT INTO rules(match_field, match_type, pattern, account_id, category, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                rule["match_field"],
                rule["match_type"],
                rule["pattern"].strip(),
                rule.get("account_id"),
                rule["category"],
                utcnow(),
            ),
        )
        out = rule_json(db.one("SELECT * FROM rules WHERE id=?", (rule_id,)))
        if body.apply_to_past:
            out["updated_transactions"] = rules_mod.assign_rules(db)
            rematch()
        return out

    @app.patch("/api/rules/{rule_id}")
    def patch_rule(rule_id: int, body: RulePatchBody, ctx: Auth):
        current = db.one("SELECT * FROM rules WHERE id=?", (rule_id,))
        if current is None:
            raise HTTPException(404, "rule not found")
        rule = checked_rule(body, current)
        db.run(
            "UPDATE rules SET match_field=?, match_type=?, pattern=?, account_id=?, category=? "
            "WHERE id=?",
            (
                rule["match_field"],
                rule["match_type"],
                rule["pattern"].strip(),
                rule.get("account_id"),
                rule["category"],
                rule_id,
            ),
        )
        rules_mod.reassign_for_rule(db, rule_id)
        rematch()
        return rule_json(db.one("SELECT * FROM rules WHERE id=?", (rule_id,)))

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int, ctx: Auth):
        if db.run("DELETE FROM rules WHERE id=?", (rule_id,)) == 0:
            raise HTTPException(404, "rule not found")
        rules_mod.reassign_for_rule(db, rule_id)  # falls back to another rule, then Plaid
        rematch()
        return {"ok": True}

    @app.post("/api/rules/{rule_id}/apply")
    def apply_rule(rule_id: int, ctx: Auth):
        if db.one("SELECT 1 FROM rules WHERE id=?", (rule_id,)) is None:
            raise HTTPException(404, "rule not found")
        updated = rules_mod.assign_rules(db)
        rematch()
        return {"updated_transactions": updated}


    # -- merchants -----------------------------------------------------------

    def period_start(period: str) -> str | None:
        current = today_str()[:7]
        offsets = {"month": 0, "3m": -2, "12m": -11}
        return None if period == "all" else f"{add_months(current, offsets[period])}-01"

    def alias_json(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "merchant_id": row["merchant_id"],
            "match_field": row["match_field"],
            "match_type": row["match_type"],
            "pattern": row["pattern"],
            "source": row["source"],
            "created_at": row["created_at"],
        }

    def merchant_activity(since: str | None, merchant_id: int | None = None) -> dict[int, dict[str, Any]]:
        """Per merchant: what you spent, paid out and received, in the base currency."""
        where, params = ["a.hidden = 0", "t.merchant_id IS NOT NULL"], []
        if since is not None:
            where.append("t.date >= ?")
            params.append(since)
        if merchant_id is not None:
            where.append("t.merchant_id = ?")
            params.append(merchant_id)
        rows = db.query(
            f"SELECT t.merchant_id, t.date, t.amount, t.currency, {EFFECTIVE_CATEGORY_SQL} AS category "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE " + " AND ".join(where),
            params,
        )
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            m = out.setdefault(
                r["merchant_id"],
                {"count": 0, "spent": Decimal(0), "outflow": Decimal(0), "outflow_count": 0, "inflow": Decimal(0), "other": 0, "cats": {}},
            )
            if r["currency"] != base:
                m["other"] += 1
                continue
            amount = Decimal(r["amount"])
            m["count"] += 1
            if amount > 0:
                m["outflow"] += amount
                m["outflow_count"] += 1
            m["inflow"] += -amount if amount < 0 else Decimal(0)
            if not is_excluded(r["category"]):
                m["spent"] += amount
            m["cats"][r["category"]] = m["cats"].get(r["category"], Decimal(0)) + amount
        return out

    def top_category(cats: dict[str, Decimal]) -> str | None:
        return max(cats, key=lambda c: abs(cats[c])) if cats else None

    def merchant_json(row: Any, activity: dict[str, Any] | None, span: Any, alias_count: int) -> dict[str, Any]:
        a = activity or {"count": 0, "spent": Decimal(0), "outflow": Decimal(0), "outflow_count": 0, "inflow": Decimal(0), "other": 0, "cats": {}}
        return {
            "id": row["id"],
            "name": row["name"],
            "auto": bool(row["auto"]),
            "aliases": alias_count,
            "transactions": a["count"],
            "spent": num(a["spent"]),
            "outflow": num(a["outflow"]),
            "inflow": num(a["inflow"]),
            "other_currency_transactions": a["other"],
            "top_category": top_category(a["cats"]),
            "category": row["category"],
            "first_date": span["first"] if span else None,
            "last_date": span["last"] if span else None,
        }

    def merchant_spans() -> dict[int, Any]:
        return {
            r["merchant_id"]: r
            for r in db.query(
                "SELECT t.merchant_id, min(t.date) AS first, max(t.date) AS last "
                "FROM transactions t JOIN accounts a ON a.id = t.account_id "
                "WHERE a.hidden = 0 AND t.merchant_id IS NOT NULL GROUP BY t.merchant_id"
            )
        }

    def refresh_merchants() -> int:
        """Re-name transactions, then re-evaluate their categories (a merchant's saved category feeds in)."""
        changed = merchants_mod.assign_merchants(db)
        rules_mod.assign_rules(db)
        return changed

    def clean_merchant_category(raw: str | None) -> str | None:
        if raw is None or not raw.strip():
            return None
        try:
            return normalize_category(raw)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def merchant_or_404(merchant_id: int) -> Any:
        row = db.one("SELECT * FROM merchants WHERE id=?", (merchant_id,))
        if row is None:
            raise HTTPException(404, "merchant not found")
        return row

    @app.get("/api/merchants")
    def list_merchants(
        ctx: Auth,
        period: Literal["month", "3m", "12m", "all"] = "12m",
        search: Annotated[str | None, Query(max_length=100)] = None,
    ):
        """The master list. `spent`, `outflow`, `inflow` and `transactions` are for the period
        (base currency); first and last dates are over all time. Sorted by spent, largest first."""
        activity = merchant_activity(period_start(period))
        spans = merchant_spans()
        counts = {r["merchant_id"]: r["n"] for r in db.query("SELECT merchant_id, count(*) AS n FROM merchant_aliases GROUP BY merchant_id")}
        needle = _norm_search(search)
        rows = [
            merchant_json(r, activity.get(r["id"]), spans.get(r["id"]), counts.get(r["id"], 0))
            for r in db.query("SELECT * FROM merchants")
            if not needle or needle in _norm_search(r["name"])
        ]
        rows.sort(key=lambda m: (-(m["spent"] or 0), -m["transactions"], m["name"].casefold()))
        return {"period": period, "currency": base, "merchants": rows}

    @app.get("/api/merchants/unassigned")
    def unassigned_merchants(ctx: Auth):
        """Raw transaction names nobody has named yet (Plaid gave no clean merchant name).
        Most frequent first. Name one with POST /api/merchants/aliases."""
        groups: dict[str, dict[str, Any]] = {}
        for r in db.query(
            "SELECT t.name, t.date, t.amount, t.currency, a.name AS account_name "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE a.hidden = 0 AND t.merchant_id IS NULL ORDER BY t.date DESC"
        ):
            g = groups.setdefault(
                r["name"],
                {"name": r["name"], "transactions": 0, "outflow": Decimal(0), "last_date": r["date"], "account_name": r["account_name"], "currency": r["currency"]},
            )
            g["transactions"] += 1
            if r["currency"] == g["currency"] and Decimal(r["amount"]) > 0:
                g["outflow"] += Decimal(r["amount"])
        ranked = sorted(groups.values(), key=lambda g: (-g["transactions"], g["name"].casefold()))
        return [{**g, "outflow": num(g["outflow"])} for g in ranked]

    @app.post("/api/merchants/aliases/preview")
    def preview_alias(body: AliasBody, ctx: Auth):
        candidate = {
            "match_field": body.match_field,
            "match_type": body.match_type,
            "pattern": merchants_mod.clean_pattern(body.pattern),
        }
        if not candidate["pattern"]:
            raise HTTPException(400, "pattern cannot be empty")
        result = merchants_mod.preview(db, candidate)
        for sample in result["samples"]:
            sample["amount"] = num(sample["amount"])
        return result

    @app.post("/api/merchants/aliases", status_code=201)
    def assign_alias(body: AssignBody, ctx: Auth):
        """Give raw transaction text a merchant: an existing one (merchant_id) or a name
        (reused if it exists, created if not). Existing and future transactions follow."""
        pattern = merchants_mod.clean_pattern(body.pattern)
        if not pattern:
            raise HTTPException(400, "pattern cannot be empty")
        if (body.merchant_id is None) == (body.name is None or not body.name.strip()):
            raise HTTPException(400, "give either merchant_id or name")
        category = clean_merchant_category(body.category) if "category" in body.model_fields_set else None
        if body.merchant_id is not None:
            merchant = merchant_or_404(body.merchant_id)
        else:
            try:
                merchant = merchants_mod.get_or_create_merchant(db, body.name, auto=False)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        db.run("UPDATE merchants SET auto=0 WHERE id=?", (merchant["id"],))  # you chose it: keep it
        if "category" in body.model_fields_set:
            db.run("UPDATE merchants SET category=? WHERE id=?", (category, merchant["id"]))
        existing = db.one(
            "SELECT id FROM merchant_aliases WHERE match_field=? AND match_type=? AND lower(pattern)=lower(?)",
            (body.match_field, body.match_type, pattern),
        )
        if existing is not None:
            db.run("UPDATE merchant_aliases SET merchant_id=?, source='user' WHERE id=?", (merchant["id"], existing["id"]))
            alias_id = existing["id"]
        else:
            alias_id = db.run(
                "INSERT INTO merchant_aliases(merchant_id, match_field, match_type, pattern, source, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (merchant["id"], body.match_field, body.match_type, pattern, "user", utcnow()),
            )
        changed = refresh_merchants()
        fresh = db.one("SELECT * FROM merchants WHERE id=?", (merchant["id"],))
        return {
            "merchant": {"id": fresh["id"], "name": fresh["name"]},
            "alias": alias_json(db.one("SELECT * FROM merchant_aliases WHERE id=?", (alias_id,))),
            "updated_transactions": changed,
        }

    @app.delete("/api/merchants/aliases/{alias_id}")
    def delete_alias(alias_id: int, ctx: Auth):
        if db.run("DELETE FROM merchant_aliases WHERE id=?", (alias_id,)) == 0:
            raise HTTPException(404, "alias not found")
        refresh_merchants()
        return {"ok": True}

    @app.post("/api/merchants", status_code=201)
    def create_merchant(body: MerchantNameBody, ctx: Auth):
        try:
            name = merchants_mod.clean_name(body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if merchants_mod.find_merchant(db, name) is not None:
            raise HTTPException(409, f"A merchant named {name} already exists.")
        category = clean_merchant_category(body.category)
        row = merchants_mod.get_or_create_merchant(db, name, auto=False)
        if category:
            db.run("UPDATE merchants SET category=? WHERE id=?", (category, row["id"]))
            row = merchant_or_404(row["id"])
        return merchant_json(row, None, None, 0)

    @app.get("/api/merchants/{merchant_id}")
    def merchant_detail(
        merchant_id: int, ctx: Auth, months: Annotated[int, Query(ge=1, le=60)] = 12
    ):
        """One merchant over time: totals for the window, a month-by-month series (oldest
        first, zero months included), the raw names that map to it, and recent transactions."""
        row = merchant_or_404(merchant_id)
        current = today_str()[:7]
        first = add_months(current, -(months - 1))
        window = merchant_activity(f"{first}-01", merchant_id).get(merchant_id)
        lifetime = merchant_activity(None, merchant_id).get(merchant_id)
        span = merchant_spans().get(merchant_id)
        aliases = db.query("SELECT * FROM merchant_aliases WHERE merchant_id=? ORDER BY source, id", (merchant_id,))
        monthly: dict[str, dict[str, Any]] = {}
        cats: dict[str, Decimal] = {}
        for r in db.query(
            f"SELECT t.date, t.amount, {EFFECTIVE_CATEGORY_SQL} AS category FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id WHERE a.hidden = 0 AND t.merchant_id = ? "
            "AND t.currency = ? AND t.date >= ?",
            (merchant_id, base, f"{first}-01"),
        ):
            bucket = monthly.setdefault(r["date"][:7], {"total": Decimal(0), "count": 0})
            bucket["count"] += 1
            if not is_excluded(r["category"]):
                bucket["total"] += Decimal(r["amount"])
            cats[r["category"]] = cats.get(r["category"], Decimal(0)) + Decimal(r["amount"])
        recent = db.query(
            f"SELECT t.*, {EFFECTIVE_CATEGORY_SQL} AS category, m.name AS merchant_display "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id LEFT JOIN merchants m ON m.id = t.merchant_id "
            "WHERE t.merchant_id = ? AND a.hidden = 0 ORDER BY t.date DESC, t.id LIMIT 50",
            (merchant_id,),
        )
        out = merchant_json(row, window, span, len(aliases))
        out["months"] = months
        out["lifetime"] = {
            "transactions": lifetime["count"] if lifetime else 0,
            "spent": num(lifetime["spent"]) if lifetime else 0.0,
            "outflow": num(lifetime["outflow"]) if lifetime else 0.0,
            "inflow": num(lifetime["inflow"]) if lifetime else 0.0,
        }
        out["average_transaction"] = (
            num((window["outflow"] / window["outflow_count"]).quantize(Decimal("0.01")))
            if window and window["outflow_count"]
            else None
        )
        out["monthly"] = [
            {
                "month": m,
                "total": num(monthly.get(m, {}).get("total", Decimal(0))),
                "transactions": monthly.get(m, {}).get("count", 0),
            }
            for m in (add_months(first, i) for i in range(months))
        ]
        out["categories"] = [
            {"category": c, "amount": num(v)}
            for c, v in sorted(cats.items(), key=lambda kv: -abs(kv[1]))
            if not is_excluded(c) and v != 0
        ]
        out["alias_list"] = [alias_json(a) for a in aliases]
        out["recent_transactions"] = [tx_json(r) for r in recent]
        return out

    @app.patch("/api/merchants/{merchant_id}")
    def edit_merchant(merchant_id: int, body: MerchantPatchBody, ctx: Auth):
        """Rename a merchant and/or set its saved category (`category: null` clears it). The
        category applies to all its transactions unless a rule or you changed one by hand."""
        current = merchant_or_404(merchant_id)
        sent = body.model_fields_set
        if not ({"name", "category"} & sent):
            raise HTTPException(400, "send a name or a category")
        name = current["name"]
        if "name" in sent:
            if body.name is None:
                raise HTTPException(400, "A merchant name cannot be empty.")
            try:
                name = merchants_mod.clean_name(body.name)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            clash = db.one("SELECT id FROM merchants WHERE lower(name)=lower(?) AND id != ?", (name, merchant_id))
            if clash is not None:
                raise HTTPException(409, f"A merchant named {name} already exists. Merge them instead.")
        category = clean_merchant_category(body.category) if "category" in sent else current["category"]
        db.run("UPDATE merchants SET name=?, category=?, auto=0 WHERE id=?", (name, category, merchant_id))
        if category != current["category"]:
            rules_mod.assign_rules(db)
        row = db.one("SELECT * FROM merchants WHERE id=?", (merchant_id,))
        count = db.one("SELECT count(*) AS n FROM merchant_aliases WHERE merchant_id=?", (merchant_id,))["n"]
        return merchant_json(row, merchant_activity(None, merchant_id).get(merchant_id), merchant_spans().get(merchant_id), count)

    @app.post("/api/merchants/{merchant_id}/merge")
    def merge_merchant(merchant_id: int, body: MergeBody, ctx: Auth):
        """Fold this merchant into another: its names and transactions move across."""
        source = merchant_or_404(merchant_id)
        target = merchant_or_404(body.into_id)
        if body.into_id == merchant_id:
            raise HTTPException(400, "cannot merge a merchant into itself")
        if target["category"] is None and source["category"] is not None:
            db.run("UPDATE merchants SET category=? WHERE id=?", (source["category"], body.into_id))
        db.run("UPDATE merchant_aliases SET merchant_id=? WHERE merchant_id=?", (body.into_id, merchant_id))
        db.run("UPDATE merchants SET auto=0 WHERE id=?", (body.into_id,))
        db.run("DELETE FROM merchants WHERE id=?", (merchant_id,))
        refresh_merchants()
        return {"ok": True, "into_id": body.into_id}

    @app.delete("/api/merchants/{merchant_id}")
    def delete_merchant(merchant_id: int, ctx: Auth):
        """Forget a merchant and the names you gave it. Transactions with a Plaid merchant
        name get an automatic merchant again; the rest become unassigned."""
        merchant_or_404(merchant_id)
        db.run("DELETE FROM merchants WHERE id=?", (merchant_id,))
        refresh_merchants()
        return {"ok": True}

    return app
