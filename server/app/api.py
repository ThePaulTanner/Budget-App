"""The HTTP API. Shapes match the fixtures in api-fixtures/ (see CLAUDE.md).

Every `/api/*` and `/auth/*` route except login and refresh needs a bearer token.
`GET /health` and the bank-linking page at `/` are public, but the page only holds a
sign-in form: it can show nothing until you log in.
"""

import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, condecimal

from . import rules as rules_mod
from .auth import AuthContext, AuthError
from .bootstrap import Services
from .crypto import seal
from .db import utcnow
from .plaid import PlaidError
from .rules import EFFECTIVE_CATEGORY_SQL, EXCLUDED_FROM_SPENDING, normalize_category
from .sync import PlaidNotConfigured, SyncBusy, compute_networth, today_str

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

# The web app has no inline script or style, loads nothing from other sites, and
# cannot be framed. That is what keeps injected script (XSS) out of your data.
APP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# `__Host-` makes browsers insist on Secure, Path=/ and no Domain, so a sibling site
# cannot plant or overwrite it.
WEB_COOKIE = "__Host-hf_rt"
WEB_COOKIE_MAX_AGE = 30 * 24 * 3600  # renewed each time the page refreshes
WEB_HEADER = "x-requested-with"
WEB_HEADER_VALUE = "HomeFinance"


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


class HiddenBody(BaseModel):
    hidden: bool


class CategoryBody(BaseModel):
    category: str | None  # required, but may be null (reset to automatic)


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
        return "rule"
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
        "category_overridden": row["override_category"] is not None,
        "logo_url": row["logo_url"],
        "category_source": category_source(row),
        "rule_id": row["rule_id"] if row["rule_category"] is not None else None,
    }


def account_json(row: Any, institution: str | None) -> dict[str, Any]:
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


def create_api_app(services: Services) -> FastAPI:
    db, auth, settings = services.db, services.auth, services.settings
    base = settings.base_currency
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
        return response

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
        return auth.login(body.username, body.password, body.code, body.device_name, ip)

    @app.post("/auth/refresh")
    def refresh(body: RefreshBody):
        return auth.refresh(body.refresh_token)

    @app.post("/auth/logout")
    def logout(ctx: Auth):
        auth.revoke(ctx, ctx.session_id)
        return {"ok": True}

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
            }
        )
        response.set_cookie(
            WEB_COOKIE,
            tokens["refresh_token"],
            max_age=WEB_COOKIE_MAX_AGE,
            path="/",
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
        return web_tokens(auth.login(body.username, body.password, body.code, "Web browser", ip))

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
        return FileResponse(STATIC_DIR / "web" / "index.html", headers={"Content-Security-Policy": APP_CSP})

    # -- summary -------------------------------------------------------------

    def totals_for(start: str, end: str, category: str | None = None) -> dict[str, Decimal]:
        sql = (
            f"SELECT {EFFECTIVE_CATEGORY_SQL} AS category, t.amount FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id "
            "WHERE a.hidden = 0 AND t.currency = ? AND t.date >= ? AND t.date < ?"
        )
        totals: dict[str, Decimal] = {}
        for row in db.query(sql, (base, start, end)):
            totals[row["category"]] = totals.get(row["category"], Decimal(0)) + Decimal(row["amount"])
        return totals

    def spend_total(totals: dict[str, Decimal]) -> Decimal:
        return sum(
            (v for k, v in totals.items() if k not in EXCLUDED_FROM_SPENDING), Decimal(0)
        )

    def budget_entries(month: str) -> list[dict[str, Any]]:
        start, end = month_bounds(month)
        totals = totals_for(start, end)
        entries = []
        for row in db.query("SELECT category, monthly_limit FROM budgets ORDER BY category"):
            limit = Decimal(row["monthly_limit"])
            spent = totals.get(row["category"], Decimal(0))
            percent = (spent / limit * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)
            entries.append(
                {
                    "category": row["category"],
                    "monthly_limit": num(limit),
                    "spent": num(spent),
                    "remaining": num(limit - spent),
                    "percent": float(percent),
                }
            )
        return entries

    @app.get("/api/summary")
    def summary(ctx: Auth):
        month = today_str()[:7]
        start, end = month_bounds(month)
        budgets = budget_entries(month)
        worth = compute_networth(db)
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
            "spent_this_month": num(spend_total(totals_for(start, end))),
            "budgeted_this_month": num(sum((Decimal(str(b["monthly_limit"])) for b in budgets), Decimal(0))),
            "over_budget": [b for b in budgets if b["spent"] > b["monthly_limit"]],
            "items_needing_attention": [
                r["institution"] or "Unknown bank" for r in items if r["needs_reauth"]
            ],
            "last_synced_at": max(synced) if synced else None,
        }

    # -- accounts ------------------------------------------------------------

    @app.get("/api/accounts")
    def accounts(ctx: Auth, include_hidden: bool = True):
        sql = (
            "SELECT a.*, i.institution AS institution FROM accounts a "
            "JOIN items i ON i.id = a.item_id"
        )
        if not include_hidden:
            sql += " WHERE a.hidden = 0"
        sql += " ORDER BY a.type, a.name"
        return [account_json(r, r["institution"]) for r in db.query(sql)]

    @app.patch("/api/accounts/{account_id}")
    def patch_account(account_id: str, body: HiddenBody, ctx: Auth):
        if db.run("UPDATE accounts SET hidden=? WHERE id=?", (int(body.hidden), account_id)) == 0:
            raise HTTPException(404, "account not found")
        row = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        return account_json(row, None)  # institution is null here, as in the spec

    # -- transactions --------------------------------------------------------

    @app.get("/api/transactions")
    def transactions(
        ctx: Auth,
        account_id: str | None = None,
        category: str | None = None,
        search: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        where, params = ["1=1"], []
        if account_id:
            where.append("t.account_id = ?")
            params.append(account_id)
        else:
            where.append("a.hidden = 0")
        if category:
            where.append(f"{EFFECTIVE_CATEGORY_SQL} = ?")
            try:
                params.append(normalize_category(category))
            except ValueError:
                params.append(category.strip().upper())  # not a valid name: matches nothing
        if search and search.strip():
            like = "%" + re.sub(r"([\\%_])", r"\\\1", search.strip()) + "%"
            where.append("(t.name LIKE ? ESCAPE '\\' OR t.merchant_name LIKE ? ESCAPE '\\')")
            params += [like, like]
        if (s := parse_day(start, "start")) is not None:
            where.append("t.date >= ?")
            params.append(s)
        if (e := parse_day(end, "end")) is not None:
            where.append("t.date <= ?")
            params.append(e)
        clause = " AND ".join(where)
        join = "FROM transactions t JOIN accounts a ON a.id = t.account_id"
        total = db.one(f"SELECT count(*) AS n {join} WHERE {clause}", params)["n"]
        rows = db.query(
            f"SELECT t.*, {EFFECTIVE_CATEGORY_SQL} AS category {join} WHERE {clause} "
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
        try:
            category = None if body.category is None else normalize_category(body.category)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if db.run("UPDATE transactions SET override_category=? WHERE id=?", (category, tx_id)) == 0:
            raise HTTPException(404, "transaction not found")
        row = db.one(
            f"SELECT *, {EFFECTIVE_CATEGORY_SQL} AS category FROM transactions WHERE id=?", (tx_id,)
        )
        return tx_json(row)

    @app.get("/api/categories")
    def categories(ctx: Auth):
        found: set[str] = set()
        for col in ("override_category", "rule_category", "plaid_category"):
            found.update(r[0] for r in db.query(f"SELECT DISTINCT {col} FROM transactions") if r[0])
        found.update(r[0] for r in db.query("SELECT category FROM budgets"))
        found.update(r[0] for r in db.query("SELECT category FROM rules"))
        return sorted(found)

    # -- spending and budgets ------------------------------------------------

    @app.get("/api/spending")
    def spending(ctx: Auth, month: str | None = None):
        month = parse_month(month)
        totals = {
            k: v
            for k, v in totals_for(*month_bounds(month)).items()
            if k not in EXCLUDED_FROM_SPENDING and v != 0
        }
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        return {
            "month": month,
            "currency": base,
            "total": num(sum(totals.values(), Decimal(0))),
            "categories": [{"category": k, "spent": num(v)} for k, v in ranked],
        }

    @app.get("/api/spending/monthly")
    def spending_monthly(ctx: Auth, months: Annotated[int, Query(ge=1, le=60)] = 12):
        current = today_str()[:7]
        first = add_months(current, -(months - 1))
        sql = (
            f"SELECT {EFFECTIVE_CATEGORY_SQL} AS category, t.amount, t.date FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id "
            "WHERE a.hidden = 0 AND t.currency = ? AND t.date >= ? AND t.date < ?"
        )
        sums: dict[str, Decimal] = {}
        for row in db.query(sql, (base, f"{first}-01", month_bounds(current)[1])):
            if row["category"] in EXCLUDED_FROM_SPENDING:
                continue
            key = row["date"][:7]
            sums[key] = sums.get(key, Decimal(0)) + Decimal(row["amount"])
        return [
            {"month": m, "total": num(sums.get(m, Decimal(0)))}
            for m in (add_months(first, i) for i in range(months))
        ]

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

    # -- investments, net worth, banks ---------------------------------------

    @app.get("/api/holdings")
    def holdings(ctx: Auth):
        rows = db.query(
            "SELECT h.*, a.name AS account_name, a.mask AS account_mask FROM holdings h "
            "JOIN accounts a ON a.id = h.account_id WHERE a.hidden = 0"
        )
        rows.sort(key=lambda r: Decimal(r["value"]) if r["value"] is not None else Decimal("-Infinity"), reverse=True)
        totals: dict[str, Decimal] = {}
        out = []
        for r in rows:
            value = Decimal(r["value"]) if r["value"] is not None else None
            cost = Decimal(r["cost_basis"]) if r["cost_basis"] is not None else None
            if value is not None:
                totals[r["currency"]] = totals.get(r["currency"], Decimal(0)) + value
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
                    "price": num(r["price"]),
                    "value": num(value),
                    "cost_basis": num(cost),
                    "gain": num(value - cost) if value is not None and cost is not None else None,
                    "currency": r["currency"],
                }
            )
        return {"totals": {c: num(v) for c, v in sorted(totals.items())}, "holdings": out}

    @app.get("/api/networth")
    def networth(
        ctx: Auth,
        days: Annotated[int, Query(ge=1, le=36500)] = 365,
        currency: str | None = None,
    ):
        since = (date.fromisoformat(today_str()) - timedelta(days=days)).isoformat()
        rows = db.query(
            "SELECT * FROM networth WHERE currency = ? AND date >= ? ORDER BY date",
            ((currency or base).upper(), since),
        )
        return [
            {
                "date": r["date"],
                "assets": num(r["assets"]),
                "liabilities": num(r["liabilities"]),
                "investments": num(r["investments"]),
                "net_worth": num(r["net_worth"]),
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
            return services.sync.sync_items(blocking=False)
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
        name = (
            plaid.institution_name(institution_id, settings.plaid_country_codes)
            if institution_id
            else None
        ) or "Bank"
        sealed = seal(access_token.encode(), services.keys.kek, b"item:" + item_id.encode())
        db.run(
            "INSERT INTO items(id, institution_id, institution, access_token, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token, "
            "institution=excluded.institution, institution_id=excluded.institution_id, "
            "needs_reauth=0, error_code=NULL",
            (item_id, institution_id, name, sealed, utcnow()),
        )
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
        return rule_json(db.one("SELECT * FROM rules WHERE id=?", (rule_id,)))

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int, ctx: Auth):
        if db.run("DELETE FROM rules WHERE id=?", (rule_id,)) == 0:
            raise HTTPException(404, "rule not found")
        rules_mod.reassign_for_rule(db, rule_id)  # falls back to another rule, then Plaid
        return {"ok": True}

    @app.post("/api/rules/{rule_id}/apply")
    def apply_rule(rule_id: int, ctx: Auth):
        if db.one("SELECT 1 FROM rules WHERE id=?", (rule_id,)) is None:
            raise HTTPException(404, "rule not found")
        return {"updated_transactions": rules_mod.assign_rules(db)}

    return app
