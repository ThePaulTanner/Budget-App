"""Live prices: the Yahoo parser (against a mocked HTTP layer), symbol guessing, caching,
failure handling, the change maths, and the /api/holdings/live endpoint.

The Yahoo response shape here is written from Yahoo's public chart format. It has NOT been
checked against the live service from the build environment (no access): see the README."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app import quotes as q
from app.quotes import (
    Provider,
    Quote,
    QuoteError,
    QuoteService,
    Wanted,
    YahooProvider,
    candidates,
    reference_prices,
)

from .conftest import assert_matches, fixture_float

GMT = -4 * 3600  # Toronto in October
QUOTE_DAY = date(2026, 10, 2)
QUOTE_TIME = int(datetime(2026, 10, 2, 19, 58, tzinfo=UTC).timestamp())  # 15:58 in Toronto
NOW = float(QUOTE_TIME + 120)


def stamp(day: date) -> int:
    """The session start (9:30 Toronto) for a date, as Yahoo reports it."""
    return int(datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC).timestamp())


def history_bars() -> list[tuple[date, Decimal]]:
    """Weekdays from a year back to the day before the quote, closing 40.00 + 0.02 a day."""
    bars, d = [], date(2025, 10, 2)
    while d < QUOTE_DAY:
        if d.weekday() < 5:
            bars.append((d, Decimal("40.00") + Decimal("0.02") * (d - date(2025, 10, 2)).days))
        d += timedelta(days=1)
    return bars


def live_quote(symbol="VEQT.TO", price="47.85", currency="CAD") -> Quote:
    return Quote(symbol, currency, Decimal(price), QUOTE_TIME, QUOTE_DAY, Decimal("47.50"), Decimal("47.40"))


class FakeProvider:
    def __init__(self, known=("VEQT.TO",), price="47.85"):
        self.known, self.price = set(known), price
        self.quote_calls: list[str] = []
        self.history_calls: list[str] = []
        self.fail: QuoteError | None = None
        self.currency = "CAD"

    def quote(self, symbol):
        self.quote_calls.append(symbol)
        if self.fail is not None:
            raise self.fail
        if symbol not in self.known:
            raise QuoteError("unknown", "symbol not found")
        return live_quote(symbol, self.price, self.currency)

    def history(self, symbol):
        self.history_calls.append(symbol)
        if self.fail is not None:
            raise self.fail
        return history_bars()


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def wanted(ticker="VEQT", mic=None, currency="CAD"):
    return Wanted(ticker, mic, currency)


# -- the Yahoo parser ---------------------------------------------------------


def chart_json(range_, price=47.85, currency="CAD", with_open=True):
    day = stamp(QUOTE_DAY)
    meta = {
        "currency": currency, "symbol": "VEQT.TO", "gmtoffset": GMT, "regularMarketPrice": price,
        "regularMarketTime": QUOTE_TIME, "chartPreviousClose": 47.4,
    }
    if range_ == "1d":
        quote = {"open": [47.5 if with_open else None], "close": [price]}
        return {"chart": {"result": [{"meta": meta, "timestamp": [day], "indicators": {"quote": [quote]}}], "error": None}}
    bars = history_bars()
    quote = {"close": [float(c) for _, c in bars] + [None]}
    stamps = [stamp(d) for d, _ in bars] + [stamp(date(2026, 9, 30))]
    return {"chart": {"result": [{"meta": meta, "timestamp": stamps, "indicators": {"quote": [quote]}}], "error": None}}


def mocked(handler):
    return YahooProvider(httpx.Client(transport=httpx.MockTransport(handler)), spacing=0)


def test_parses_a_quote_exactly_with_todays_open():
    seen = {}

    def handler(request):
        seen["agent"] = request.headers["user-agent"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json=chart_json("1d", price=132.47))

    quote = mocked(handler).quote("VEQT.TO")
    assert quote.price == Decimal("132.47")  # not 132.47000000000003
    assert (quote.currency, quote.day, quote.open, quote.previous_close) == ("CAD", QUOTE_DAY, Decimal("47.5"), Decimal("47.4"))
    assert "/VEQT.TO" in seen["url"] and "range=1d" in seen["url"] and "Mozilla" in seen["agent"]


def test_open_is_missing_until_the_market_has_opened():
    assert mocked(lambda r: httpx.Response(200, json=chart_json("1d", with_open=False))).quote("X").open is None


def test_open_is_not_taken_from_an_earlier_days_bar():
    body = chart_json("1d")
    body["chart"]["result"][0]["timestamp"] = [stamp(date(2026, 10, 1))]  # yesterday's bar
    assert mocked(lambda r: httpx.Response(200, json=body)).quote("X").open is None


def test_parses_history_and_skips_empty_bars():
    bars = mocked(lambda r: httpx.Response(200, json=chart_json("1y"))).history("VEQT.TO")
    assert bars[0][0] == date(2025, 10, 2) and bars[-1][0] < QUOTE_DAY
    assert all(isinstance(c, Decimal) for _, c in bars) and len(bars) == len(history_bars())


@pytest.mark.parametrize(
    "response, kind",
    [
        (httpx.Response(404, json={}), "unknown"),
        (httpx.Response(200, json={"chart": {"result": None, "error": {"code": "Not Found", "description": "x"}}}), "unknown"),
        (httpx.Response(429, text="Too Many Requests"), "unavailable"),
        (httpx.Response(503), "unavailable"),
        (httpx.Response(200, text="<html>blocked</html>"), "unavailable"),
        (httpx.Response(200, json={"chart": {"result": [{"meta": {}}], "error": None}}), "unknown"),
        (httpx.Response(200, json={"unexpected": True}), "unavailable"),
    ],
)
def test_bad_answers_become_quote_errors(response, kind):
    with pytest.raises(QuoteError) as exc:
        mocked(lambda r: response).quote("X")
    assert exc.value.kind == kind


def test_network_failure_is_unavailable():
    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(QuoteError) as exc:
        mocked(boom).quote("X")
    assert exc.value.kind == "unavailable" and "ConnectError" in str(exc.value)


# -- which symbol --------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker, mic, currency, expected",
    [
        ("VEQT", "XTSE", "CAD", ["VEQT.TO"]),
        ("XYZ", "XTSX", "CAD", ["XYZ.V"]),
        ("ABC", "NEOE", "CAD", ["ABC.NE"]),
        ("AAPL", "XNAS", "USD", ["AAPL"]),
        ("AAPL", "XNAS", "CAD", ["AAPL"]),  # a US listing held in a CAD account stays US
        ("VEQT", None, "CAD", ["VEQT.TO", "VEQT.NE", "VEQT.V", "VEQT.CN"]),
        ("VOO", None, "USD", ["VOO"]),
        ("BRK.B", "XNYS", "USD", ["BRK-B"]),
        ("BAM.A", "XTSE", "CAD", ["BAM-A.TO"]),
        ("RY.TO", None, "CAD", ["RY.TO"]),
        ("  ", None, "CAD", []),
    ],
)
def test_symbol_candidates(ticker, mic, currency, expected):
    assert candidates(ticker, mic, currency) == expected


# -- the service: caching, fallbacks ---------------------------------------------


def test_finds_the_right_canadian_listing_and_remembers_it():
    provider = FakeProvider(known={"VEQT.NE"})
    service = QuoteService(provider, Clock())
    result = service.lookup([wanted()])
    assert result.live[wanted()].symbol == "VEQT.NE" and result.unavailable is None
    assert provider.quote_calls[:2] == ["VEQT.TO", "VEQT.NE"]
    calls = len(provider.quote_calls)
    service.lookup([wanted()])
    assert len(provider.quote_calls) == calls  # cached, and the symbol is not searched for again


def test_unknown_ticker_is_not_retried_every_minute():
    provider = FakeProvider(known=())
    clock = Clock()
    service = QuoteService(provider, clock)
    assert service.lookup([wanted("NOPE")]).live == {}
    first = len(provider.quote_calls)
    clock.now += 600
    service.lookup([wanted("NOPE")])
    assert len(provider.quote_calls) == first
    clock.now += q.UNKNOWN_TTL
    service.lookup([wanted("NOPE")])
    assert len(provider.quote_calls) > first


def test_prices_are_cached_for_a_minute_and_history_for_an_hour():
    provider = FakeProvider()
    clock = Clock()
    service = QuoteService(provider, clock)
    service.lookup([wanted()])
    service.lookup([wanted()])
    assert provider.quote_calls.count("VEQT.TO") == 1 and provider.history_calls == ["VEQT.TO"]
    clock.now += q.QUOTE_TTL + 1
    service.lookup([wanted()])
    assert provider.quote_calls.count("VEQT.TO") == 2 and len(provider.history_calls) == 1
    clock.now += q.HISTORY_TTL
    service.lookup([wanted()])
    assert len(provider.history_calls) == 2


def test_a_closed_market_is_refreshed_far_less_often():
    provider = FakeProvider()
    clock = Clock(QUOTE_TIME + q.FRESH_SECONDS + 3600)  # last trade is over an hour old
    service = QuoteService(provider, clock)
    service.lookup([wanted()])
    clock.now += 120
    service.lookup([wanted()])
    assert provider.quote_calls.count("VEQT.TO") == 1
    clock.now += q.CLOSED_QUOTE_TTL
    service.lookup([wanted()])
    assert provider.quote_calls.count("VEQT.TO") == 2


def test_a_throttled_yahoo_is_left_alone_and_the_cache_still_answers():
    provider = FakeProvider()
    clock = Clock()
    service = QuoteService(provider, clock)
    service.lookup([wanted()])
    clock.now += q.QUOTE_TTL + 1
    provider.fail = QuoteError("unavailable", "Yahoo Finance answered 429")
    result = service.lookup([wanted()])
    assert result.unavailable == "Yahoo Finance answered 429" and result.stale is True
    assert result.live[wanted()].symbol == "VEQT.TO"  # the cached price, flagged stale
    calls = len(provider.quote_calls)
    clock.now += 60
    again = service.lookup([wanted()])
    assert len(provider.quote_calls) == calls and again.unavailable  # cooling down: no new requests
    clock.now += q.COOLDOWN
    provider.fail = None
    assert service.lookup([wanted()]).unavailable is None


def test_with_no_cache_and_yahoo_down_nothing_is_live():
    provider = FakeProvider()
    provider.fail = QuoteError("unavailable", "could not reach Yahoo Finance (ConnectError)")
    result = QuoteService(provider, Clock()).lookup([wanted()])
    assert result.live == {} and "ConnectError" in result.unavailable


def test_a_listing_in_another_currency_is_not_used():
    provider = FakeProvider(known={"VEQT.TO"})
    provider.currency = "USD"
    assert QuoteService(provider, Clock()).lookup([wanted()]).live == {}


def test_a_quote_with_no_currency_is_not_trusted():
    provider = FakeProvider()
    provider.currency = None
    assert QuoteService(provider, Clock()).lookup([wanted()]).live == {}


def test_history_failure_keeps_the_live_price():
    provider = FakeProvider()
    original = provider.history

    def flaky(symbol):
        raise QuoteError("unknown", "no history")

    provider.history = flaky
    result = QuoteService(provider, Clock()).lookup([wanted()])
    assert result.live[wanted()].quote.price == Decimal("47.85") and result.live[wanted()].bars == []
    provider.history = original


# -- the change maths -----------------------------------------------------------


def test_reference_prices_pick_the_right_closes():
    refs = reference_prices(live_quote(), history_bars())
    bars = dict(history_bars())

    def close_on_or_before(day):
        return bars[max(d for d in bars if d <= day)]

    assert refs["day"] == Decimal("47.40") and refs["open"] == Decimal("47.50")
    assert refs["1w"] == close_on_or_before(date(2026, 9, 25))
    assert refs["1m"] == close_on_or_before(date(2026, 9, 2))
    assert refs["3m"] == close_on_or_before(date(2026, 7, 2))
    assert refs["ytd"] == close_on_or_before(date(2025, 12, 31))
    assert refs["1y"] == close_on_or_before(date(2025, 10, 2))


def test_short_history_gives_none_instead_of_wrong_numbers():
    young = [(d, c) for d, c in history_bars() if d >= date(2026, 9, 10)]
    refs = reference_prices(live_quote(), young)
    assert refs["1w"] is not None and refs["1m"] is None and refs["ytd"] is None and refs["1y"] is None


def test_day_falls_back_to_the_last_close_and_open_may_be_missing():
    quote = Quote("X", "CAD", Decimal("50"), QUOTE_TIME, QUOTE_DAY, None, None)
    refs = reference_prices(quote, history_bars())
    assert refs["day"] == history_bars()[-1][1] and refs["open"] is None


def test_month_arithmetic_clamps_short_months():
    assert q._shift_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert q._shift_months(date(2026, 1, 15), -3) == date(2025, 10, 15)
    assert q._shift_months(date(2028, 3, 31), -1) == date(2028, 2, 29)


# -- the endpoint ----------------------------------------------------------------


@pytest.fixture
def live(seeded, services):
    client, auth = seeded
    provider = FakeProvider()
    services.quotes = QuoteService(provider, Clock())
    return client, auth, provider


def test_holdings_live_matches_the_fixture(live):
    client, auth, _ = live
    assert_matches(client.get("/api/holdings/live", headers=auth).json(), fixture_float("holdings_live.json"))


def test_live_value_replaces_plaids_and_the_maths_adds_up(live):
    client, auth, _ = live
    body = client.get("/api/holdings/live", headers=auth).json()
    veqt = next(h for h in body["holdings"] if h["ticker"] == "VEQT")
    assert veqt["live"] is True and veqt["symbol"] == "VEQT.TO" and veqt["market_open"] is True
    assert veqt["price"] == 47.85 and veqt["value"] == 42108.0  # 880 x 47.85
    assert veqt["changes"]["day"] == {"amount": 396.0, "percent": 0.95}  # 880 x (47.85 - 47.40)
    assert veqt["changes"]["open"] == {"amount": 308.0, "percent": 0.74}  # 880 x (47.85 - 47.50)
    assert veqt["gain"] == 4108.0  # against the 38,000 cost basis
    cash = next(h for h in body["holdings"] if h["ticker"] is None)
    assert cash["live"] is False and cash["changes"]["day"] is None and cash["value"] == 464.0
    cad = body["totals"]["CAD"]
    assert cad["value"] == 42108.0 + 464.0
    assert cad["changes"]["day"] == {"amount": 396.0, "percent": 0.95, "positions": 1}
    assert body["status"] == {
        "source": "yahoo", "enabled": True, "live_positions": 1, "positions": 2, "stale": False, "message": None,
    }


def test_yahoo_down_falls_back_to_plaid_and_says_so(live):
    client, auth, provider = live
    provider.fail = QuoteError("unavailable", "Yahoo Finance answered 429")
    body = client.get("/api/holdings/live", headers=auth).json()
    veqt = next(h for h in body["holdings"] if h["ticker"] == "VEQT")
    assert veqt["live"] is False and veqt["price"] == 47.2 and veqt["value"] == 41536.0
    assert body["status"]["message"] == "Yahoo Finance answered 429" and body["status"]["live_positions"] == 0
    assert body["totals"]["CAD"]["changes"]["day"] is None


def test_turned_off_never_calls_out(seeded, services):
    client, auth = seeded
    services.quotes = None
    body = client.get("/api/holdings/live", headers=auth).json()
    assert body["status"]["enabled"] is False and "turned off" in body["status"]["message"]
    assert all(h["live"] is False for h in body["holdings"])


def test_only_ticker_symbols_are_sent(live):
    client, auth, provider = live
    client.get("/api/holdings/live", headers=auth)
    assert set(provider.quote_calls) == {"VEQT.TO"}  # no cash, no names, no accounts


def test_the_exchange_code_from_plaid_is_used(live, services):
    client, auth, provider = live
    services.db.run("UPDATE holdings SET mic='XTSE' WHERE ticker='VEQT'")
    client.get("/api/holdings/live", headers=auth)
    assert provider.quote_calls == ["VEQT.TO"]


def test_live_route_needs_a_token_and_the_old_route_is_unchanged(client, seeded):
    assert client.get("/api/holdings/live").status_code == 401
    _, auth = seeded
    assert_matches(client.get("/api/holdings", headers=auth).json(), fixture_float("holdings.json"))


def test_sync_stores_the_exchange_code(services, plaid, seeded):
    from app.sync import sync_holdings

    plaid.holdings = {
        "accounts": [],
        "holdings": [{"account_id": "acc_tfsa", "security_id": "sec_veqt", "quantity": 880, "institution_price": 47.2,
                      "institution_value": 41536, "cost_basis": 38000, "iso_currency_code": "CAD"}],
        "securities": [{"security_id": "sec_veqt", "name": "VEQT", "ticker_symbol": "VEQT", "type": "etf",
                        "market_identifier_code": "XTSE"}],
    }
    sync_holdings(services.db, plaid, "token", "item_desj", "CAD")
    assert services.db.one("SELECT mic FROM holdings WHERE security_id='sec_veqt'")["mic"] == "XTSE"


# -- throttling: gentle requests, honoured waits, doubling rests ---------------------------


def test_429_carries_the_wait_yahoo_asked_for():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "120"}, text="Too Many Requests")

    with pytest.raises(QuoteError) as caught:
        mocked(handler).quote("VEQT.TO")
    assert caught.value.kind == "unavailable" and caught.value.retry_after == 120.0
    assert "429" in str(caught.value) and "too many requests" in str(caught.value)


@pytest.mark.parametrize("header, expected", [(None, None), ("soon", None), ("999999", q.MAX_COOLDOWN)])
def test_429_without_a_usable_wait(header, expected):
    def handler(request):
        return httpx.Response(429, headers={} if header is None else {"Retry-After": header})

    with pytest.raises(QuoteError) as caught:
        mocked(handler).quote("VEQT.TO")
    assert caught.value.retry_after == expected


def test_requests_to_yahoo_are_spaced_out():
    import time

    def handler(request):
        return httpx.Response(200, json=chart_json("1d"))

    provider = YahooProvider(httpx.Client(transport=httpx.MockTransport(handler)), spacing=0.05)
    started = time.monotonic()
    for _ in range(4):
        provider.quote("VEQT.TO")
    assert time.monotonic() - started >= 0.15  # three gaps of 0.05 s


def test_repeated_throttling_doubles_the_rest_and_a_success_resets_it():
    provider, clock = FakeProvider(), Clock()
    service = QuoteService(provider, clock)
    provider.fail = QuoteError("unavailable", "Yahoo Finance answered 429")

    def rest():
        before = clock.now
        service.lookup([wanted()])
        return service._cooldown_until - before

    assert rest() == q.COOLDOWN
    clock.now += q.COOLDOWN
    assert rest() == 2 * q.COOLDOWN
    clock.now += 2 * q.COOLDOWN
    assert rest() == 4 * q.COOLDOWN
    for _ in range(6):  # never longer than the cap
        clock.now += q.MAX_COOLDOWN
        assert rest() <= q.MAX_COOLDOWN
    clock.now += q.MAX_COOLDOWN
    provider.fail = None
    assert service.lookup([wanted()]).unavailable is None
    provider.fail = QuoteError("unavailable", "Yahoo Finance answered 429")
    clock.now += q.CLOSED_QUOTE_TTL + 1  # long enough that the cached price has to be refreshed
    assert rest() == q.COOLDOWN  # started again from the beginning


def test_the_wait_yahoo_asks_for_is_honoured_when_longer():
    provider, clock = FakeProvider(), Clock()
    service = QuoteService(provider, clock)
    provider.fail = QuoteError("unavailable", "Yahoo Finance answered 429", retry_after=900.0)
    service.lookup([wanted()])
    assert service._cooldown_until - clock.now == 900.0


def test_a_slow_lookup_that_made_progress_is_not_a_failure(monkeypatch):
    import time

    class Slow(FakeProvider):
        def quote(self, symbol):
            if symbol == "SLOW.TO":
                time.sleep(1.0)
            return super().quote(symbol)

    monkeypatch.setattr(q, "DEADLINE", 0.3)
    provider = Slow(known=("VEQT.TO", "SLOW.TO"))
    service = QuoteService(provider, Clock())
    result = service.lookup([wanted("VEQT"), wanted("SLOW")])
    assert result.unavailable is None and service._cooldown_until == 0.0  # no rest imposed
    assert result.live[wanted("VEQT")].symbol == "VEQT.TO"
    assert wanted("SLOW") not in result.live  # that one simply is not ready yet


def test_nothing_finishing_in_time_is_still_a_problem(monkeypatch):
    import time

    class Stuck(FakeProvider):
        def quote(self, symbol):
            time.sleep(1.0)
            return super().quote(symbol)

    monkeypatch.setattr(q, "DEADLINE", 0.2)
    result = QuoteService(Stuck(), Clock()).lookup([wanted()])
    assert result.unavailable == "Yahoo Finance was too slow"


# -- a bank price of 0 is missing, not a worthless holding ----------------------------------


def zero_the_banks_price(services):
    services.db.run("UPDATE holdings SET price='0', value='0' WHERE ticker='VEQT'")


def test_a_bank_price_of_zero_is_shown_as_missing(seeded, services):
    client, auth = seeded
    zero_the_banks_price(services)
    body = client.get("/api/holdings", headers=auth).json()
    veqt = next(h for h in body["holdings"] if h["ticker"] == "VEQT")
    assert (veqt["price"], veqt["value"], veqt["gain"]) == (None, None, None) and veqt["quantity"] == 880.0
    assert body["totals"]["CAD"] == 464.0  # only the cash line; the unpriced position is left out, not counted as $0


def test_with_yahoo_down_that_position_has_no_price_instead_of_zero(live, services):
    client, auth, provider = live
    zero_the_banks_price(services)
    provider.fail = QuoteError("unavailable", "Yahoo Finance answered 429 (too many requests)")
    body = client.get("/api/holdings/live", headers=auth).json()
    veqt = next(h for h in body["holdings"] if h["ticker"] == "VEQT")
    assert veqt["live"] is False and veqt["price"] is None and veqt["value"] is None
    assert body["totals"]["CAD"]["value"] == 464.0


def test_a_live_price_fixes_a_position_the_bank_could_not_price(live, services):
    client, auth, _ = live
    zero_the_banks_price(services)
    veqt = next(h for h in client.get("/api/holdings/live", headers=auth).json()["holdings"] if h["ticker"] == "VEQT")
    assert veqt["live"] is True and veqt["price"] == 47.85 and veqt["value"] == 42108.0


# -- the quote-test command --------------------------------------------------------------


def test_quote_test_command_reports_both_outcomes(monkeypatch, capsys):
    from app import cli

    class Fake:
        outcome = None

        def quote(self, symbol):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return live_quote(symbol)

    fake = Fake()
    monkeypatch.setattr("app.quotes.YahooProvider", lambda: fake)
    assert cli.main(["quote-test", "veqt.to"]) == 0
    assert "VEQT.TO: 47.85 CAD" in capsys.readouterr().out
    fake.outcome = QuoteError("unavailable", "Yahoo Finance answered 429 (too many requests)", retry_after=120.0)
    assert cli.main(["quote-test"]) == 1
    out = capsys.readouterr().out
    assert "XIU.TO" in out and "429" in out and "wait 120 seconds" in out
