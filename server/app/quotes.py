"""Near-real-time prices for investment positions, from Yahoo Finance.

Plaid only reports end-of-day prices, so live values come from Yahoo's public chart
endpoint. It is unofficial: it can change, throttle or disappear without notice, so
every failure degrades to Plaid's last price and is reported, never raised to the
caller. Only ticker symbols leave the NAS (never balances, quantities or accounts).
One cheap call per symbol every minute gives the current price and today's open; one
call per symbol per hour gives a year of daily closes for the week, month, year-to-date
and one-year changes.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Protocol

import httpx

try:  # Yahoo throttles plain Python connections; this one looks like Chrome's. Optional: httpx is the fallback.
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.exceptions import RequestException as CurlError
except Exception:  # not installed, or its native part will not load here
    curl_requests = None

    class CurlError(Exception):  # never raised; keeps the except clause below valid
        pass

log = logging.getLogger("homefinance.quotes")

YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")  # throttled separately: try the other when one answers 429
YAHOO_CHART = "https://{host}/v8/finance/chart/{symbol}"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

QUOTE_TTL = 120.0  # while the market is moving (Yahoo is up to 15 minutes behind anyway)
CLOSED_QUOTE_TTL = 600.0  # the last price is older than FRESH_SECONDS: nothing is changing
HISTORY_TTL = 6 * 3600.0  # daily closes only change once a day
HISTORY_PER_LOOKUP = 3  # new price histories fetched per look, so a first load trickles in instead of bursting
UNKNOWN_TTL = 3600.0  # remember that a ticker was not found, so it is not retried every minute
COOLDOWN = 300.0  # after Yahoo throttles or fails, leave it alone for this long
MAX_COOLDOWN = 1800.0  # repeated failures double the rest, up to this
REQUEST_SPACING = 1.0  # seconds between any two requests to Yahoo: bursts are what get throttled
FRESH_SECONDS = 30 * 60
DEADLINE = 12.0  # one request never waits longer than this for all symbols together
MAX_WORKERS = 4

LIVE_TYPES = {"equity", "etf", "mutual fund"}
CANADIAN_SUFFIX = {"XTSE": ".TO", "XTSX": ".V", "NEOE": ".NE", "XCNQ": ".CN"}  # by Plaid's market code
CANADIAN_ORDER = (".TO", ".NE", ".V", ".CN")
PERIODS = ("day", "open", "1w", "1m", "3m", "ytd", "1y")


class QuoteError(Exception):
    """`kind` is "unknown" (no such symbol) or "unavailable" (throttled, down, unreadable)."""

    def __init__(self, kind: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after  # seconds Yahoo asked us to wait, if it said


@dataclass(frozen=True)
class Quote:
    symbol: str
    currency: str | None
    price: Decimal
    time: int  # epoch seconds of the last trade
    day: date  # the trading date in the exchange's own time zone
    open: Decimal | None  # today's open, once the market has opened
    previous_close: Decimal | None


Bars = list[tuple[date, Decimal]]  # daily closes, oldest first


class Provider(Protocol):
    def quote(self, symbol: str) -> Quote: ...

    def history(self, symbol: str) -> Bars: ...


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _day(epoch: float, gmt_offset: int) -> date:
    return (datetime.fromtimestamp(epoch, UTC) + timedelta(seconds=gmt_offset)).date()


class YahooProvider:
    def __init__(self, client: Any | None = None, spacing: float = REQUEST_SPACING) -> None:
        self._browser = client is None and curl_requests is not None
        if client is not None:
            self._client = client
        elif curl_requests is not None:
            self._client = curl_requests.Session(impersonate="chrome", timeout=8.0)
        else:
            self._client = httpx.Client(timeout=8.0)
        self._spacing = spacing
        self._pace_lock = threading.Lock()
        self._io_lock = threading.Lock()  # one request at a time: the shared connection is not for parallel use
        self._next_slot = 0.0
        self._host = 0  # index into YAHOO_HOSTS of the one that last worked
        self.connection = "browser-style connection" if self._browser else "plain connection"

    def _pace(self) -> None:
        """Hand out request slots at least `spacing` seconds apart, whichever thread asks."""
        if self._spacing <= 0:
            return
        with self._pace_lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._spacing
        if slot > now:
            time.sleep(slot - now)

    def _get(self, symbol: str, range_: str) -> tuple[Any, str]:
        """Ask one host, and on a 429 ask the other once; remember the one that answered. Returns the response and,
        when both refused, what they said (kept per call, so threads cannot swap each other's text)."""
        response: Any = None
        refused: list[str] = []
        for step in range(len(YAHOO_HOSTS)):
            index = (self._host + step) % len(YAHOO_HOSTS)
            self._pace()
            # A Chrome-like connection brings its own matching headers; a plain one needs a browser's User-Agent.
            headers = {"Accept": "application/json"} if self._browser else {"User-Agent": USER_AGENT, "Accept": "application/json"}
            try:
                with self._io_lock:
                    response = self._client.get(
                        YAHOO_CHART.format(host=YAHOO_HOSTS[index], symbol=symbol),
                        params={"range": range_, "interval": "1d"},
                        headers=headers,
                    )
            except (httpx.HTTPError, CurlError) as exc:
                raise QuoteError("unavailable", f"could not reach Yahoo Finance ({type(exc).__name__})") from None
            if response.status_code != 429:
                self._host = index
                return response, ""
            refused.append(YAHOO_HOSTS[index].split(".")[0])
        reply = "".join(ch for ch in str(getattr(response, "text", ""))[:80] if ch.isprintable()).strip()
        said = f"; Yahoo said: {reply!r}" if reply else ""
        return response, f" on {' and '.join(refused)} ({self.connection}){said}"

    def _chart(self, symbol: str, range_: str) -> dict[str, Any]:
        response, refusal = self._get(symbol, range_)
        if response.status_code == 404:
            raise QuoteError("unknown", "symbol not found")
        if response.status_code == 429:
            try:
                asked = float(response.headers.get("Retry-After", ""))
            except ValueError:
                asked = float("nan")
            wait = min(asked, MAX_COOLDOWN) if math.isfinite(asked) and asked > 0 else None  # "nan", "-5" and dates are ignored
            raise QuoteError("unavailable", f"Yahoo Finance answered 429 (too many requests){refusal}", retry_after=wait)
        if response.status_code != 200:
            raise QuoteError("unavailable", f"Yahoo Finance answered {response.status_code}")
        try:
            body = response.json()["chart"]
        except (ValueError, KeyError, TypeError):
            raise QuoteError("unavailable", "unreadable answer from Yahoo Finance") from None
        error = body.get("error") if isinstance(body, dict) else None
        if error:
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            raise QuoteError("unknown" if "not found" in code.lower() else "unavailable", code or "error")
        result = (body.get("result") or [None])[0] if isinstance(body, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("meta"), dict):
            raise QuoteError("unavailable", "unexpected answer from Yahoo Finance")
        return result

    def quote(self, symbol: str) -> Quote:
        result = self._chart(symbol, "1d")
        meta = result["meta"]
        price, stamp = _dec(meta.get("regularMarketPrice")), meta.get("regularMarketTime")
        if price is None or price <= 0 or not isinstance(stamp, (int, float)):
            raise QuoteError("unknown", "no price for this symbol")
        offset = int(meta.get("gmtoffset") or 0)
        day = _day(stamp, offset)
        opens = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("open") or []
        stamps = result.get("timestamp") or []
        today_open = None
        if stamps and opens and isinstance(stamps[-1], (int, float)) and _day(stamps[-1], offset) == day:
            today_open = _dec(opens[-1])
        return Quote(
            symbol=symbol,
            currency=meta.get("currency"),
            price=price,
            time=int(stamp),
            day=day,
            open=today_open,
            previous_close=_dec(meta.get("chartPreviousClose")) or _dec(meta.get("previousClose")),
        )

    def history(self, symbol: str) -> Bars:
        result = self._chart(symbol, "1y")
        offset = int(result["meta"].get("gmtoffset") or 0)
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        bars: Bars = []
        for stamp, close in zip(result.get("timestamp") or [], closes):
            value = _dec(close)
            if isinstance(stamp, (int, float)) and value is not None and value > 0:
                bars.append((_day(stamp, offset), value))
        return bars


# -- which symbol is this security? -----------------------------------------------


def candidates(ticker: str, mic: str | None, currency: str | None) -> list[str]:
    """Yahoo symbols to try for a Plaid security, best first."""
    base = ticker.strip().upper().replace(" ", "")
    if not base:
        return []
    if any(base.endswith(s) for s in CANADIAN_ORDER):
        return [base]
    base = base.replace(".", "-")  # BRK.B and BAM.A are BRK-B and BAM-A on Yahoo
    if mic and mic.upper() in CANADIAN_SUFFIX:
        return [base + CANADIAN_SUFFIX[mic.upper()]]
    if mic:
        return [base]  # a known non-Canadian market
    if (currency or "").upper() == "CAD":
        return [base + s for s in CANADIAN_ORDER]
    return [base]


# -- the numbers ---------------------------------------------------------------


def _shift_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, d)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def reference_prices(quote: Quote, bars: Bars) -> dict[str, Decimal | None]:
    """The price each period is measured from. A change is price now minus this."""
    earlier = [(d, c) for d, c in bars if d < quote.day]  # today's partial bar is not a reference

    def on_or_before(target: date, slack_days: int = 0) -> Decimal | None:
        best = None
        for d, c in earlier:
            if d <= target:
                best = c
            else:
                break
        if best is None and earlier and (earlier[0][0] - target).days <= slack_days:
            return earlier[0][1]  # the history starts a few days after the target
        return best

    return {
        "day": quote.previous_close or (earlier[-1][1] if earlier else None),
        "open": quote.open,
        "1w": on_or_before(quote.day - timedelta(days=7)),
        "1m": on_or_before(_shift_months(quote.day, -1)),
        "3m": on_or_before(_shift_months(quote.day, -3)),
        "ytd": on_or_before(date(quote.day.year - 1, 12, 31)),
        "1y": on_or_before(_shift_months(quote.day, -12), slack_days=7),
    }


# -- cache, throttling, fallbacks ------------------------------------------------


@dataclass(frozen=True)
class Wanted:
    ticker: str
    mic: str | None
    currency: str


@dataclass(frozen=True)
class Live:
    symbol: str
    quote: Quote
    bars: Bars  # may be empty if only the history call failed


@dataclass
class Lookup:
    live: dict[Wanted, Live]
    unavailable: str | None  # why Yahoo could not be used, if it could not
    stale: bool  # some prices come from the cache because a refresh failed
    why: dict[Wanted, str] = field(default_factory=dict)  # why a position has no live price


class QuoteService:
    def __init__(self, provider: Provider, clock: Callable[[], float] = time.time) -> None:
        self.provider = provider
        self.clock = clock
        self._lock = threading.Lock()
        self._symbols: dict[Wanted, tuple[float, str | None]] = {}
        self._why: dict[Wanted, str] = {}
        self._quotes: dict[str, tuple[float, Quote]] = {}
        self._bars: dict[str, tuple[float, Bars]] = {}
        self._cooldown_until = 0.0
        self._failures = 0  # in a row: each one doubles the rest, a success resets it
        self._last_error: str | None = None

    def _quote_fresh(self, symbol: str, now: float) -> bool:
        cached = self._quotes.get(symbol)
        if cached is None:
            return False
        age_of_price = now - cached[1].time
        ttl = QUOTE_TTL if age_of_price <= FRESH_SECONDS else CLOSED_QUOTE_TTL
        return now - cached[0] < ttl

    def _take_history_slot(self, slots: list[int]) -> bool:
        with self._lock:
            if slots[0] <= 0:
                return False
            slots[0] -= 1
            return True

    def _refresh(self, wanted: Wanted, slots: list[int]) -> None:
        now = self.clock()
        with self._lock:
            known = self._symbols.get(wanted)
        if known is not None and known[1] is None and now - known[0] < UNKNOWN_TTL:
            return  # looked for it recently and it does not exist
        symbol = known[1] if known is not None else None  # a found symbol never changes
        if symbol is None:
            reasons = []
            for candidate in candidates(wanted.ticker, wanted.mic, wanted.currency):
                try:
                    quote = self.provider.quote(candidate)
                except QuoteError as exc:
                    if exc.kind == "unavailable":
                        raise
                    reasons.append(f"Yahoo has no {candidate}")
                    continue
                if not quote.currency or quote.currency.upper() != wanted.currency.upper():
                    # another currency, or none stated so it cannot be checked: no FX guessing
                    reasons.append(f"{candidate} is priced in {quote.currency or 'an unknown currency'} on Yahoo, but your bank holds it in {wanted.currency}")
                    continue
                symbol = candidate
                with self._lock:
                    self._quotes[symbol] = (now, quote)
                break
            with self._lock:
                self._symbols[wanted] = (now, symbol)
                if symbol is None:
                    self._why[wanted] = "; ".join(reasons) or "no symbol to look up"
                else:
                    self._why.pop(wanted, None)
            if symbol is None:
                return
        with self._lock:
            quote_ok = self._quote_fresh(symbol, now)
            bars_cached = self._bars.get(symbol)
        if not quote_ok:
            quote = self.provider.quote(symbol)
            with self._lock:
                self._quotes[symbol] = (now, quote)
        if (bars_cached is None or now - bars_cached[0] >= HISTORY_TTL) and self._take_history_slot(slots):
            try:
                bars = self.provider.history(symbol)
            except QuoteError as exc:
                if exc.kind == "unavailable" and bars_cached is None:
                    raise
                return  # keep the older history; the live price is what matters
            with self._lock:
                self._bars[symbol] = (now, bars)

    def lookup(self, wanted: Iterable[Wanted]) -> Lookup:
        unique = list(dict.fromkeys(wanted))
        now = self.clock()
        problem: str | None = None
        if unique and now < self._cooldown_until:
            problem = self._last_error or "Yahoo Finance is being given a rest after an error"
        elif unique:
            pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
            slots = [HISTORY_PER_LOOKUP]  # shared by this look's workers
            futures = {pool.submit(self._refresh, w, slots): w for w in unique}
            done, not_done = wait(futures, timeout=DEADLINE)
            pool.shutdown(wait=False, cancel_futures=True)
            asked = 0.0
            for future in done:
                exc = future.exception()
                if isinstance(exc, QuoteError):
                    problem = str(exc)
                    asked = max(asked, exc.retry_after or 0.0)
                elif exc is not None:
                    log.warning("quote refresh failed: %s", type(exc).__name__)
                    problem = "unexpected error while fetching prices"
            # Slow but moving is fine: what was fetched is kept and the next look carries on.
            if not_done and problem is None and not done:
                problem = "Yahoo Finance was too slow"
            with self._lock:  # two requests can be here at once
                if problem:
                    self._failures += 1
                    rest = min(max(COOLDOWN * 2 ** (self._failures - 1), asked), MAX_COOLDOWN)
                    self._last_error = problem
                    self._cooldown_until = self.clock() + rest
                    log.warning("live prices unavailable: %s (resting %d s)", problem, rest)
                else:
                    self._failures = 0
        live: dict[Wanted, Live] = {}
        why: dict[Wanted, str] = {}
        stale = False
        with self._lock:
            for w in unique:
                if w in self._why:
                    why[w] = self._why[w]
                symbol = (self._symbols.get(w) or (0.0, None))[1]
                cached = self._quotes.get(symbol) if symbol else None
                if symbol is None or cached is None:
                    continue
                if not self._quote_fresh(symbol, self.clock()):
                    stale = True
                live[w] = Live(symbol, cached[1], (self._bars.get(symbol) or (0.0, []))[1])
        return Lookup(live=live, unavailable=problem, stale=stale, why=why)
