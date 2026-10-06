"""Pulling data from Plaid into the database, and the worker that schedules it."""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from decimal import Decimal
from typing import Any

from .config import Settings
from .crypto import Keys, unseal
from .db import Database, utcnow
from .plaid import PlaidClient, PlaidError
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
                continue
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
                "INSERT OR REPLACE INTO holdings(account_id, security_id, name, ticker, type, "
                "quantity, price, value, cost_basis, currency, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    h["account_id"],
                    h["security_id"],
                    sec.get("name") or sec.get("ticker_symbol") or "Holding",
                    sec.get("ticker_symbol"),
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


# -- net worth ---------------------------------------------------------------


def compute_networth(db: Database) -> dict[str, dict[str, Decimal]]:
    """Assets, liabilities, investments and net worth per currency (hidden accounts excluded)."""
    totals: dict[str, dict[str, Decimal]] = {}
    for acct in db.query("SELECT type, current_balance, currency FROM accounts WHERE hidden=0"):
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
    """Record today's net worth per currency (one row per day, overwritten on re-sync)."""
    date = date or today_str()
    with db.transaction():
        for currency, t in compute_networth(db).items():
            db.run(
                "INSERT OR REPLACE INTO networth(date, currency, assets, liabilities, "
                "investments, net_worth) VALUES (?,?,?,?,?,?)",
                (
                    date,
                    currency,
                    str(t["assets"]),
                    str(t["liabilities"]),
                    str(t["investments"]),
                    str(t["net_worth"]),
                ),
            )


# -- one bank ----------------------------------------------------------------


def sync_item(db: Database, plaid: PlaidClient, keys: Keys, item: Any, base: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "item_id": item["id"],
        "institution": item["institution"],
        "transactions_changed": 0,
        "holdings": 0,
        "ok": True,
    }
    try:
        token = item_token(keys, item)
        sync_accounts(db, plaid, token, item["id"], base)
        result["transactions_changed"] = sync_transactions(db, plaid, token, item, base)
        result["holdings"], has_investments = sync_holdings(db, plaid, token, item["id"], base)
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
                sync_item(self.db, self.plaid, self.keys, item, self.settings.base_currency)
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
