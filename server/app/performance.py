"""How the investments have done, against a benchmark, with the numbers professionals use.

The method is a *holdings-based* backtest, the same assumption the live position changes already make: today's
quantities are taken to have been held for the whole period, each priced at that day's Yahoo close and converted to the
base currency at that day's exchange rate. That answers "how have the things I own now behaved?", which is the
question that guides what to do next. It is not an account statement: it ignores what was bought and sold along the
way, dividends (prices only) and fees, and it cannot see a security Yahoo does not know (those stay flat and are
counted as "not priced"). The benchmark is measured the same way (price only, in the base currency).

Everything here is arithmetic on daily closes. Nothing leaves the NAS except ticker symbols (see quotes.py).
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable

from .history import Closes
from .positions import bank_price_and_value, effective_currency
from .quotes import QuoteError, QuoteService, Wanted

log = logging.getLogger(__name__)

TRADING_DAYS = 252
FETCH_RANGE = "5y"  # one long history per symbol, kept for hours, and every period is cut from it
RANGES = {"1m": 31, "3m": 92, "6m": 183, "1y": 366, "3y": 1096, "5y": 1827}  # "ytd" is worked out from the date
MIN_DAYS_FOR_ANNUAL = 30  # a period shorter than this is not annualised: the figure would mean nothing
DEFAULT_RISK_FREE = 0.03  # a year, as a fraction: roughly a short government bill. The page lets you change it.
PRICED_SHARE_FOR_START = 0.9  # the history starts once positions holding this share of the money have prices

# key: (name, Yahoo symbol, currency the index is quoted in). "balanced" is built from two of the others.
BENCHMARKS: dict[str, tuple[str, str, str]] = {
    "sp500": ("S&P 500", "^GSPC", "USD"),
    "tsx": ("S&P/TSX Composite", "^GSPTSE", "CAD"),
    "world": ("MSCI World (URTH)", "URTH", "USD"),
    "bonds": ("US bonds (AGG)", "AGG", "USD"),
}
BALANCED = ("balanced", "60/40: S&P 500 and bonds", (("sp500", 0.6), ("bonds", 0.4)))
DEFAULT_BENCHMARK = "sp500"
NON_PRICED_TYPES = {"cash"}
INCOME_WORDS = ("dividend",)
INTEREST_WORDS = ("interest",)


class DataUnavailable(Exception):
    """Market data cannot be had right now (Yahoo is resting or unreachable)."""


# -- the arithmetic -------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def daily_returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1 if values[i - 1] else 0.0 for i in range(1, len(values))]


def drawdowns(values: list[float]) -> list[float]:
    """Each day's fall from the highest value so far, as a fraction (0 at a new high, -0.2 = 20% below)."""
    peak, out = float("-inf"), []
    for v in values:
        peak = max(peak, v)
        out.append(v / peak - 1 if peak > 0 else 0.0)
    return out


def worst_drawdown(dates: list[date], values: list[float]) -> dict[str, Any]:
    dd = drawdowns(values)
    if not dd:
        return {"depth": 0.0, "peak": None, "trough": None, "recovered": None, "days_down": 0, "current": 0.0}
    trough_i = min(range(len(dd)), key=lambda i: dd[i])
    peak_i = max(range(trough_i + 1), key=lambda i: values[i])
    recovered = next((dates[i] for i in range(trough_i, len(values)) if values[i] >= values[peak_i]), None)
    if dd[trough_i] >= 0:
        return {"depth": 0.0, "peak": None, "trough": None, "recovered": None, "days_down": 0, "current": dd[-1]}
    end = recovered or dates[-1]
    return {
        "depth": dd[trough_i],
        "peak": dates[peak_i].isoformat(),
        "trough": dates[trough_i].isoformat(),
        "recovered": recovered.isoformat() if recovered else None,
        "days_down": (end - dates[peak_i]).days,
        "current": dd[-1],
    }


def monthly_returns(dates: list[date], values: list[float]) -> list[dict[str, Any]]:
    """Return of each calendar month from the last value of the month before. The first and last months may be partial."""
    out: list[dict[str, Any]] = []
    prior = values[0] if values else 0.0
    month, last = None, None
    for d, v in zip(dates, values):
        key = d.strftime("%Y-%m")
        if month is not None and key != month:
            out.append({"month": month, "value": last / prior - 1 if prior else 0.0, "end": last})
            prior = last
        month, last = key, v
    if month is not None:
        out.append({"month": month, "value": last / prior - 1 if prior else 0.0, "end": last})
    return out


def solo_metrics(dates: list[date], values: list[float], rf: float) -> dict[str, Any]:
    """Return and risk of one series."""
    if len(values) < 2 or values[0] <= 0:
        return {}
    r = daily_returns(values)
    days = (dates[-1] - dates[0]).days
    total = values[-1] / values[0] - 1
    cagr = (values[-1] / values[0]) ** (365.25 / days) - 1 if days >= MIN_DAYS_FOR_ANNUAL and values[-1] > 0 else None
    rf_d = (1 + rf) ** (1 / TRADING_DAYS) - 1
    excess = [x - rf_d for x in r]
    vol = _stdev(r) * math.sqrt(TRADING_DAYS)
    downside = math.sqrt(_mean([min(0.0, x) ** 2 for x in excess])) * math.sqrt(TRADING_DAYS)
    ann_excess = _mean(excess) * TRADING_DAYS
    dd = worst_drawdown(dates, values)
    months = monthly_returns(dates, values)
    best_day = max(range(len(r)), key=lambda i: r[i])
    worst_day = min(range(len(r)), key=lambda i: r[i])
    full_months = months[1:-1] if len(months) > 2 else months
    var = -_percentile(r, 0.05) if len(r) >= 60 else None
    tail = [x for x in r if x <= _percentile(r, 0.05)] if len(r) >= 60 else []
    return {
        "total_return": total,
        "cagr": cagr,
        "volatility": vol if len(r) >= 20 else None,
        "sharpe": ann_excess / vol if vol > 1e-12 and len(r) >= 20 else None,
        "sortino": ann_excess / downside if downside > 1e-12 and len(r) >= 20 else None,
        "max_drawdown": dd["depth"],
        "drawdown": dd,
        "calmar": cagr / abs(dd["depth"]) if cagr is not None and dd["depth"] < -1e-9 else None,
        "best_day": {"date": dates[best_day + 1].isoformat(), "return": r[best_day]},
        "worst_day": {"date": dates[worst_day + 1].isoformat(), "return": r[worst_day]},
        "best_month": max(full_months, key=lambda m: m["value"])["month"] if full_months else None,
        "best_month_return": max(m["value"] for m in full_months) if full_months else None,
        "worst_month": min(full_months, key=lambda m: m["value"])["month"] if full_months else None,
        "worst_month_return": min(m["value"] for m in full_months) if full_months else None,
        "positive_days": sum(1 for x in r if x > 0) / len(r),
        "positive_months": sum(1 for m in full_months if m["value"] > 0) / len(full_months) if full_months else None,
        "var_95": var,
        "cvar_95": -_mean(tail) if tail else None,
        "days": days,
    }


def relative_metrics(
    dates: list[date], portfolio: list[float], benchmark: list[float], rf: float, p_cagr: float | None, b_cagr: float | None
) -> dict[str, Any]:
    """How the portfolio moved against the benchmark."""
    rp, rb = daily_returns(portfolio), daily_returns(benchmark)
    if len(rp) < 20 or len(rp) != len(rb):
        return {}
    rf_d = (1 + rf) ** (1 / TRADING_DAYS) - 1
    mp, mb = _mean(rp), _mean(rb)
    var_b = _mean([(x - mb) ** 2 for x in rb])
    cov = _mean([(a - mp) * (b - mb) for a, b in zip(rp, rb)])
    sp, sb = math.sqrt(_mean([(x - mp) ** 2 for x in rp])), math.sqrt(var_b)
    beta = cov / var_b if var_b > 1e-14 else None
    corr = cov / (sp * sb) if sp > 1e-12 and sb > 1e-12 else None
    active = [a - b for a, b in zip(rp, rb)]
    te = _stdev(active) * math.sqrt(TRADING_DAYS)
    alpha = (mp - rf_d - beta * (mb - rf_d)) * TRADING_DAYS if beta is not None else None
    up = down = None
    months_p, months_b = monthly_returns(dates, portfolio), monthly_returns(dates, benchmark)
    if len(months_p) == len(months_b):
        ups = [(p["value"], b["value"]) for p, b in zip(months_p, months_b) if b["value"] > 0]
        downs = [(p["value"], b["value"]) for p, b in zip(months_p, months_b) if b["value"] < 0]
        if len(ups) >= 3:
            up = _mean([p for p, _ in ups]) / _mean([b for _, b in ups])
        if len(downs) >= 3:
            down = _mean([p for p, _ in downs]) / _mean([b for _, b in downs])
    return {
        "beta": beta,
        "alpha": alpha,
        "correlation": corr,
        "r_squared": corr * corr if corr is not None else None,
        "tracking_error": te if te > 1e-12 else 0.0,
        "information_ratio": _mean(active) * TRADING_DAYS / te if te > 1e-6 else None,
        "excess_return": (p_cagr - b_cagr) if p_cagr is not None and b_cagr is not None else None,
        "up_capture": up,
        "down_capture": down,
    }


def concentration(weights: list[float]) -> dict[str, Any]:
    ordered = sorted((w for w in weights if w > 0), reverse=True)
    total = sum(ordered)
    if not ordered or total <= 0:
        return {"holdings": 0, "top1": None, "top3": None, "top5": None, "hhi": None, "effective_holdings": None}
    shares = [w / total for w in ordered]
    hhi = sum(s * s for s in shares)
    return {
        "holdings": len(shares),
        "top1": shares[0],
        "top3": sum(shares[:3]),
        "top5": sum(shares[:5]),
        "hhi": hhi,
        "effective_holdings": 1 / hhi,
    }


# -- gathering the data ---------------------------------------------------------


def window_start(range_: str, today: date) -> date:
    if range_ == "ytd":
        return date(today.year - 1, 12, 31)
    return today - timedelta(days=RANGES[range_])


class _Fx:
    """Exchange rates into the base currency, fetched once per currency."""

    def __init__(self, quotes: QuoteService, base: str) -> None:
        self.quotes, self.base = quotes, base
        self.cache: dict[str, Closes | None] = {}
        self.missing: set[str] = set()

    def closes(self, currency: str) -> Closes | None:
        if currency == self.base:
            return None
        if currency not in self.cache:
            try:
                bars = self.quotes.long_closes(f"{currency}{self.base}=X", FETCH_RANGE)
            except QuoteError as exc:
                if exc.kind == "unavailable":
                    raise DataUnavailable(str(exc)) from None
                bars = []
            self.cache[currency] = Closes(bars) if bars else None
            if not bars:
                self.missing.add(currency)
        return self.cache[currency]

    def usable(self, currency: str) -> bool:
        return currency == self.base or self.closes(currency) is not None

    def rate(self, currency: str, day: date) -> float:
        c = self.closes(currency)
        return float(c.on(day)) if c is not None else 1.0


def _closes_for(quotes: QuoteService, wanted: Wanted) -> Closes | None:
    try:
        bars = quotes.daily_closes(wanted, FETCH_RANGE)
    except QuoteError as exc:
        if exc.kind == "unavailable":
            raise DataUnavailable(str(exc)) from None
        bars = []
    return Closes(bars) if bars else None


def _symbol_closes(quotes: QuoteService, symbol: str) -> Closes | None:
    try:
        bars = quotes.long_closes(symbol, FETCH_RANGE)
    except QuoteError as exc:
        if exc.kind == "unavailable":
            raise DataUnavailable(str(exc)) from None
        bars = []
    return Closes(bars) if bars else None


def benchmark_choices() -> list[dict[str, str]]:
    return [{"key": k, "name": v[0]} for k, v in BENCHMARKS.items()] + [{"key": BALANCED[0], "name": BALANCED[1]}]


def _benchmark_series(
    quotes: QuoteService, fx: _Fx, key: str, dates: list[date]
) -> tuple[list[float] | None, dict[str, Any]]:
    """The benchmark's level on each date, in the base currency (carried over days its market was shut)."""
    note: dict[str, Any] = {"key": key, "converted": True}

    def one(k: str) -> tuple[list[float] | None, bool]:
        name, symbol, currency = BENCHMARKS[k]
        closes = _symbol_closes(quotes, symbol)
        if closes is None:
            return None, False
        converted = fx.usable(currency)
        return [float(closes.on(d)) * (fx.rate(currency, d) if converted else 1.0) for d in dates], converted

    if key == BALANCED[0]:
        parts = []
        for k, weight in BALANCED[2]:
            series, converted = one(k)
            if series is None:
                return None, {**note, "name": BALANCED[1], "converted": False}
            parts.append((series, weight, converted))
        index = [100.0]
        for i in range(1, len(dates)):  # rebalanced every day to 60/40
            index.append(index[-1] * (1 + sum(w * (s[i] / s[i - 1] - 1) for s, w, _ in parts)))
        return index, {**note, "name": BALANCED[1], "converted": all(c for _, _, c in parts)}
    name, symbol, currency = BENCHMARKS[key]
    series, converted = one(key)
    return series, {**note, "name": name, "symbol": symbol, "quoted_in": currency, "converted": converted}


def build(
    db: Any,
    quotes: QuoteService,
    *,
    base: str,
    range_: str,
    benchmark: str,
    risk_free: float,
    today: date,
    scope: tuple[str, list[Any]] = ("", []),
) -> dict[str, Any]:
    """Everything the Performance page shows. Raises DataUnavailable if prices cannot be had now."""
    condition, extra = scope
    clause = f" AND {condition}" if condition else ""
    rows = db.query(
        "SELECT h.*, a.name AS account_name FROM holdings h JOIN accounts a ON a.id = h.account_id WHERE a.hidden = 0" + clause,
        extra,
    )
    result: dict[str, Any] = {
        "available": False,
        "range": range_,
        "currency": base,
        "as_of": today.isoformat(),
        "risk_free": risk_free,
        "benchmarks": benchmark_choices(),
        "assumption": "Today's positions, held for the whole period, at each day's closing prices.",
    }
    if quotes.resting():
        raise DataUnavailable("market data is resting after an error")

    fx = _Fx(quotes, base)
    # One entry per security (the same fund in two accounts is one position).
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for h in rows:
        quantity = Decimal(h["quantity"]) if h["quantity"] is not None else Decimal(0)
        price, value = bank_price_and_value(h)
        currency = effective_currency(h)
        kind = (h["type"] or "other").lower()
        key = (h["security_id"], currency)
        p = merged.setdefault(
            key,
            {"ticker": h["ticker"], "name": h["name"], "type": kind, "mic": h["mic"], "currency": currency,
             "quantity": Decimal(0), "value": Decimal(0), "price": price, "accounts": set(), "has_value": False},
        )
        p["quantity"] += quantity
        if value is not None:
            p["value"] += value
            p["has_value"] = True
        p["accounts"].add(h["account_name"])
    positions = list(merged.values())
    if not positions:
        result["message"] = "There are no investment positions yet."
        return result

    # Priced: Yahoo has daily closes (and an exchange rate if it is in another currency).
    for p in positions:
        p["closes"] = None
        p["fx_ok"] = fx.usable(p["currency"])
        if p["type"] not in NON_PRICED_TYPES and p["ticker"] and p["quantity"] > 0 and p["fx_ok"]:
            p["closes"] = _closes_for(quotes, Wanted(p["ticker"], p["mic"], p["currency"]))
        p["now_base"] = None
        if p["has_value"] and p["fx_ok"]:
            p["now_base"] = float(p["value"]) * (fx.rate(p["currency"], today) if p["currency"] != base else 1.0)
        elif p["closes"] is not None:  # no value from the bank: the last close times the quantity
            p["now_base"] = float(p["quantity"]) * float(p["closes"].on(today)) * (fx.rate(p["currency"], today) if p["currency"] != base else 1.0)
    priced = [p for p in positions if p["closes"] is not None]
    if not priced:
        result["message"] = "Yahoo Finance has no price history for these positions yet. Try again later."
        return result

    # Where the history can start: once positions holding most of the money have prices.
    wanted_start = window_start(range_, today)
    starts = sorted(((p["closes"].days[0], p["now_base"] or 0.0) for p in priced), key=lambda t: t[0])
    total_priced = sum(v for _, v in starts) or 1.0
    running, share_start = 0.0, starts[0][0]
    for first, v in starts:
        running += v
        share_start = first
        if running / total_priced >= PRICED_SHARE_FOR_START:
            break
    start = max(wanted_start, share_start)
    all_days = sorted({d for p in priced for d in p["closes"].days if start <= d <= today})
    if len(all_days) < 3:
        result["message"] = "There is not enough price history yet for this period."
        return result
    dates = all_days
    # One closing day before the period, so the first day's move is counted: use the first date as the base.
    values: list[float] = []
    for d in dates:
        total = 0.0
        for p in positions:
            q = float(p["quantity"])
            if p["closes"] is not None:
                total += q * float(p["closes"].on(d)) * fx.rate(p["currency"], d)
            elif p["now_base"] is not None:
                total += p["now_base"]  # not priced: held flat at today's value
        values.append(total)
    if values[0] <= 0:
        result["message"] = "Not enough priced positions to measure."
        return result

    bench_values, bench_note = _benchmark_series(quotes, fx, benchmark if benchmark in BENCHMARKS or benchmark == BALANCED[0] else DEFAULT_BENCHMARK, dates)
    solo = solo_metrics(dates, values, risk_free)
    bench_solo = solo_metrics(dates, bench_values, risk_free) if bench_values else {}
    relative = (
        relative_metrics(dates, values, bench_values, risk_free, solo.get("cagr"), bench_solo.get("cagr")) if bench_values else {}
    )

    # Each position's part in the result, and its own risk.
    total_now = sum((p["now_base"] or 0.0) for p in positions)
    pos_out = []
    for p in positions:
        entry: dict[str, Any] = {
            "ticker": p["ticker"], "name": p["name"], "type": p["type"], "currency": p["currency"],
            "weight": (p["now_base"] / total_now) if p["now_base"] and total_now > 0 else None,
            "value": round(p["now_base"], 2) if p["now_base"] is not None else None, "priced": p["closes"] is not None,
            "return": None, "contribution": None, "volatility": None, "max_drawdown": None,
        }
        if p["closes"] is not None:
            q = float(p["quantity"])
            series = [q * float(p["closes"].on(d)) * fx.rate(p["currency"], d) for d in dates]
            if series[0] > 0:
                entry["return"] = series[-1] / series[0] - 1
                entry["contribution"] = (series[-1] - series[0]) / values[0]
                r = daily_returns(series)
                entry["volatility"] = _stdev(r) * math.sqrt(TRADING_DAYS) if len(r) >= 20 else None
                entry["max_drawdown"] = worst_drawdown(dates, series)["depth"]
        pos_out.append(entry)
    pos_out.sort(key=lambda e: -(e["value"] or 0.0))

    # What the money is in (today's values, in the base currency).
    def split(key_of: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
        sums: dict[str, float] = defaultdict(float)
        for p in positions:
            if p["now_base"]:
                sums[key_of(p)] += p["now_base"]
        return [{"name": k, "value": round(v, 2), "share": v / total_now} for k, v in sorted(sums.items(), key=lambda kv: -kv[1])] if total_now > 0 else []

    type_names = {"equity": "Stocks", "etf": "ETFs", "mutual fund": "Mutual funds", "fixed income": "Bonds", "cash": "Cash", "derivative": "Derivatives", "other": "Other"}
    alloc = {
        "by_type": split(lambda p: type_names.get(p["type"], p["type"].title())),
        "by_currency": split(lambda p: p["currency"]),
        "by_account": split(lambda p: " & ".join(sorted(p["accounts"]))),
    }
    conc = concentration([p["now_base"] or 0.0 for p in positions])
    equities = [(p["now_base"] or 0.0) / total_now for p in positions if p["type"] == "equity"] if total_now > 0 else []
    top_equity = max(equities) if equities else None

    # Income and fees over the last twelve months (kept in each currency: no exchange rate is assumed).
    since = (today - timedelta(days=365)).isoformat()
    ids = {r["account_id"] for r in rows}
    income: dict[str, dict[str, float]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for t in db.query(
            f"SELECT type, subtype, amount, fees, currency FROM investment_transactions WHERE account_id IN ({marks}) AND date >= ?",
            [*ids, since],
        ):
            bucket = income.setdefault(t["currency"], {"dividends": 0.0, "interest": 0.0, "fees": 0.0})
            amount, subtype = float(t["amount"]), (t["subtype"] or "").lower()
            if t["type"] == "cash" and amount < 0:
                if any(w in subtype for w in INCOME_WORDS):
                    bucket["dividends"] += -amount
                elif any(w in subtype for w in INTEREST_WORDS):
                    bucket["interest"] += -amount
            if t["type"] == "fee" and amount > 0:
                bucket["fees"] += amount
            if t["fees"] is not None and t["type"] in ("buy", "sell"):
                bucket["fees"] += float(t["fees"])
    values_by_currency: dict[str, float] = defaultdict(float)
    for p in positions:
        if p["has_value"]:
            values_by_currency[p["currency"]] += float(p["value"])
    for cur, b in income.items():
        v = values_by_currency.get(cur, 0.0)
        b["dividend_yield"] = b["dividends"] / v if v > 0 else None
        b["fee_share"] = b["fees"] / v if v > 0 else None
        for field in ("dividends", "interest", "fees"):
            b[field] = round(b[field], 2)

    priced_value = sum((p["now_base"] or 0.0) for p in priced)
    coverage = {
        "positions": len(positions),
        "priced_positions": len(priced),
        "priced_share": priced_value / total_now if total_now > 0 else None,
        "unpriced": [p["ticker"] or p["name"] for p in positions if p["closes"] is None and p["type"] not in NON_PRICED_TYPES],
        "missing_rates": sorted(fx.missing),
        "started_later": start > wanted_start,
        "start": dates[0].isoformat(),
        "requested_start": wanted_start.isoformat(),
    }
    result.update(
        {
            "available": True,
            "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(),
            "benchmark": bench_note,
            "dates": [d.isoformat() for d in dates],
            "portfolio": [round(v / values[0] * 100, 4) for v in values],
            "benchmark_index": [round(v / bench_values[0] * 100, 4) for v in bench_values] if bench_values else None,
            "portfolio_drawdown": [round(x, 5) for x in drawdowns(values)],
            "benchmark_drawdown": [round(x, 5) for x in drawdowns(bench_values)] if bench_values else None,
            "metrics": solo,
            "benchmark_metrics": bench_solo,
            "relative": relative,
            "monthly": _paired_months(dates, values, bench_values),
            "positions": pos_out,
            "allocation": alloc,
            "concentration": conc,
            "income": income,
            "coverage": coverage,
        }
    )
    result["guidance"] = guidance(result, top_equity, total_now)
    return result


def _paired_months(dates: list[date], values: list[float], bench: list[float] | None) -> list[dict[str, Any]]:
    mp = monthly_returns(dates, values)
    mb = monthly_returns(dates, bench) if bench else []
    out = []
    for i, m in enumerate(mp):
        out.append(
            {
                "month": m["month"],
                "portfolio": round(m["value"], 5),
                "benchmark": round(mb[i]["value"], 5) if i < len(mb) else None,
                "partial": i == 0 or i == len(mp) - 1,
            }
        )
    return out


# -- plain-language notes -------------------------------------------------------


def _pct(x: float, digits: int = 0) -> str:
    return f"{x * 100:.{digits}f}%"


def guidance(r: dict[str, Any], top_equity_share: float | None, total_now: float) -> list[dict[str, str]]:
    """Rule-of-thumb observations, each with the reason. They are prompts to look closer, not advice."""
    notes: list[dict[str, str]] = []
    m, b, rel, conc = r["metrics"], r["benchmark_metrics"], r["relative"], r["concentration"]
    name = r["benchmark"].get("name", "the benchmark")
    base = r["currency"]

    def add(level: str, title: str, text: str) -> None:
        notes.append({"level": level, "title": title, "text": text})

    cov = r["coverage"]
    if cov["priced_share"] is not None and cov["priced_share"] < 0.9:
        add("watch", "Some positions are not in these numbers",
            f"Only {_pct(cov['priced_share'])} of the money has a price history, so the figures describe that part. "
            f"Not priced: {', '.join(cov['unpriced'][:6]) or 'nothing listed'}.")
    if top_equity_share is not None and top_equity_share >= 0.10:
        add("watch", "One company is a big part of the money",
            f"A single company's shares are {_pct(top_equity_share)} of your investments. A common rule of thumb is to keep any one "
            "company well under 10%, because one bad year for it can move the whole total. A broad fund spreads that risk.")
    if conc["effective_holdings"] is not None and conc["holdings"] >= 3 and conc["effective_holdings"] < 3:
        add("info", "Most of the money is in very few positions",
            f"Your {conc['holdings']} positions behave like {conc['effective_holdings']:.1f} equal-sized ones. That is fine if the largest is "
            "itself a broad fund, but it means one choice drives the result.")
    foreign = [a for a in r["allocation"]["by_currency"] if a["name"] != base and a["share"] >= 0.5]
    if foreign:
        add("info", "Exchange rates matter here",
            f"{_pct(foreign[0]['share'])} of the money is in {foreign[0]['name']}. A move in the exchange rate changes your {base} value "
            "even if prices do not move.")
    cash = next((a for a in r["allocation"]["by_type"] if a["name"] == "Cash"), None)
    if cash and cash["share"] >= 0.2:
        add("info", "A lot of the money is cash", f"Cash is {_pct(cash['share'])} of these accounts. It is steady but does not grow with markets; make sure that is on purpose.")
    for cur, inc in r["income"].items():
        if inc.get("fee_share") and inc["fee_share"] >= 0.005:
            add("watch", "Fees are worth a look",
                f"Trading fees over the last 12 months were {_pct(inc['fee_share'], 2)} of the {cur} value. Fees come off the top every year, "
                "so even small percentages add up over decades.")
    if m.get("max_drawdown") is not None and b.get("max_drawdown") is not None and m.get("total_return") is not None:
        if m["max_drawdown"] < b["max_drawdown"] * 1.2 and m["max_drawdown"] < -0.1 and m["total_return"] < b["total_return"]:
            add("watch", "More risk for less return",
                f"Over this period the largest fall was {_pct(m['max_drawdown'], 1)} against {_pct(b['max_drawdown'], 1)} for {name}, "
                f"and the return was lower. That is the pattern to avoid: take on more swings without being paid for them.")
    if m.get("sharpe") is not None and b.get("sharpe") is not None:
        if m["sharpe"] >= b["sharpe"] + 0.1:
            add("good", "Good return for the risk taken",
                f"Return per unit of risk (Sharpe {m['sharpe']:.2f}) is above {name} ({b['sharpe']:.2f}) for this period. "
                "A short period can flatter any portfolio, so look at one year and longer.")
        elif m["sharpe"] <= b["sharpe"] - 0.1:
            add("info", "Return per unit of risk trails the benchmark",
                f"Sharpe {m['sharpe']:.2f} against {b['sharpe']:.2f} for {name}. A low-cost index fund covering the benchmark is the "
                "yardstick to beat, after fees and tax.")
    if rel.get("beta") is not None:
        if rel["beta"] >= 1.2:
            add("info", "Swings bigger than the benchmark",
                f"Beta {rel['beta']:.2f}: when {name} moves 1%, your positions have tended to move about {rel['beta']:.1f}%. Expect deeper falls in a downturn.")
        elif 0 <= rel["beta"] <= 0.8:
            add("info", "Calmer than the benchmark",
                f"Beta {rel['beta']:.2f}: your positions have moved less than {name}. Calmer rides usually also mean smaller gains in good years.")
    if rel.get("down_capture") is not None and rel.get("up_capture") is not None:
        if rel["up_capture"] > rel["down_capture"] + 0.1:
            add("good", "You keep more of the ups than the downs",
                f"Up capture {_pct(rel['up_capture'])} against down capture {_pct(rel['down_capture'])}, month by month versus {name}.")
        elif rel["up_capture"] + 0.1 < rel["down_capture"]:
            add("watch", "You take more of the downs than the ups",
                f"Up capture {_pct(rel['up_capture'])} against down capture {_pct(rel['down_capture'])} versus {name}.")
    dd = m.get("drawdown") or {}
    if dd.get("current") is not None and dd["current"] <= -0.1:
        add("info", "Below the recent high",
            f"The positions are {_pct(-dd['current'], 1)} below their highest value in this period. Falls like this happen in most years; "
            "the useful question is whether you could leave the money alone through another one.")
    if not notes:
        add("info", "Nothing stands out", "None of the usual warning signs show up in this period. Check again after a few months, and look at the longer periods too.")
    return notes


# -- worked out ahead of time -------------------------------------------------------

RESULT_TTL = 30 * 60.0  # a finished page is reused this long; the warmer renews it before it runs out
WARM_EVERY = 20 * 60.0  # keeps each symbol's history fresh (quotes.py keeps them 6-12 hours) and the pages ready
WARM_FIRST_AFTER = 30.0  # after start-up, once the server has settled


class PerformanceService:
    """The Performance page's answers, kept ready.

    The slow part of a first request is fetching each symbol's history from Yahoo (one request a second), not the
    arithmetic. So a background thread works out the household's pages for every period and benchmark on a timer,
    and a request is then a lookup. Anything not ready (another person, an unusual risk-free rate) is worked out on
    request and kept for a while. A page is also worked out again when the holdings change."""

    def __init__(self, db: Any, get_quotes: Callable[[], QuoteService | None], base: str, get_today: Callable[[], date]) -> None:
        self.db, self.get_quotes, self.base, self.get_today = db, get_quotes, base, get_today
        self._results: dict[tuple, tuple[float, str, dict[str, Any]]] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _fingerprint(self) -> str:
        row = self.db.query(
            "SELECT COUNT(*) AS n, COALESCE(MAX(h.updated_at), '') AS u, "
            "COALESCE(SUM(CAST(h.quantity AS REAL)), 0) AS q, COALESCE(SUM(CAST(h.value AS REAL)), 0) AS v, "
            "(SELECT COALESCE(group_concat(id), '') FROM accounts WHERE hidden = 1) AS hidden, "
            "(SELECT COALESCE(group_concat(account_id || ':' || person_id), '') FROM account_owners) AS owners FROM holdings h"
        )[0]
        return f"{row['n']}|{row['u']}|{row['q']}|{row['v']}|{row['hidden']}|{row['owners']}|{self.get_today().isoformat()}"

    def get(
        self, *, range_: str, benchmark: str, risk_free: float, person_id: int | None, scope: tuple[str, list[Any]], refresh: bool = False
    ) -> dict[str, Any]:
        """The page, from the ready answers when they are current (`refresh` works it out again regardless, which
        also renews each symbol's history once that has aged). Raises DataUnavailable like `build`."""
        quotes = self.get_quotes()
        key = (range_, benchmark, round(risk_free, 4), person_id)  # 0.01 of a percent is as fine as the page asks
        stamp = self._fingerprint()
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # a request waits for the same page being worked out, rather than fetching everything twice
            held = self._results.get(key)
            if not refresh and held is not None and held[1] == stamp and time.monotonic() - held[0] < RESULT_TTL:
                return held[2]
            result = build(
                self.db, quotes, base=self.base, range_=range_, benchmark=benchmark, risk_free=risk_free,
                today=self.get_today(), scope=scope,
            )
            if result.get("available"):
                self._results[key] = (time.monotonic(), stamp, result)
                self._forget_old()
            return result

    def _forget_old(self) -> None:
        """Pages worked out on request (another person, an unusual rate) are dropped once they have expired, so the
        kept pages cannot grow without limit. The household's warmed pages are always replaced, never dropped."""
        now = time.monotonic()
        with self._guard:
            for key, (made, _, _) in list(self._results.items()):
                if now - made >= RESULT_TTL * 2 and (key[3] is not None or key[2] != round(DEFAULT_RISK_FREE, 4)):
                    del self._results[key]
                    self._locks.pop(key, None)

    def warm_once(self) -> int:
        """Work out the household's pages for every period and benchmark. Returns how many were made.
        Stops early when prices are not to be had (the next round tries again)."""
        if self.get_quotes() is None:
            return 0
        made = 0
        keys = [(r, b) for b in [DEFAULT_BENCHMARK, *[k for k in (*BENCHMARKS, BALANCED[0]) if k != DEFAULT_BENCHMARK]] for r in (*RANGES, "ytd")]  # year to date is worked out from the date, so it is not in RANGES
        keys = sorted(keys, key=lambda rb: (rb[1] != DEFAULT_BENCHMARK, rb[0] != "1y"))  # the page's own first view first
        for range_, benchmark in keys:
            if self._stop.is_set():
                break
            try:
                self.get(range_=range_, benchmark=benchmark, risk_free=DEFAULT_RISK_FREE, person_id=None, scope=("", []), refresh=True)
                made += 1
            except DataUnavailable:
                break
            except Exception:
                log.exception("Could not get the %s performance page ready", range_)
        return made

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="performance", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        self._stop.wait(WARM_FIRST_AFTER)
        while not self._stop.is_set():
            try:
                self.warm_once()
            except Exception:
                log.exception("Warming the performance pages failed")
            self._stop.wait(WARM_EVERY)
