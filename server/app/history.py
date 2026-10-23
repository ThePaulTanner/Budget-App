"""Net worth for the days before the server began recording it, rebuilt from your transactions and market prices.

The recorded history (`networth` rows with `estimated = 0`) starts the day the first bank was synced. Before that
day this module works backwards from today:

* A bank, credit or loan account: today's balance with every transaction after a day undone.
* An investment account: today's positions with every buy and sell after a day undone (so the quantity held on that
  day), each priced at that day's Yahoo close and converted to the account's currency at that day's rate, then scaled
  so today matches the bank's own balance. Cash moves with the account's cash flows.

An account linked after recording began was not in the recorded days before its bank was linked. What it would have
added on those days is worked out the same way and kept in `networth_adjust`; the networth endpoint adds it to the
recorded rows and calls those days estimates, so the chart does not jump on the day a bank was added.

These are estimates (`estimated = 1`): a deposit or transfer a bank never reported, or a security Yahoo does not know,
makes them drift. They are rebuilt from scratch each time and never replace a recorded day.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .db import Database
from .positions import bank_price_and_value, effective_currency
from .quotes import QuoteError, QuoteService, Wanted
from .sync import ASSET_TYPES, LIABILITY_TYPES, today_str

log = logging.getLogger("homefinance.history")

MAX_DAYS = 730  # Plaid keeps about two years of transactions
CENT = Decimal("0.01")
SCALE_LIMITS = (Decimal("0.5"), Decimal("2"))  # a reconstruction further than this from the bank's balance is not trusted


class Unavailable(Exception):
    """Prices could not be fetched right now; try again later."""


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def _sum_by_date(rows: list[tuple[str, Decimal]]) -> dict[date, Decimal]:
    out: dict[date, Decimal] = defaultdict(Decimal)
    for day, amount in rows:
        out[date.fromisoformat(day)] += amount
    return out


def _after_sums(by_date: dict[date, Decimal], days: list[date]) -> dict[date, Decimal]:
    """For each day, the sum of everything dated strictly after it."""
    out: dict[date, Decimal] = {}
    running = Decimal(0)
    for day in reversed(days):
        out[day] = running
        running += by_date.get(day, Decimal(0))
    return out


class Closes:
    """A security's daily closes, carried across weekends and holidays."""

    def __init__(self, bars: list[tuple[date, Decimal]]) -> None:
        bars = sorted(bars)
        self.days = [d for d, _ in bars]
        self.values = [c for _, c in bars]

    def __bool__(self) -> bool:
        return bool(self.days)

    def on(self, day: date) -> Decimal:
        index = bisect_right(self.days, day) - 1
        return self.values[max(index, 0)]  # before the first close, the first close


def bank_series(db: Database, account: Any, days: list[date]) -> dict[date, Decimal]:
    """A bank, credit card or loan balance on each day: today's, with later transactions undone.

    A positive amount is money out (Plaid's sign). For an asset account that lowered the balance, so earlier it was
    higher. For a credit or loan account (balance = amount owed) a positive amount raised what is owed. Pending
    transactions are in a card's balance but not in a deposit account's."""
    liability = account["type"] in LIABILITY_TYPES
    rows = db.query("SELECT date, amount, pending FROM transactions WHERE account_id=?", (account["id"],))
    amounts = [(r["date"], Decimal(r["amount"])) for r in rows if liability or not r["pending"]]
    later = _after_sums(_sum_by_date(amounts), days)
    current = Decimal(account["current_balance"])
    sign = -1 if liability else 1
    return {day: current + sign * later[day] for day in days}


def investment_series(
    db: Database, account: Any, days: list[date], quotes: QuoteService | None, range_: str
) -> dict[date, Decimal] | None:
    """An investment account's value on each day, or None if its positions cannot be told (then it stays flat)."""
    holdings = db.query("SELECT * FROM holdings WHERE account_id=?", (account["id"],))
    if not holdings:
        return None
    txns = db.query(
        "SELECT security_id, date, type, quantity, amount FROM investment_transactions WHERE account_id=?", (account["id"],)
    )
    cash_flow = _after_sums(_sum_by_date([(t["date"], Decimal(t["amount"])) for t in txns]), days)
    by_security: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
    for t in txns:
        if t["quantity"]:
            by_security[t["security_id"]].append((t["date"], Decimal(t["quantity"])))
    account_currency = account["currency"]
    fx_cache: dict[str, Closes | None] = {}

    def fx(currency: str) -> Closes | None:
        if currency == account_currency:
            return None
        if currency not in fx_cache:
            try:
                bars = quotes.long_closes(f"{currency}{account_currency}=X", range_) if quotes else []
            except QuoteError as exc:
                if exc.kind == "unavailable":
                    raise Unavailable(str(exc)) from None
                bars = []
            fx_cache[currency] = Closes(bars) if bars else None
        return fx_cache[currency]

    total = {day: Decimal(0) for day in days}
    today = days[-1]
    priced = 0
    for h in holdings:
        quantity_now = Decimal(h["quantity"] or "0")
        price_now, value_now = bank_price_and_value(h)
        currency = effective_currency(h)
        rate = fx(currency)
        if (h["type"] or "").lower() == "cash":
            amounts = {day: quantity_now + cash_flow[day] for day in days}  # cash is one unit = one dollar
            closes = None
        else:
            moved = _after_sums(_sum_by_date(by_security.get(h["security_id"], [])), days)
            amounts = {day: quantity_now - moved[day] for day in days}
            closes = None
            if quotes is not None and h["ticker"]:
                try:
                    bars = quotes.daily_closes(Wanted(h["ticker"], h["mic"], currency), range_)
                except QuoteError as exc:
                    if exc.kind == "unavailable":
                        raise Unavailable(str(exc)) from None
                    bars = []
                closes = Closes(bars) if bars else None
        flat = price_now if price_now is not None else (value_now / quantity_now if value_now is not None and quantity_now > 0 else None)
        if closes is None and flat is None and (h["type"] or "").lower() != "cash":
            continue  # no price from the bank or from Yahoo: this position cannot be valued
        priced += 1
        for day in days:
            unit = Decimal(1) if closes is None and (h["type"] or "").lower() == "cash" else (closes.on(day) if closes else flat)
            to_account = rate.on(day) if rate else Decimal(1)
            total[day] += max(amounts[day], Decimal(0)) * unit * to_account
    if priced == 0:
        return None
    # Scale so today matches the bank's own figure (prices, rates and anything unreported differ a little).
    bank_today = Decimal(account["current_balance"])
    if total[today] > 0 and bank_today > 0:
        k = bank_today / total[today]
        if SCALE_LIMITS[0] <= k <= SCALE_LIMITS[1]:
            return {day: value * k for day, value in total.items()}
        log.info("Net worth history for an investment account is %.0f%% of the bank's balance today; left unscaled", k * 100)
    return total


def rebuild_networth_history(db: Database, quotes: QuoteService | None, today: date | None = None) -> int:
    """Rebuild every estimated day before the first recorded one. Returns how many rows were written.

    Raises Unavailable when prices cannot be had now; nothing is changed then, so the last good estimate stays."""
    today = today or date.fromisoformat(today_str())
    if quotes is not None and quotes.resting():
        raise Unavailable("market data is resting after an error")
    accounts = [
        a
        for a in db.query(
            "SELECT a.id, a.type, a.currency, a.current_balance, substr(i.created_at, 1, 10) AS added "
            "FROM accounts a JOIN items i ON i.id = a.item_id WHERE a.hidden=0"
        )
        if a["current_balance"] is not None
    ]
    if not accounts:
        return 0
    ids = [a["id"] for a in accounts]
    marks = ",".join("?" * len(ids))
    first = [
        row["d"]
        for row in (
            db.one(f"SELECT MIN(date) AS d FROM transactions WHERE account_id IN ({marks})", ids),
            db.one(f"SELECT MIN(date) AS d FROM investment_transactions WHERE account_id IN ({marks})", ids),
        )
        if row and row["d"]
    ]
    if not first:
        return 0
    # One day before the first transaction, so the balance from before it shows too.
    start = max(date.fromisoformat(min(first)) - timedelta(days=1), today - timedelta(days=MAX_DAYS))
    if start >= today:
        return 0
    days = _days(start, today)
    range_ = "2y" if (today - start).days <= 700 else "5y"

    series: dict[str, dict[date, Decimal]] = {}
    for account in accounts:
        built = None
        if account["type"] == "investment":
            built = investment_series(db, account, days, quotes, range_)
            if built is None:  # positions cannot be told: stay flat at today's balance
                built = {day: Decimal(account["current_balance"]) for day in days}
        else:
            built = bank_series(db, account, days)
        series[account["id"]] = built

    types = {a["id"]: (a["type"], a["currency"]) for a in accounts}
    added = {a["id"]: date.fromisoformat(a["added"]) for a in accounts if a["added"]}
    first_day = days[0]
    scopes: list[tuple[int, set[str]]] = [(0, set(ids))]
    for person in db.query("SELECT id FROM people"):
        owned = {r["account_id"] for r in db.query("SELECT account_id FROM account_owners WHERE person_id=?", (person["id"],))}
        scopes.append((person["id"], owned & set(ids)))

    written = 0
    adjust: dict[tuple[date, str, int], dict[str, Decimal]] = {}
    with db.transaction():
        db.run("DELETE FROM networth WHERE estimated=1")
        db.run("DELETE FROM networth_adjust")
        for person_id, owned in scopes:
            real = db.one("SELECT MIN(date) AS d FROM networth WHERE person_id=? AND estimated=0", (person_id,))
            boundary = date.fromisoformat(real["d"]) if real and real["d"] else today
            # Recorded days before an account's bank was linked: add what that account would have been on them.
            for account_id in owned:
                kind, currency = types[account_id]
                joined = added.get(account_id)
                if joined is None or joined <= boundary:
                    continue
                for day in _days(boundary, min(joined - timedelta(days=1), today)):
                    value = series[account_id].get(day, series[account_id][first_day])  # before the first transaction: unchanged
                    parts = {"assets": Decimal(0), "liabilities": Decimal(0), "investments": Decimal(0)}
                    if kind in LIABILITY_TYPES:
                        parts["liabilities"] = value
                    elif kind in ASSET_TYPES:
                        parts["assets"] = value
                        if kind == "investment":
                            parts["investments"] = value
                    adjust[(day, currency, person_id)] = {k: adjust.get((day, currency, person_id), {}).get(k, Decimal(0)) + v for k, v in parts.items()}
            for day in days:
                if day >= boundary:
                    break
                totals: dict[str, dict[str, Decimal]] = {}
                for account_id in owned:
                    kind, currency = types[account_id]
                    value = series[account_id][day]
                    t = totals.setdefault(currency, {"assets": Decimal(0), "liabilities": Decimal(0), "investments": Decimal(0)})
                    if kind in LIABILITY_TYPES:
                        t["liabilities"] += value
                    elif kind in ASSET_TYPES:
                        t["assets"] += value
                        if kind == "investment":
                            t["investments"] += value
                for currency, t in totals.items():
                    db.run(
                        "INSERT OR IGNORE INTO networth(date, currency, person_id, assets, liabilities, investments, net_worth, estimated) "
                        "VALUES (?,?,?,?,?,?,?,1)",
                        (
                            day.isoformat(),
                            currency,
                            person_id,
                            str(t["assets"].quantize(CENT, ROUND_HALF_UP)),
                            str(t["liabilities"].quantize(CENT, ROUND_HALF_UP)),
                            str(t["investments"].quantize(CENT, ROUND_HALF_UP)),
                            str((t["assets"] - t["liabilities"]).quantize(CENT, ROUND_HALF_UP)),
                        ),
                    )
                    written += 1
        for (day, currency, person_id), t in adjust.items():
            db.run(
                "INSERT OR REPLACE INTO networth_adjust(date, currency, person_id, assets, liabilities, investments) VALUES (?,?,?,?,?,?)",
                (day.isoformat(), currency, person_id, *(str(t[k].quantize(CENT, ROUND_HALF_UP)) for k in ("assets", "liabilities", "investments"))),
            )
    return written
