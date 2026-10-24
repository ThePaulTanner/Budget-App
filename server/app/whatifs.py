"""What if you had not sold? Each past sale played out against what happened to the money.

For a sale of *q* shares on day *t* for *P*:

* **If kept:** the same shares, worth what you were paid on the day and then moving as the share's price moved since
  that day's close (converted to the base currency at each day's rate). So the two paths start level on the sale day.
* **What happened to the money:** by default it is followed. Buys in the same account within a few days of the sale
  are matched to it (the money was most likely put into them) and the proceeds then move as those purchases did; any
  part not matched stays as cash that stayed put (also moving with the exchange rate when it is in another currency).
  `proceeds="cash"` takes all of it as cash. Following it matters: the account's real value already contains what the
  money bought, so the comparison has to take that out, not just the cash.
* **The gap** is the first less the second. Positive means keeping the shares would have left you better off today.

The page's "if kept" line is the real account value plus every sale's gap from its date on. It is hindsight, in
prices only: no dividends the shares would have paid, no tax, and closing prices rather than the price you got.
Sales older than the two years Plaid keeps cannot be seen.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .history import Unavailable, investment_series
from .performance import DataUnavailable, FETCH_RANGE, _closes_for, _Fx
from .quotes import QuoteService, Wanted

RANGES = {"3m": 92, "6m": 183, "1y": 366, "all": None}
DEFAULT_RANGE = "1y"
DEFAULT_MIN_AMOUNT = 50.0  # sales smaller than this (in the base currency) are hidden: they are noise
REINVEST_DAYS = 5  # a buy within this many days of a sale, in the same account, is taken to be where the money went
EVEN_BAND = 0.005  # within half a percent of the proceeds counts as "about even"


def _verdict(gap_percent: float | None) -> str:
    if gap_percent is None or abs(gap_percent) <= EVEN_BAND:
        return "even"
    return "missed_gain" if gap_percent > 0 else "good_call"


def build(
    db: Any,
    quotes: QuoteService,
    *,
    base: str,
    range_: str,
    today: date,
    scope: tuple[str, list[Any]] = ("", []),
    account_id: str | None = None,
    min_amount: float = DEFAULT_MIN_AMOUNT,
    proceeds: str = "auto",
) -> dict[str, Any]:
    """The What ifs page. `proceeds` is "auto" (follow the money into what it bought) or "cash".
    Raises DataUnavailable if prices cannot be had now."""
    result: dict[str, Any] = {
        "available": False,
        "range": range_,
        "currency": base,
        "as_of": today.isoformat(),
        "min_amount": min_amount,
        "proceeds": proceeds,
        "assumption": (
            "Every share you sold is taken as kept. The money you were paid is followed into what you bought with it, "
            f"when you bought within {REINVEST_DAYS} days in the same account, and is otherwise cash that stayed put. "
            "Price changes only, at closing prices."
            if proceeds == "auto"
            else "Every share you sold is taken as kept, and what you were paid as cash that stayed put. Price changes only, at closing prices."
        ),
    }
    if quotes.resting():
        raise DataUnavailable("market data is resting after an error")
    condition, extra = scope
    where, params = ["a.hidden = 0", "t.type = 'sell'", "CAST(t.quantity AS REAL) < 0"], list(extra)
    days_back = RANGES.get(range_)
    if days_back is not None:
        where.append("t.date >= ?")
        params.append((today - timedelta(days=days_back)).isoformat())
    if account_id:
        where.append("a.id = ?")
        params.append(account_id)
    if condition:
        where.append(condition)
    sales = db.query(
        "SELECT t.id, t.date, t.name, t.ticker, t.security_id, t.mic, t.quantity, t.price, t.amount, t.currency, "
        "t.account_id, a.name AS account_name FROM investment_transactions t JOIN accounts a ON a.id = t.account_id "
        f"WHERE {' AND '.join(where)} ORDER BY t.date, t.id",
        params,
    )
    if not sales:
        result["message"] = "You have not sold anything in this period, so there is nothing to play out."
        return result

    # A security sold out of the account is no longer in the holdings: take its market from any record that has one.
    mics: dict[str, str] = {}
    for row in db.query("SELECT security_id, mic FROM holdings WHERE mic IS NOT NULL AND mic != ''"):
        mics.setdefault(row["security_id"], row["mic"])
    for row in db.query("SELECT security_id, mic FROM investment_transactions WHERE mic IS NOT NULL AND mic != ''"):
        mics.setdefault(row["security_id"], row["mic"])

    fx = _Fx(quotes, base)
    closes_cache: dict[tuple, Any] = {}
    shown: list[dict[str, Any]] = []
    hidden_small = 0
    for t in sales:
        proceeds_local = abs(Decimal(t["amount"]))
        qty = abs(Decimal(t["quantity"]))
        currency = t["currency"]
        if not fx.usable(currency):
            sale_base = None
        else:
            sale_base = float(proceeds_local) * fx.rate(currency, date.fromisoformat(t["date"]))
        if sale_base is not None and sale_base < min_amount:
            hidden_small += 1
            continue
        mic = t["mic"] or mics.get(t["security_id"]) or None
        entry: dict[str, Any] = {
            "id": t["id"], "date": t["date"], "ticker": t["ticker"], "name": t["name"], "account_id": t["account_id"],
            "account_name": t["account_name"], "quantity": float(qty),
            "price": float(Decimal(t["price"])) if t["price"] else None, "currency": currency,
            "proceeds": round(sale_base, 2) if sale_base is not None else None,
            "priced": False, "reason": None, "start": None, "kept_path": None, "proceeds_path": None,
            "followed": [], "proceeds_mode": None,
            "kept_now": None, "proceeds_now": None, "gap": None, "gap_percent": None, "verdict": None,
            "_qty": qty, "_proceeds_local": proceeds_local, "_closes": None, "_sale_base": sale_base,
        }
        if not t["ticker"]:
            entry["reason"] = "no ticker to look up"
        elif sale_base is None:
            entry["reason"] = f"no exchange rate for {currency}"
        else:
            key = (t["ticker"], mic, currency)
            if key not in closes_cache:
                closes_cache[key] = _closes_for(quotes, Wanted(t["ticker"], mic, currency))
            if closes_cache[key] is None:
                entry["reason"] = "Yahoo Finance has no price history for it"
            elif float(closes_cache[key].on(date.fromisoformat(t["date"]))) <= 0:
                entry["reason"] = "its price history is not usable around the sale"
            else:
                entry["_closes"] = closes_cache[key]
                entry["priced"] = True
        shown.append(entry)

    priced = [e for e in shown if e["priced"]]
    unpriced = [e for e in shown if not e["priced"]]
    coverage = {
        "sells": len(shown) + hidden_small,
        "shown": len(shown),
        "priced": len(priced),
        "unpriced": [{"ticker": e["ticker"], "name": e["name"], "date": e["date"], "reason": e["reason"]} for e in unpriced],
        "hidden_small": hidden_small,
        "window_start": (today - timedelta(days=days_back)).isoformat() if days_back is not None else None,
    }
    result["coverage"] = coverage
    if not priced:
        result["sells"] = [_public(e) for e in shown]
        result["message"] = (
            "Every sale in this period is smaller than the minimum, so none is shown." if not shown
            else "None of the sales in this period has a price history to play out."
        )
        return result

    first = min(date.fromisoformat(e["date"]) for e in priced)
    dates = sorted({d for e in priced for d in e["_closes"].days if first <= d <= today})
    if len(dates) < 2:
        result["sells"] = [_public(e) for e in shown]
        result["message"] = "There is not enough price history yet to play these out."
        return result

    allocations = _match_buys(db, fx, quotes, priced, closes_cache, mics) if proceeds == "auto" else {}
    gap_total = [0.0] * len(dates)
    for e in priced:
        start = bisect_left(dates, date.fromisoformat(e["date"]))
        e["start"] = start
        local = float(e["_proceeds_local"])
        sale_close = float(e["_closes"].on(date.fromisoformat(e["date"])))
        allocs = allocations.get(e["id"], [])
        kept, path = [], []
        for d in dates[start:]:
            rate = fx.rate(e["currency"], d)
            kept.append(local * float(e["_closes"].on(d)) / sale_close * rate)  # what was paid, riding the share's moves
            path.append(_money_value(fx, e, allocs, d))
        gap = kept[-1] - path[-1]
        pct = gap / e["_sale_base"] if e["_sale_base"] else None  # of what you were paid
        covered = sum(a["share"] for a in allocs)
        e.update(
            kept_path=[round(v, 2) for v in kept], proceeds_path=[round(v, 2) for v in path],
            kept_now=round(kept[-1], 2), proceeds_now=round(path[-1], 2), gap=round(gap, 2), gap_percent=pct,
            verdict=_verdict(pct),
            followed=[
                {"id": a["info"]["row"]["id"], "ticker": a["info"]["row"]["ticker"], "name": a["info"]["row"]["name"],
                 "date": a["info"]["date"].isoformat(), "share": round(a["share"], 4)}
                for a in allocs
            ],
            proceeds_mode="followed" if covered >= 0.995 else "partly" if covered > 0 else "cash",
        )
        for i, (k, p) in enumerate(zip(kept, path)):
            gap_total[start + i] += k - p

    actual = _actual_series(db, quotes, fx, dates, scope, account_id)
    gap_sum = sum(e["gap"] for e in priced)
    paid_sum = sum(e["_sale_base"] for e in priced)
    now_sum = sum(e["proceeds_now"] for e in priced)
    result.update(
        {
            "available": True,
            "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(),
            "dates": [d.isoformat() for d in dates],
            "actual": actual,
            "kept": [round(a + g, 2) for a, g in zip(actual, gap_total)] if actual is not None else None,
            "gap_total": [round(g, 2) for g in gap_total],
            "sells": [_public(e) for e in shown],
            "totals": {
                "gap": round(gap_sum, 2),
                "gap_percent": gap_sum / paid_sum if paid_sum > 0 else None,
                "proceeds": round(paid_sum, 2),
                "proceeds_now": round(now_sum, 2),
                "kept_now": round(sum(e["kept_now"] for e in priced), 2),
                "missed": sum(1 for e in priced if e["verdict"] == "missed_gain"),
                "good": sum(1 for e in priced if e["verdict"] == "good_call"),
                "even": sum(1 for e in priced if e["verdict"] == "even"),
            },
        }
    )
    return result


def _match_buys(
    db: Any, fx: _Fx, quotes: QuoteService, sales: list[dict[str, Any]], closes_cache: dict[tuple, Any], mics: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """For each sale, the buys its money most likely went into: in the same account, on the sale day or within
    REINVEST_DAYS after it. Sales are taken oldest first and each buy can only be spent once, so two sales cannot both
    claim the same purchase. A buy that cannot be priced is passed over (that part of the money stays cash)."""
    accounts = sorted({e["account_id"] for e in sales})
    if not accounts:
        return {}
    first = min(date.fromisoformat(e["date"]) for e in sales)
    rows = db.query(
        "SELECT id, account_id, date, name, ticker, security_id, mic, amount, currency FROM investment_transactions "
        f"WHERE type = 'buy' AND CAST(quantity AS REAL) > 0 AND date >= ? AND account_id IN ({','.join('?' * len(accounts))}) "
        "ORDER BY date, id",
        [first.isoformat(), *accounts],
    )
    buys: list[dict[str, Any]] = []
    for b in rows:
        if not b["ticker"] or not fx.usable(b["currency"]):
            continue
        amount = abs(Decimal(b["amount"]))
        if amount <= 0:
            continue
        mic = b["mic"] or mics.get(b["security_id"]) or None
        key = (b["ticker"], mic, b["currency"])
        if key not in closes_cache:
            closes_cache[key] = _closes_for(quotes, Wanted(b["ticker"], mic, b["currency"]))
        closes = closes_cache[key]
        bought = date.fromisoformat(b["date"])
        if closes is None or float(closes.on(bought)) <= 0:
            continue
        buys.append({"row": b, "date": bought, "closes": closes, "left": float(amount) * fx.rate(b["currency"], bought)})
    out: dict[str, list[dict[str, Any]]] = {}
    for e in sorted(sales, key=lambda e: (e["date"], e["id"])):
        sold, paid = date.fromisoformat(e["date"]), e["_sale_base"]
        left, allocs = paid, []
        for buy in buys:
            if left <= 0.01 or buy["left"] <= 0.01:
                continue
            if buy["row"]["account_id"] != e["account_id"] or not (sold <= buy["date"] <= sold + timedelta(days=REINVEST_DAYS)):
                continue
            take = min(left, buy["left"])
            buy["left"] -= take
            left -= take
            allocs.append({"info": buy, "share": take / paid})
        out[e["id"]] = allocs
    return out


def _money_value(fx: _Fx, e: dict[str, Any], allocs: list[dict[str, Any]], d: date) -> float:
    """What the money from one sale is worth on day d: cash until it was spent, then moving as the purchase did.
    Anything not matched to a purchase stays cash. Cash moves only with the exchange rate."""
    local = float(e["_proceeds_local"])
    rate = fx.rate(e["currency"], d)
    value = (1 - sum(a["share"] for a in allocs)) * local * rate
    for a in allocs:
        info = a["info"]
        if d < info["date"]:
            value += a["share"] * local * rate
        else:
            bought, currency = info["date"], info["row"]["currency"]
            at_buy = a["share"] * local * fx.rate(e["currency"], bought)  # the cash that was spent, in the base currency
            price = float(info["closes"].on(d)) / float(info["closes"].on(bought))
            value += at_buy * price * fx.rate(currency, d) / fx.rate(currency, bought)
    return value


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def _actual_series(
    db: Any, quotes: QuoteService, fx: _Fx, dates: list[date], scope: tuple[str, list[Any]], account_id: str | None
) -> list[float] | None:
    """The real value of the investment accounts on each date, worked back from today's positions (the same way the
    net worth history is). None when an account's positions cannot be told, so the page shows only the gap."""
    condition, extra = scope
    where, params = ["a.hidden = 0", "a.type = 'investment'", "a.current_balance IS NOT NULL"], list(extra)
    if account_id:
        where.append("a.id = ?")
        params.append(account_id)
    if condition:
        where.append(condition)
    accounts = db.query(f"SELECT a.id, a.type, a.currency, a.current_balance FROM accounts a WHERE {' AND '.join(where)}", params)
    if not accounts:
        return None
    calendar = [dates[0] + timedelta(days=n) for n in range((dates[-1] - dates[0]).days + 1)]  # every day: transactions land on any
    total = [0.0] * len(dates)
    for account in accounts:
        if not fx.usable(account["currency"]):
            return None
        if not db.one("SELECT 1 FROM holdings WHERE account_id = ?", (account["id"],)):
            continue  # an empty account is worth nothing, on every day
        try:
            series = investment_series(db, account, calendar, quotes, FETCH_RANGE)
        except Unavailable as exc:
            raise DataUnavailable(str(exc)) from None
        if series is None:
            return None
        for i, d in enumerate(dates):
            total[i] += float(series[d]) * fx.rate(account["currency"], d)
    return [round(v, 2) for v in total]
