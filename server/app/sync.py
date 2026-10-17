"""Pulling data from Plaid into the database, and the worker that schedules it."""

from __future__ import annotations

import base64
import logging
import queue
import re
import threading
from datetime import datetime
from decimal import Decimal
from typing import Any

from .config import Settings
from .crypto import Keys, unseal
from .db import Database, utcnow
from .plaid import PlaidClient, PlaidError
from .merchants import assign_merchants
from .rules import assign_rules

log = logging.getLogger("homefinance.sync")

NO_INVESTMENTS = {
    "PRODUCTS_NOT_SUPPORTED",
    "NO_INVESTMENT_ACCOUNTS",
    "ADDITIONAL_CONSENT_REQUIRED",
    "INVALID_PRODUCT",
    "PRODUCT_NOT_READY",
}
LIABILITY_TYPES = {"credit", "loan"}
ASSET_TYPES = {"depository", "investment", "other"}
DEBOUNCE_SECONDS = 5.0


class SyncBusy(Exception):
    """A sync is already running."""


class PlaidNotConfigured(Exception):
    pass


def to_dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def as_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def today_str() -> str:
    return datetime.now().astimezone().date().isoformat()


def map_account_type(plaid_type: str | None) -> str:
    if plaid_type in {"depository", "credit", "loan", "investment"}:
        return plaid_type
    if plaid_type == "brokerage":
        return "investment"
    return "other"


def clean_logo(value: Any) -> str | None:
    """Plaid's logo is a base64 PNG. Keep it only if it really is one and is small."""
    if not isinstance(value, str) or not value or len(value) > 200_000:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    return value if raw.startswith(b"\x89PNG\r\n\x1a\n") else None


def clean_color(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else None


def store_institution(db: Database, item_id: str, details: dict[str, Any] | None) -> None:
    """Remember a bank's logo and colour. Only a real answer from Plaid counts as checked,
    so a failed call is tried again at the next sync."""
    if details is None:
        return
    db.run(
        "UPDATE items SET logo=?, color=?, logo_checked=1 WHERE id=?",
        (clean_logo(details.get("logo")), clean_color(details.get("color")), item_id),
    )


def item_token(keys: Keys, item: Any) -> str:
    return unseal(item["access_token"], keys.kek, b"item:" + item["id"].encode()).decode()


def _currency(obj: dict[str, Any], fallback: str) -> str:
    return obj.get("iso_currency_code") or obj.get("unofficial_currency_code") or fallback


# -- accounts ----------------------------------------------------------------


def sync_accounts(db: Database, plaid: PlaidClient, token: str, item_id: str, base: str) -> None:
    now = utcnow()
    accounts = plaid.accounts_get(token)["accounts"]
    with db.transaction():
        for acct in accounts:
            bal = acct.get("balances") or {}
            db.run(
                "INSERT INTO accounts(id, item_id, name, official_name, mask, type, subtype, "
                "current_balance, available_balance, credit_limit, currency, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET item_id=excluded.item_id, name=excluded.name, "
                "official_name=excluded.official_name, mask=excluded.mask, type=excluded.type, "
                "subtype=excluded.subtype, current_balance=excluded.current_balance, "
                "available_balance=excluded.available_balance, credit_limit=excluded.credit_limit, "
                "currency=excluded.currency, updated_at=excluded.updated_at",  # keeps `hidden`
                (
                    acct["account_id"],
                    item_id,
                    acct.get("name") or "Account",
                    acct.get("official_name"),
                    acct.get("mask"),
                    map_account_type(acct.get("type")),
                    acct.get("subtype"),
                    as_text(to_dec(bal.get("current"))),
                    as_text(to_dec(bal.get("available"))),
                    as_text(to_dec(bal.get("limit"))),
                    _currency(bal, base),
                    now,
                ),
            )


# -- transactions ------------------------------------------------------------


def _fetch_changes(plaid: PlaidClient, token: str, cursor: str | None):
    for attempt in range(3):
        added: list[dict] = []
        modified: list[dict] = []
        removed: list[dict] = []
        next_cursor = cursor
        try:
            while True:
                data = plaid.transactions_sync(token, next_cursor)
                added += data.get("added", [])
                modified += data.get("modified", [])
                removed += data.get("removed", [])
                next_cursor = data["next_cursor"]
                if not data.get("has_more"):
                    return added, modified, removed, next_cursor
        except PlaidError as exc:
            if exc.error_code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" or attempt == 2:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


def sync_transactions(
    db: Database, plaid: PlaidClient, token: str, item: Any, base: str
) -> int:
    added, modified, removed, next_cursor = _fetch_changes(plaid, token, item["cursor"])
    account_currency = {r["id"]: r["currency"] for r in db.query("SELECT id, currency FROM accounts")}
    now = utcnow()
    touched: list[str] = []
    with db.transaction():
        for tx in added + modified:
            account_id = tx["account_id"]
            if account_id not in account_currency:
                # Plaid did not list this account. Keep the transaction (the cursor moves
                # on, so it would never be sent again) under a placeholder account.
                log.warning("A transaction belongs to an account Plaid did not list; adding a placeholder")
                db.run(
                    "INSERT OR IGNORE INTO accounts(id, item_id, name, type, currency, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (account_id, item["id"], "Unknown account", "other", base, now),
                )
                account_currency[account_id] = base
            pfc = tx.get("personal_finance_category") or {}
            tx_id = tx["transaction_id"]
            carried_override = None
            pending_id = tx.get("pending_transaction_id")
            if pending_id:
                # The posted version of a pending charge keeps the user's category.
                old = db.one("SELECT override_category FROM transactions WHERE id=?", (pending_id,))
                if old is not None:
                    carried_override = old["override_category"]
                    db.run("DELETE FROM transactions WHERE id=?", (pending_id,))
            db.run(
                "INSERT INTO transactions(id, account_id, date, authorized_date, name, "
                "merchant_name, amount, currency, pending, plaid_category, category_detailed, "
                "override_category, logo_url, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id, date=excluded.date, "
                "authorized_date=excluded.authorized_date, name=excluded.name, "
                "merchant_name=excluded.merchant_name, amount=excluded.amount, "
                "currency=excluded.currency, pending=excluded.pending, "
                "plaid_category=excluded.plaid_category, "
                "category_detailed=excluded.category_detailed, logo_url=excluded.logo_url, "
                "updated_at=excluded.updated_at",  # keeps override and rule columns
                (
                    tx_id,
                    account_id,
                    tx["date"],
                    tx.get("authorized_date"),
                    tx.get("name") or tx.get("merchant_name") or "Transaction",
                    tx.get("merchant_name"),
                    str(to_dec(tx["amount"])),
                    _currency(tx, account_currency[account_id]),
                    1 if tx.get("pending") else 0,
                    pfc.get("primary") or "OTHER",
                    pfc.get("detailed"),
                    carried_override,
                    tx.get("logo_url"),
                    now,
                ),
            )
            touched.append(tx_id)
        for gone in removed:
            db.run("DELETE FROM transactions WHERE id=?", (gone["transaction_id"],))
        db.run("UPDATE items SET cursor=? WHERE id=?", (next_cursor, item["id"]))
    if touched:
        assign_rules(db, touched)
    if touched or removed:
        assign_merchants(db, touched)  # also tidies automatic merchants whose transactions were removed
    return len(added) + len(modified) + len(removed)


# -- holdings ----------------------------------------------------------------


def sync_holdings(db: Database, plaid: PlaidClient, token: str, item_id: str, base: str) -> tuple[int, bool]:
    """Returns (holding count, whether this bank has investments)."""
    try:
        data = plaid.investments_holdings_get(token)
    except PlaidError as exc:
        if exc.error_code in NO_INVESTMENTS:
            return 0, False
        raise
    securities = {s["security_id"]: s for s in data.get("securities", [])}
    account_ids = [r["id"] for r in db.query("SELECT id FROM accounts WHERE item_id=?", (item_id,))]
    now = utcnow()
    count = 0
    with db.transaction():
        for account_id in account_ids:
            db.run("DELETE FROM holdings WHERE account_id=?", (account_id,))
        for h in data.get("holdings", []):
            if h["account_id"] not in account_ids:
                continue
            sec = securities.get(h["security_id"], {})
            db.run(
                "INSERT OR REPLACE INTO holdings(account_id, security_id, name, ticker, mic, type, "
                "quantity, price, value, cost_basis, currency, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    h["account_id"],
                    h["security_id"],
                    sec.get("name") or sec.get("ticker_symbol") or "Holding",
                    sec.get("ticker_symbol"),
                    sec.get("market_identifier_code"),
                    sec.get("type"),
                    as_text(to_dec(h.get("quantity"))),
                    as_text(to_dec(h.get("institution_price"))),
                    as_text(to_dec(h.get("institution_value"))),
                    as_text(to_dec(h.get("cost_basis"))),
                    _currency(h, base),
                    now,
                ),
            )
            count += 1
    return count, True


# -- investment transactions --------------------------------------------------

FIRST_INVESTMENT_DAYS = 730  # Plaid keeps about two years
RECENT_INVESTMENT_DAYS = 60  # later syncs only look back this far (they re-read, so edits are caught)
INVESTMENT_PAGE = 500


def sync_investment_transactions(db: Database, plaid: PlaidClient, token: str, item_id: str, base: str) -> int:
    """Buys, sells, dividends and fees with their dates. Best effort, like the due dates: a bank
    that cannot give them (or is not ready yet) must not fail the whole sync. Returns rows stored."""
    from datetime import date, timedelta

    account_ids = {r["id"] for r in db.query("SELECT id FROM accounts WHERE item_id=?", (item_id,))}
    if not account_ids:
        return 0
    marks = ",".join("?" * len(account_ids))
    have = db.one(f"SELECT count(*) AS n FROM investment_transactions WHERE account_id IN ({marks})", list(account_ids))["n"]
    today = date.fromisoformat(today_str())
    start = today - timedelta(days=RECENT_INVESTMENT_DAYS if have else FIRST_INVESTMENT_DAYS)
    stored = 0
    offset = 0
    try:
        while True:
            data = plaid.investments_transactions_get(token, start.isoformat(), today.isoformat(), offset, INVESTMENT_PAGE)
            securities = {s["security_id"]: s for s in data.get("securities", [])}
            rows = data.get("investment_transactions", [])
            now = utcnow()
            with db.transaction():
                for t in rows:
                    if t.get("account_id") not in account_ids:
                        continue
                    sec = securities.get(t.get("security_id"), {})
                    db.run(
                        "INSERT OR REPLACE INTO investment_transactions(id, account_id, security_id, date, name, "
                        "ticker, type, subtype, quantity, price, amount, fees, currency, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            t["investment_transaction_id"],
                            t["account_id"],
                            t.get("security_id"),
                            t["date"],
                            t.get("name") or sec.get("name") or "Investment activity",
                            sec.get("ticker_symbol"),
                            t.get("type") or "other",
                            t.get("subtype"),
                            as_text(to_dec(t.get("quantity"))),
                            as_text(to_dec(t.get("price"))),
                            as_text(to_dec(t.get("amount"))) or "0",
                            as_text(to_dec(t.get("fees"))),
                            _currency(t, base),
                            now,
                        ),
                    )
                    stored += 1
            offset += len(rows)
            if not rows or offset >= int(data.get("total_investment_transactions") or 0):
                break
    except PlaidError as exc:
        log.info("No investment transactions for item %s: %s", item_id, exc.error_code)
    except Exception:
        log.exception("Could not store investment transactions for item %s", item_id)
    return stored


# -- credit card due dates ---------------------------------------------------


def sync_liabilities(db: Database, plaid: PlaidClient, token: str, item_id: str) -> int:
    """Credit card statement balance, minimum payment and due date. Best effort: many banks
    do not offer this, and a hiccup here must not fail the whole bank. Returns cards updated."""
    try:
        data = plaid.liabilities_get(token)
    except PlaidError as exc:
        log.info("No credit card due dates for item %s: %s", item_id, exc.error_code)
        return 0
    try:
        cards = (data.get("liabilities") or {}).get("credit") or []
        account_ids = {r["id"] for r in db.query("SELECT id FROM accounts WHERE item_id=?", (item_id,))}
        now = utcnow()
        count = 0
        with db.transaction():
            for account_id in account_ids:
                db.run("DELETE FROM card_dues WHERE account_id=?", (account_id,))
            for card in cards:
                if card.get("account_id") not in account_ids:
                    continue
                db.run(
                    "INSERT INTO card_dues(account_id, due_date, minimum_payment, "
                    "statement_balance, statement_date, last_payment_amount, last_payment_date, "
                    "is_overdue, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        card["account_id"],
                        card.get("next_payment_due_date"),
                        as_text(to_dec(card.get("minimum_payment_amount"))),
                        as_text(to_dec(card.get("last_statement_balance"))),
                        card.get("last_statement_issue_date"),
                        as_text(to_dec(card.get("last_payment_amount"))),
                        card.get("last_payment_date"),
                        1 if card.get("is_overdue") else 0,
                        now,
                    ),
                )
                count += 1
        return count
    except Exception as exc:  # a surprising reply here must not fail the bank's whole sync
        log.warning("Could not read credit card due dates for item %s (%s)", item_id, type(exc).__name__)
        return 0


# -- net worth ---------------------------------------------------------------


def compute_networth(db: Database, person_id: int | None = None) -> dict[str, dict[str, Decimal]]:
    """Assets, liabilities, investments and net worth per currency (hidden accounts excluded).

    Without a person this is the whole household and every account counts once, however many
    owners it has. With a person it is the accounts that person owns, in full: an account shared
    with someone else shows in both people's totals, never twice in the household's."""
    totals: dict[str, dict[str, Decimal]] = {}
    sql = "SELECT type, current_balance, currency FROM accounts WHERE hidden=0"
    params: tuple = ()
    if person_id is not None:
        sql += " AND id IN (SELECT account_id FROM account_owners WHERE person_id=?)"
        params = (person_id,)
    for acct in db.query(sql, params):
        if acct["current_balance"] is None:
            continue
        t = totals.setdefault(
            acct["currency"],
            {
                "assets": Decimal(0),
                "liabilities": Decimal(0),
                "investments": Decimal(0),
                "net_worth": Decimal(0),
            },
        )
        balance = Decimal(acct["current_balance"])
        if acct["type"] in LIABILITY_TYPES:
            t["liabilities"] += balance
        elif acct["type"] in ASSET_TYPES:
            t["assets"] += balance
            if acct["type"] == "investment":
                t["investments"] += balance
    for t in totals.values():
        t["net_worth"] = t["assets"] - t["liabilities"]
    return totals


def snapshot_networth(db: Database, date: str | None = None) -> None:
    """Record today's net worth per currency for the household (person_id 0) and for each
    person (one row per day and scope, overwritten on re-sync)."""
    date = date or today_str()
    scopes: list[tuple[int, int | None]] = [(0, None)]
    scopes += [(r["id"], r["id"]) for r in db.query("SELECT id FROM people")]
    with db.transaction():
        for stored_id, person in scopes:
            for currency, t in compute_networth(db, person).items():
                db.run(
                    "INSERT OR REPLACE INTO networth(date, currency, person_id, assets, liabilities, "
                    "investments, net_worth) VALUES (?,?,?,?,?,?,?)",
                    (
                        date,
                        currency,
                        stored_id,
                        str(t["assets"]),
                        str(t["liabilities"]),
                        str(t["investments"]),
                        str(t["net_worth"]),
                    ),
                )


# -- one bank ----------------------------------------------------------------


def sync_item(
    db: Database,
    plaid: PlaidClient,
    keys: Keys,
    item: Any,
    base: str,
    country_codes: tuple[str, ...] = ("CA", "US"),
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "item_id": item["id"],
        "institution": item["institution"],
        "transactions_changed": 0,
        "holdings": 0,
        "ok": True,
    }
    try:
        token = item_token(keys, item)
        if not item["logo_checked"] and item["institution_id"]:
            store_institution(db, item["id"], plaid.institution_details(item["institution_id"], country_codes))
        sync_accounts(db, plaid, token, item["id"], base)
        result["transactions_changed"] = sync_transactions(db, plaid, token, item, base)
        result["holdings"], has_investments = sync_holdings(db, plaid, token, item["id"], base)
        if has_investments:
            sync_investment_transactions(db, plaid, token, item["id"], base)
        sync_liabilities(db, plaid, token, item["id"])
        db.run(
            "UPDATE items SET last_synced_at=?, error_code=NULL, needs_reauth=0, "
            "has_investments=? WHERE id=?",
            (utcnow(), 1 if has_investments else 0, item["id"]),
        )
    except PlaidError as exc:
        log.warning("Sync failed for item %s: %s", item["id"], exc.error_code)
        login_required = exc.error_code == "ITEM_LOGIN_REQUIRED"
        db.run(
            "UPDATE items SET error_code=?, needs_reauth=? WHERE id=?",
            (exc.error_code, 1 if login_required else 0, item["id"]),
        )
        result.update(ok=False, error=exc.message)
    except Exception:
        # A corrupt token, a surprising Plaid payload, a full disk: record it for this bank
        # and carry on with the others instead of failing the whole sync.
        log.exception("Unexpected error syncing item %s", item["id"])
        db.run("UPDATE items SET error_code=? WHERE id=?", ("INTERNAL_ERROR", item["id"]))
        result.update(ok=False, error="Unexpected error while syncing this bank")
    return result


# -- scheduling --------------------------------------------------------------


class SyncManager:
    """Runs syncs one at a time: on demand, when a webhook arrives, and on a timer."""

    def __init__(self, db: Database, keys: Keys, plaid: PlaidClient | None, settings: Settings):
        self.db = db
        self.keys = keys
        self.plaid = plaid
        self.settings = settings
        self._lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sync_items(self, item_ids: list[str] | None = None, *, blocking: bool = False) -> list[dict]:
        if self.plaid is None:
            raise PlaidNotConfigured()
        if not self._lock.acquire(blocking=blocking):
            raise SyncBusy()
        try:
            if item_ids is None:
                items = self.db.query("SELECT * FROM items ORDER BY created_at")
            else:
                items = [
                    row
                    for item_id in item_ids
                    if (row := self.db.one("SELECT * FROM items WHERE id=?", (item_id,)))
                ]
            results = [
                sync_item(self.db, self.plaid, self.keys, item, self.settings.base_currency, self.settings.plaid_country_codes)
                for item in items
            ]
            snapshot_networth(self.db)
            return results
        finally:
            self._lock.release()

    def request(self, item_id: str) -> None:
        """Ask the worker to sync one bank soon (used by webhooks)."""
        self._queue.put(item_id)

    def start(self) -> None:
        if self._thread is None and self.plaid is not None:
            self._thread = threading.Thread(target=self._run, name="sync-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        interval = max(60, self.settings.sync_interval_minutes * 60)
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=interval)
            except queue.Empty:
                self._safe_sync(None)  # backup timer, in case a webhook never arrived
                continue
            if first is None:
                break
            wanted = {first}
            while True:  # let a burst of webhooks settle, then sync each bank once
                try:
                    more = self._queue.get(timeout=DEBOUNCE_SECONDS)
                except queue.Empty:
                    break
                if more is None:
                    return
                wanted.add(more)
            self._safe_sync(sorted(wanted))

    def _safe_sync(self, item_ids: list[str] | None) -> None:
        try:
            self.sync_items(item_ids, blocking=True)
        except Exception:  # keep the worker alive whatever happens
            log.exception("Background sync failed")
