"""Cash flow: income and expenses per day, week or month, running totals, the net worth of the
accounts in view, and the transactions behind each bar. Used by `/api/cashflow` and the web app's
Spending > Cash flow tab.

What counts (see CLAUDE.md for the rules the app shows):
- Only the accounts in view: by default every visible account that isn't an investment account,
  in the base currency. Buying and selling investments isn't income or spending.
- Transfers between your own accounts are left out (transfers.py pairs them), and so are
  contributions to and withdrawals from investment accounts (Plaid labels those; the investment
  side has no bank transaction to pair with).
- A refund is a negative expense, not income.
- A choice made by hand (`cashflow_override`) beats all of the above.

Numbers come back with income and expenses both positive; charts flip expenses below zero. Plaid's
own `amount` (positive = money out) is passed through untouched.
"""

from __future__ import annotations

import calendar
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .db import Database, placeholders
from .rules import EFFECTIVE_CATEGORY_SQL, top_level

ZERO = Decimal(0)
RANGES = ("mtd", "6m", "12m", "ytd", "all", "custom")
GROUPS = ("day", "week", "month")
DEFAULT_GROUP = {"mtd": "day", "6m": "week", "12m": "week", "ytd": "week", "all": "month"}
MAX_DAY_BUCKETS = 366
MAX_SPAN_DAYS = 3660
TOP_N = 5

INCOME_CATEGORIES = frozenset({"INCOME", "TRANSFER_IN", "OTHER"})
DEBT_TYPES = frozenset({"credit", "loan"})
INVESTMENT_TRANSFER_PREFIXES = ("TRANSFER_OUT_INVESTMENT", "TRANSFER_IN_INVESTMENT")


class CashflowError(ValueError):
    """Bad parameters. The API turns this into a 400 with this message."""


@dataclass
class Params:
    range: str = "mtd"
    start: date | None = None
    end: date | None = None
    group: str | None = None
    week_start: str = "mon"
    accounts: list[str] | None = None  # None = the default set
    exclude_categories: set[str] = field(default_factory=set)
    include_pending: bool = True
    include_transfers: bool = False
    person_id: int | None = None


def num(value: Decimal) -> float:
    return float(value)


# -- dates -------------------------------------------------------------------


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def resolve_range(p: Params, today: date, earliest: date | None) -> tuple[date, date]:
    if p.range not in RANGES:
        raise CashflowError(f"range must be one of {', '.join(RANGES)}")
    first = today.replace(day=1)
    if p.range == "mtd":
        return first, today
    if p.range == "6m":
        return add_months(first, -5), today
    if p.range == "12m":
        return add_months(first, -11), today
    if p.range == "ytd":
        return date(today.year, 1, 1), today
    if p.range == "all":
        return min(earliest or first, today), today
    if not p.start or not p.end:
        raise CashflowError("A custom range needs both start and end (YYYY-MM-DD).")
    start, end = p.start, min(p.end, today)
    if start > end:
        raise CashflowError("start must be on or before end, and not in the future.")
    if (end - start).days > MAX_SPAN_DAYS:
        raise CashflowError("A custom range can be at most 10 years.")
    return start, end


def previous_range(range_key: str, start: date, end: date) -> tuple[date, date] | None:
    """The stretch to compare with: one step earlier, lined up with the calendar where that matters."""
    if range_key == "all":
        return None
    if range_key == "mtd":
        prev = add_months(start, -1)
        return prev, min(prev + (end - start), month_end(prev))
    if range_key in ("6m", "12m"):
        months = 6 if range_key == "6m" else 12
        return add_months(start, -months), add_months(end, -months)
    if range_key == "ytd":
        return date(start.year - 1, 1, 1), add_months(end, -12)
    length = end - start
    prev_end = start - timedelta(days=1)
    return prev_end - length, prev_end


def default_group(range_key: str, start: date, end: date) -> str:
    if range_key in DEFAULT_GROUP:
        return DEFAULT_GROUP[range_key]
    span = (end - start).days
    return "day" if span <= 45 else "week" if span <= 370 else "month"


def _natural_start(d: date, group: str, week_start: int) -> date:
    if group == "day":
        return d
    if group == "week":
        return d - timedelta(days=(d.weekday() - week_start) % 7)
    return d.replace(day=1)


def _natural_end(start: date, group: str) -> date:
    if group == "day":
        return start
    if group == "week":
        return start + timedelta(days=6)
    return month_end(start)


def _labels(bs: date, be: date, group: str, multi_year: bool) -> tuple[str, str]:
    """(short label for the axis, long label for the hover box)."""
    if group == "day":
        return f"{bs:%b} {bs.day}", f"{bs:%a, %b} {bs.day}, {bs.year}"
    if group == "week":
        if bs.month == be.month:
            span = f"{bs:%b} {bs.day}–{be.day}, {be.year}"
        else:
            span = f"{bs:%b} {bs.day} – {be:%b} {be.day}, {be.year}"
        return f"{bs:%b} {bs.day}", f"Week of {span}"
    return (f"{bs:%b} ’{bs:%y}" if multi_year else f"{bs:%b}"), f"{bs:%B %Y}"


def make_buckets(start: date, end: date, group: str, week_start: int) -> list[dict[str, Any]]:
    if group not in GROUPS:
        raise CashflowError(f"group must be one of {', '.join(GROUPS)}")
    if group == "day" and (end - start).days + 1 > MAX_DAY_BUCKETS:
        raise CashflowError("Grouping by day is limited to one year. Pick week or month for a longer range.")
    multi_year = start.year != end.year
    buckets, cur = [], _natural_start(start, group, week_start)
    while cur <= end:
        nat_end = _natural_end(cur, group)
        bs, be = max(cur, start), min(nat_end, end)
        short, long = _labels(bs, be, group, multi_year)
        buckets.append({"index": len(buckets), "start": bs, "end": be, "label": long, "short_label": short,
                        "partial": bs != cur or be != nat_end})
        cur = nat_end + timedelta(days=1)
    return buckets


# -- classification ------------------------------------------------------------


def classify(row: Any, acct_type: str, p: Params) -> tuple[bool, str, Decimal, str | None]:
    """(counted, side, value, reason left out). `side` is "income" or "expense"; `value` is that
    side's contribution (a refund is a negative expense; an income reversal is negative income)."""
    category = row["category"]
    top = top_level(category) or "OTHER"
    amount = Decimal(row["amount"])
    outside_payment = False
    if amount < 0:  # money in
        if acct_type in DEBT_TYPES and top in ("TRANSFER_IN", "LOAN_PAYMENTS"):
            side, value, outside_payment = "income", -amount, True  # paying a card or loan from elsewhere
        elif top in INCOME_CATEGORIES or top == "LOAN_PAYMENTS":
            side, value = "income", -amount
        else:
            side, value = "expense", amount  # a refund
    elif top == "INCOME":
        side, value = "income", -amount  # e.g. a payroll reversal
    else:
        side, value = "expense", amount

    if row["cashflow_override"] == "exclude":
        return False, side, value, "Left out by you"
    if category in p.exclude_categories or top in p.exclude_categories:
        return False, side, value, "Category filtered out"
    if row["pending"] and not p.include_pending:
        return False, side, value, "Pending (hidden by the filter)"
    if row["cashflow_override"] == "include":
        return True, side, value, None
    if row["transfer_pair_id"] and not p.include_transfers:
        return False, side, value, "Transfer between your accounts"
    if (row["category_detailed"] or "").startswith(INVESTMENT_TRANSFER_PREFIXES) and not p.include_transfers:
        return False, side, value, "Moved to or from your investments"
    if outside_payment and not p.include_transfers:
        return False, side, value, "Card or loan payment"
    return True, side, value, None


def _category_source(row: Any) -> str:
    if row["override_category"] is not None:
        return "manual"
    return "rule" if row["rule_category"] is not None else "plaid"


def tx_out(row: Any, account_name: str, side: str, value: Decimal, counted: bool, reason: str | None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "date": row["date"],
        "display_name": row["merchant_display"] or row["merchant_name"] or row["name"],
        "name": row["name"],
        "merchant_name": row["merchant_name"],
        "account_id": row["account_id"],
        "account_name": account_name,
        "category": row["category"],
        "category_source": _category_source(row),
        "amount": num(Decimal(row["amount"])),
        "currency": row["currency"],
        "pending": bool(row["pending"]),
        "logo_url": row["logo_url"],
        "side": side,
        "value": num(value),
        "counted": counted,
        "reason": reason,
        "transfer_matched": row["transfer_pair_id"] is not None,
        "cashflow_override": row["cashflow_override"],
    }


# -- accounts ----------------------------------------------------------------


def select_accounts(db: Database, p: Params, base: str) -> tuple[dict[str, Any], list[str]]:
    """The accounts that count, plus notes about any left out. Default: visible, not an investment
    account. With a person, only accounts they own. Always only the base currency."""
    rows = db.query("SELECT id, name, type, currency, hidden, current_balance FROM accounts ORDER BY id")
    owned = None
    if p.person_id is not None:
        owned = {r["account_id"] for r in db.query("SELECT account_id FROM account_owners WHERE person_id = ?", (p.person_id,))}
    if p.accounts is not None:
        wanted = set(p.accounts)
        unknown = wanted - {r["id"] for r in rows}
        if unknown:
            raise CashflowError("Unknown account: " + ", ".join(sorted(unknown)))
        chosen = [r for r in rows if r["id"] in wanted]
    else:
        chosen = [r for r in rows if not r["hidden"] and r["type"] != "investment"]
    if owned is not None:
        chosen = [r for r in chosen if r["id"] in owned]
    notes = []
    other = [r for r in chosen if r["currency"] != base]
    if other:
        names = ", ".join(f"{r['name']} ({r['currency']})" for r in other)
        notes.append(f"Left out because they aren't in {base}: {names}.")
    return {r["id"]: r for r in chosen if r["currency"] == base}, notes


def _query(db: Database, ids: list[str], start: date, end: date) -> list[Any]:
    if not ids:
        return []
    return db.query(
        f"SELECT t.*, {EFFECTIVE_CATEGORY_SQL} AS category, m.name AS merchant_display "
        "FROM transactions t LEFT JOIN merchants m ON m.id = t.merchant_id "
        f"WHERE t.account_id IN ({placeholders(len(ids))}) AND t.date >= ? AND t.date <= ? ORDER BY t.date, t.id",
        [*ids, start.isoformat(), end.isoformat()],
    )


# -- net worth of the accounts in view -------------------------------------------


def net_worth_series(db: Database, accounts: dict[str, Any], days: list[date]) -> list[Decimal]:
    """Net worth of just these accounts at the end of each day: today's balances (cards and loans
    owed count against it), with every later transaction undone. Investment accounts aren't in
    view by default, so market moves don't show here. Pending transactions are left out, as banks
    leave them out of the balance."""
    now = ZERO
    for a in accounts.values():
        if a["current_balance"] is not None:
            balance = Decimal(a["current_balance"])
            now += -balance if a["type"] in DEBT_TYPES else balance
    if not accounts or not days:
        return [now for _ in days]
    ids = list(accounts)
    rows = db.query(
        f"SELECT date, amount FROM transactions WHERE account_id IN ({placeholders(len(ids))}) "
        "AND pending = 0 AND date > ? ORDER BY date",
        [*ids, min(days).isoformat()],
    )
    dates = [r["date"] for r in rows]
    suffix = [ZERO] * (len(rows) + 1)  # suffix[i] = sum of amounts from row i on
    for i in range(len(rows) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + Decimal(rows[i]["amount"])
    # Each later transaction moved net worth by -amount, so undoing it adds the amount back.
    return [now + suffix[bisect_right(dates, d.isoformat())] for d in days]


# -- entry points --------------------------------------------------------------


def compute(db: Database, p: Params, base: str, today: date) -> dict[str, Any]:
    if p.week_start not in ("mon", "sun"):
        raise CashflowError("week_start must be mon or sun")
    accounts, notes = select_accounts(db, p, base)
    ids = list(accounts)
    earliest = None
    if ids:
        first = db.one(f"SELECT min(date) AS d FROM transactions WHERE account_id IN ({placeholders(len(ids))})", ids)
        earliest = date.fromisoformat(first["d"]) if first and first["d"] else None
    start, end = resolve_range(p, today, earliest)
    group = p.group or default_group(p.range, start, end)
    buckets = make_buckets(start, end, group, 0 if p.week_start == "mon" else 6)
    prev = previous_range(p.range, start, end)

    starts = [b["start"].isoformat() for b in buckets]
    for b in buckets:
        b.update(income=ZERO, expenses=ZERO, pending_income=ZERO, pending_expenses=ZERO, income_count=0,
                 expense_count=0, _cats={}, _inc=[], _exp=[])
    prev_income = prev_expenses = ZERO

    for row in _query(db, ids, min(start, prev[0]) if prev else start, end):
        acct = accounts[row["account_id"]]
        counted, side, value, _ = classify(row, acct["type"], p)
        if not counted:
            continue
        if row["date"] < starts[0]:
            if prev and prev[0].isoformat() <= row["date"] <= prev[1].isoformat():
                if side == "income":
                    prev_income += value
                else:
                    prev_expenses += value
            continue
        b = buckets[bisect_right(starts, row["date"]) - 1]
        item = tx_out(row, acct["name"], side, value, True, None)
        if side == "income":
            b["income"] += value
            b["income_count"] += 1
            if row["pending"]:
                b["pending_income"] += value
            b["_inc"].append(item)
        else:
            b["expenses"] += value
            b["expense_count"] += 1
            if row["pending"]:
                b["pending_expenses"] += value
            top = top_level(row["category"]) or "OTHER"
            b["_cats"][top] = b["_cats"].get(top, ZERO) + value
            b["_exp"].append(item)

    nw_start, *nw = net_worth_series(db, accounts, [start - timedelta(days=1)] + [b["end"] for b in buckets])
    cum_i = cum_e = ZERO
    for b, worth in zip(buckets, nw):
        cum_i += b["income"]
        cum_e += b["expenses"]
        cats = sorted(((c, v) for c, v in b.pop("_cats").items() if v != 0), key=lambda kv: -kv[1])
        b.update(
            start=b["start"].isoformat(), end=b["end"].isoformat(),
            income=num(b["income"]), expenses=num(b["expenses"]), net=num(b["income"] - b["expenses"]),
            pending_income=num(b["pending_income"]), pending_expenses=num(b["pending_expenses"]),
            cum_income=num(cum_i), cum_expenses=num(cum_e), cum_net=num(cum_i - cum_e),
            net_worth=num(worth),
            top_categories=[{"category": c, "amount": num(v)} for c, v in cats[:8]],
            top_income=sorted(b.pop("_inc"), key=lambda t: -t["value"])[:TOP_N],
            top_expenses=sorted(b.pop("_exp"), key=lambda t: -t["value"])[:TOP_N],
        )

    complete = [b for b in buckets if not b["partial"]] or buckets
    n = len(complete)
    if not ids:
        notes.append("No accounts are selected.")
    return {
        "range": {"key": p.range, "start": start.isoformat(), "end": end.isoformat()},
        "group": group,
        "week_start": p.week_start,
        "currency": base,
        "filters": {
            "accounts": ids,
            "exclude_categories": sorted(p.exclude_categories),
            "include_pending": p.include_pending,
            "include_transfers": p.include_transfers,
        },
        "buckets": buckets,
        "totals": {
            "income": num(cum_i),
            "expenses": num(cum_e),
            "net": num(cum_i - cum_e),
            "savings_rate": round(float((cum_i - cum_e) / cum_i), 4) if cum_i > 0 else None,
            "avg_income": round(sum(b["income"] for b in complete) / n, 2),
            "avg_expenses": round(sum(b["expenses"] for b in complete) / n, 2),
            "avg_periods": n,
            "net_worth_start": num(nw_start),
            "net_worth_end": num(nw[-1]) if nw else num(nw_start),
            "net_worth_change": num((nw[-1] if nw else nw_start) - nw_start),
        },
        "previous": (
            {"start": prev[0].isoformat(), "end": prev[1].isoformat(), "income": num(prev_income),
             "expenses": num(prev_expenses), "net": num(prev_income - prev_expenses)}
            if prev else None
        ),
        "notes": notes,
    }


def transactions_in(db: Database, p: Params, base: str, start: date, end: date, side: str = "all") -> dict[str, Any]:
    """Every transaction in one period: the ones counted, and the ones left out with the reason."""
    if side not in ("all", "income", "expense"):
        raise CashflowError("side must be all, income or expense")
    if start > end:
        raise CashflowError("period_start must be on or before period_end")
    accounts, _ = select_accounts(db, p, base)
    counted, left_out = [], []
    income = expenses = ZERO
    for row in _query(db, list(accounts), start, end):
        acct = accounts[row["account_id"]]
        is_counted, tx_side, value, reason = classify(row, acct["type"], p)
        if side != "all" and tx_side != side:
            continue
        if is_counted:
            if tx_side == "income":
                income += value
            else:
                expenses += value
        (counted if is_counted else left_out).append(tx_out(row, acct["name"], tx_side, value, is_counted, reason))
    counted.sort(key=lambda t: (t["date"], t["id"]), reverse=True)
    left_out.sort(key=lambda t: (t["date"], t["id"]), reverse=True)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "side": side,
        "currency": base,
        "income": num(income),
        "expenses": num(expenses),
        "net": num(income - expenses),
        "transactions": counted,
        "left_out": left_out,
    }
