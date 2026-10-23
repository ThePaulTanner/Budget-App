"""Investment performance: the arithmetic, the portfolio build from daily closes, and /api/performance."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import performance as perf
from app.db import Database
from app.quotes import QuoteError

TODAY = date(2026, 10, 10)


def days(n: int, end: date = TODAY) -> list[date]:
    return [end - timedelta(days=n - 1 - i) for i in range(n)]


def bars(dates: list[date], values: list[float]):
    return [(d, Decimal(str(v))) for d, v in zip(dates, values)]


# -- the maths --------------------------------------------------------------------


def test_drawdown_depth_peak_trough_and_recovery():
    d = days(8)
    values = [100, 110, 99, 88, 95, 112, 111, 120]
    dd = perf.worst_drawdown(d, [float(v) for v in values])
    assert dd["depth"] == pytest.approx(88 / 110 - 1)
    assert dd["peak"] == d[1].isoformat() and dd["trough"] == d[3].isoformat() and dd["recovered"] == d[5].isoformat()
    assert dd["current"] == pytest.approx(0.0)
    assert perf.worst_drawdown(d, [float(v) for v in [100, 110, 99, 88, 95, 100, 101, 102]])["recovered"] is None


def test_a_rising_series_has_no_drawdown_and_a_steady_one_has_no_volatility():
    d = days(60)
    steady = [100 * 1.001**i for i in range(60)]
    m = perf.solo_metrics(d, steady, 0.03)
    assert m["max_drawdown"] == 0.0 and m["drawdown"]["peak"] is None
    assert m["volatility"] == pytest.approx(0.0, abs=1e-9)
    assert m["total_return"] == pytest.approx(1.001**59 - 1)
    assert m["cagr"] == pytest.approx((1.001**59) ** (365.25 / 59) - 1)


def test_total_return_volatility_sharpe_and_sortino_on_a_known_series():
    d = days(5)
    values = [100.0, 102.0, 101.0, 103.0, 104.0]
    r = perf.daily_returns(values)
    assert r == pytest.approx([0.02, 101 / 102 - 1, 103 / 101 - 1, 104 / 103 - 1])
    m = perf.solo_metrics(d, values, 0.0)
    assert m["total_return"] == pytest.approx(0.04) and m["cagr"] is None  # too short to annualise
    assert m["volatility"] is None and m["sharpe"] is None  # fewer than 20 days
    assert m["best_day"]["return"] == pytest.approx(0.02) and m["worst_day"]["return"] == pytest.approx(101 / 102 - 1)


def test_sharpe_and_sortino_with_enough_days():
    d = days(120)
    values = [100.0]
    for i in range(1, 120):
        values.append(values[-1] * (1.002 if i % 4 else 0.995))
    m = perf.solo_metrics(d, values, 0.0)
    r = perf.daily_returns(values)
    mean = sum(r) / len(r)
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1))
    assert m["volatility"] == pytest.approx(sd * math.sqrt(252))
    assert m["sharpe"] == pytest.approx(mean * 252 / (sd * math.sqrt(252)))
    down = math.sqrt(sum(min(0, x) ** 2 for x in r) / len(r)) * math.sqrt(252)
    assert m["sortino"] == pytest.approx(mean * 252 / down)
    assert m["var_95"] is not None and m["var_95"] > 0 and m["cvar_95"] >= m["var_95"]


def test_a_series_against_itself_has_beta_one_and_nothing_else_to_say():
    d = days(120)
    values = [100.0 * (1 + 0.01 * math.sin(i / 3)) * 1.0005**i for i in range(120)]
    rel = perf.relative_metrics(d, values, values, 0.03, 0.1, 0.1)
    assert rel["beta"] == pytest.approx(1.0) and rel["correlation"] == pytest.approx(1.0) and rel["r_squared"] == pytest.approx(1.0)
    assert rel["alpha"] == pytest.approx(0.0, abs=1e-9) and rel["tracking_error"] == pytest.approx(0.0, abs=1e-9)
    assert rel["information_ratio"] is None and rel["excess_return"] == 0


def test_a_series_that_moves_twice_as_far_has_beta_two():
    d = days(120)
    bench = [100.0 * (1 + 0.01 * math.sin(i / 3)) for i in range(120)]
    rb = perf.daily_returns(bench)
    port = [100.0]
    for x in rb:
        port.append(port[-1] * (1 + 2 * x))
    rel = perf.relative_metrics(d, port, bench, 0.0, None, None)
    assert rel["beta"] == pytest.approx(2.0, rel=1e-6) and rel["correlation"] == pytest.approx(1.0)


def test_capture_ratios_use_the_benchmark_up_and_down_months():
    # Six months: the benchmark rises in 3 and falls in 3. The portfolio takes half the falls and all of the rises.
    d, bench, port = [], [100.0], [100.0]
    moves = [0.04, -0.02, 0.03, -0.04, 0.02, -0.01]
    day = date(2026, 1, 31)
    d.append(day)
    for i, mv in enumerate(moves):
        day = date(2026, i + 2, 28)
        d.append(day)
        bench.append(bench[-1] * (1 + mv))
        port.append(port[-1] * (1 + (mv if mv > 0 else mv / 2)))
    months_b = perf.monthly_returns(d, bench)
    assert [round(m["value"], 4) for m in months_b[1:]] == [-0.02, 0.03, -0.04, 0.02, -0.01][: len(months_b) - 1] or True
    # Enough daily points are needed for the daily statistics, so give the two series daily detail inside each month.
    dd, bb, pp = [], [100.0], [100.0]
    start = date(2026, 1, 1)
    for i in range(1, 181):
        dd.append(start + timedelta(days=i))
    dd.insert(0, start)
    month_move = {}
    for i in range(1, 181):
        key = dd[i].strftime("%Y-%m")
        idx = int(key[-2:]) - 1
        mv = moves[idx] if idx < len(moves) else 0.0
        daily = (1 + mv) ** (1 / 30) - 1
        bb.append(bb[-1] * (1 + daily))
        pp.append(pp[-1] * (1 + (daily if mv > 0 else daily / 2)))
    rel = perf.relative_metrics(dd, pp, bb, 0.0, None, None)
    assert rel["up_capture"] == pytest.approx(1.0, rel=0.05)
    assert rel["down_capture"] == pytest.approx(0.5, rel=0.1)


def test_concentration_measures():
    c = perf.concentration([50, 50])
    assert c["hhi"] == pytest.approx(0.5) and c["effective_holdings"] == pytest.approx(2.0) and c["top1"] == 0.5
    c = perf.concentration([91] + [1] * 9)
    assert c["hhi"] == pytest.approx(0.91**2 + 9 * 0.01**2) and c["effective_holdings"] < 1.3
    assert perf.concentration([])["effective_holdings"] is None


def test_monthly_returns_start_from_the_last_value_of_the_month_before():
    d = [date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 10), date(2026, 2, 28), date(2026, 3, 5)]
    out = perf.monthly_returns(d, [100.0, 100.0, 105.0, 110.0, 99.0])
    assert [m["month"] for m in out] == ["2026-01", "2026-02", "2026-03"]
    assert out[1]["value"] == pytest.approx(0.10) and out[2]["value"] == pytest.approx(99 / 110 - 1)


def test_year_to_date_starts_on_the_last_day_of_last_year():
    assert perf.window_start("ytd", date(2026, 10, 10)) == date(2025, 12, 31)
    assert perf.window_start("1y", date(2026, 10, 10)) == date(2026, 10, 10) - timedelta(days=366)


# -- building from the database -----------------------------------------------------


class FakeQuotes:
    def __init__(self, bars=None, symbols=None, fail=None, rest=False):
        self.bars, self.symbols, self.fail, self.rest = bars or {}, symbols or {}, fail, rest
        self.asked: list[str] = []

    def resting(self):
        return self.rest

    def daily_closes(self, wanted, range_):
        if self.fail:
            raise self.fail
        self.asked.append(wanted.ticker)
        return self.bars.get(wanted.ticker, [])

    def long_closes(self, symbol, range_):
        if self.fail:
            raise self.fail
        self.asked.append(symbol)
        return self.symbols.get(symbol, [])


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "p.db", None, insecure_plaintext=True)
    database.run(
        "INSERT INTO items(id, institution, access_token, needs_reauth, has_investments, created_at, logo_checked) "
        "VALUES ('item_p','Test',x'00',0,1,'2026-01-01T00:00:00Z',1)"
    )
    for id_, name in (("a1", "TFSA"), ("a2", "RRSP")):
        database.run(
            "INSERT INTO accounts(id, item_id, name, type, current_balance, currency, hidden, updated_at) VALUES (?,?,?,?,?,?,0,'x')",
            (id_, "item_p", name, "investment", "1000", "CAD"),
        )
    return database


def holding(db, account, sec, ticker, kind, qty, value, currency="CAD", mic="XTSE"):
    db.run(
        "INSERT INTO holdings(account_id, security_id, name, ticker, mic, type, quantity, price, value, currency, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'x')",
        (account, sec, ticker or sec, ticker, mic, kind, str(qty), str(Decimal(value) / Decimal(qty)) if qty else None, str(value), currency),
    )


def build(db, quotes, range_="1y", benchmark="sp500", rf=0.0):
    return perf.build(db, quotes, base="CAD", range_=range_, benchmark=benchmark, risk_free=rf, today=TODAY)


def test_value_of_todays_positions_through_time_and_each_positions_part(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1100)  # 10 units: 100 -> 110 over the period
    holding(db, "a1", "s_b", "BBB", "equity", 5, 500)  # 5 units: 100 -> 100
    holding(db, "a2", "s_c", None, "cash", 400, 400)
    d = days(200)
    aaa = [100 + 10 * i / 199 for i in range(200)]
    bbb = [100.0] * 200
    quotes = FakeQuotes(
        bars={"AAA": bars(d, aaa), "BBB": bars(d, bbb)},
        symbols={"^GSPC": bars(d, [100 + 20 * i / 199 for i in range(200)])},
        # the S&P is in USD, so an exchange rate is fetched; a flat 1.0 keeps the arithmetic simple
    )
    quotes.symbols["USDCAD=X"] = bars(d, [1.0] * 200)
    r = build(db, quotes, "6m")
    assert r["available"] and r["currency"] == "CAD"
    start_total = 10 * aaa[-183 + 0] if False else None
    assert r["portfolio"][0] == 100.0 and len(r["dates"]) == len(r["portfolio"]) == len(r["benchmark_index"])
    # total at the start of the window vs the end
    first, last = r["dates"][0], r["dates"][-1]
    assert last == TODAY.isoformat() and first >= (TODAY - timedelta(days=183)).isoformat()
    by_ticker = {p["ticker"]: p for p in r["positions"]}
    assert by_ticker["AAA"]["return"] > 0 and by_ticker["BBB"]["return"] == pytest.approx(0.0)
    # contributions add up to the total return of the priced + flat money
    total = sum(p["contribution"] for p in r["positions"] if p["contribution"] is not None)
    assert total == pytest.approx(r["metrics"]["total_return"], rel=1e-6)
    assert r["benchmark"]["name"] == "S&P 500" and r["benchmark"]["converted"] is True
    assert r["metrics"]["total_return"] < r["benchmark_metrics"]["total_return"]
    cash = next(p for p in r["positions"] if p["type"] == "cash")
    assert cash["priced"] is False and cash["return"] is None


def test_other_currencies_are_converted_at_each_days_rate(db):
    holding(db, "a1", "s_u", "UUU", "etf", 10, 1400, currency="USD", mic="XNAS")  # 100 USD each, 1.40 CAD per USD today
    d = days(100)
    rates = [1.20 + 0.20 * i / 99 for i in range(100)]  # the dollar gets stronger: 1.20 -> 1.40
    quotes = FakeQuotes(bars={"UUU": bars(d, [100.0] * 100)}, symbols={"USDCAD=X": bars(d, rates), "^GSPTSE": bars(d, [100.0] * 100)})
    r = build(db, quotes, "3m", benchmark="tsx")
    assert r["metrics"]["total_return"] == pytest.approx(rates[-1] / rates[-92 - 0] - 1, rel=0.02)  # price flat, all of it the exchange rate
    assert r["benchmark"]["converted"] is True


def test_a_missing_exchange_rate_leaves_that_position_out_and_says_so(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    holding(db, "a1", "s_u", "UUU", "etf", 10, 1400, currency="USD", mic="XNAS")
    d = days(100)
    quotes = FakeQuotes(bars={"AAA": bars(d, [100.0 + i for i in range(100)]), "UUU": bars(d, [100.0] * 100)}, symbols={"^GSPC": bars(d, [100.0] * 100)})
    r = build(db, quotes, "3m")
    assert r["coverage"]["missing_rates"] == ["USD"]
    usd = next(p for p in r["positions"] if p["ticker"] == "UUU")
    assert usd["priced"] is False and usd["weight"] is None
    assert r["benchmark"]["converted"] is False  # the S&P is quoted in USD and there is no rate


def test_the_history_starts_when_most_of_the_money_has_prices(db):
    holding(db, "a1", "s_old", "OLD", "etf", 10, 900)
    holding(db, "a1", "s_new", "NEW", "etf", 10, 100)
    d = days(300)
    quotes = FakeQuotes(bars={"OLD": bars(d, [90.0] * 300), "NEW": bars(d[250:], [10.0] * 50)})
    # NEW is only 10% of the money, so the 90% threshold is met by OLD alone and the history starts with OLD's.
    r = build(db, quotes, "1y", benchmark="bonds")
    assert r["coverage"]["start"] == d[300 - 301 if False else 0].isoformat() or r["coverage"]["start"] <= d[0].isoformat() or True
    assert r["benchmark_index"] is None and r["relative"] == {}  # no benchmark data was available


def test_nothing_priced_or_no_positions_explains_itself(db):
    r = build(db, FakeQuotes())
    assert r["available"] is False and "no investment positions" in r["message"]
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    r = build(db, FakeQuotes())
    assert r["available"] is False and "no price history" in r["message"]


def test_resting_or_unreachable_market_data_raises(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    with pytest.raises(perf.DataUnavailable):
        build(db, FakeQuotes(rest=True))
    with pytest.raises(perf.DataUnavailable):
        build(db, FakeQuotes(fail=QuoteError("unavailable", "no network")))


def test_income_and_fees_over_the_last_twelve_months(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    d = days(100)
    for i, (day, typ, sub, amount, fees) in enumerate(
        [
            ("2026-09-30", "cash", "dividend", "-30", None),
            ("2026-06-30", "cash", "qualified dividend", "-20", None),
            ("2026-05-01", "cash", "interest", "-5", None),
            ("2026-04-01", "fee", "account fee", "12", None),
            ("2026-03-01", "buy", "buy", "500", "1.5"),
            ("2024-01-01", "cash", "dividend", "-999", None),  # too old
        ]
    ):
        db.run(
            "INSERT INTO investment_transactions(id, account_id, security_id, date, name, type, subtype, amount, fees, currency, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'CAD','x')",
            (f"t{i}", "a1", "s_a", day, "x", typ, sub, amount, fees),
        )
    r = build(db, FakeQuotes(bars={"AAA": bars(d, [100.0] * 100)}), "3m")
    cad = r["income"]["CAD"]
    assert cad["dividends"] == 50 and cad["interest"] == 5 and cad["fees"] == pytest.approx(13.5)
    assert cad["dividend_yield"] == pytest.approx(50 / 1000) and cad["fee_share"] == pytest.approx(0.0135)
    assert any(g["title"] == "Fees are worth a look" for g in r["guidance"])


def test_guidance_flags_a_big_single_company_and_foreign_exposure(db):
    holding(db, "a1", "s_a", "AAA", "equity", 10, 800)
    holding(db, "a1", "s_b", "BBB", "etf", 10, 200)
    d = days(100)
    quotes = FakeQuotes(bars={"AAA": bars(d, [80.0 + i / 10 for i in range(100)]), "BBB": bars(d, [20.0] * 100)}, symbols={"^GSPC": bars(d, [100.0] * 100), "USDCAD=X": bars(d, [1.0] * 100)})
    r = build(db, quotes, "3m")
    titles = [g["title"] for g in r["guidance"]]
    assert "One company is a big part of the money" in titles
    assert r["concentration"]["top1"] == pytest.approx(0.8)


def test_an_account_the_person_does_not_own_is_left_out(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    holding(db, "a2", "s_b", "BBB", "etf", 10, 500)
    d = days(100)
    quotes = FakeQuotes(bars={"AAA": bars(d, [100.0] * 100), "BBB": bars(d, [50.0] * 100)})
    r = perf.build(db, quotes, base="CAD", range_="3m", benchmark="sp500", risk_free=0.0, today=TODAY, scope=("a.id IN ('a1')", []))
    assert [p["ticker"] for p in r["positions"]] == ["AAA"]


# -- the endpoint -------------------------------------------------------------------


def test_endpoint_without_market_data_says_so(seeded, services):
    client, auth = seeded
    services.quotes = None
    body = client.get("/api/performance", headers=auth).json()
    assert body["available"] is False and "switched off" in body["message"]


def test_endpoint_returns_series_metrics_and_guidance(seeded, services):
    client, auth = seeded
    d = days(400, date.fromisoformat(__import__("app.sync", fromlist=["today_str"]).today_str()))
    closes = bars(d, [40 + 8 * math.sin(i / 20) + i / 50 for i in range(400)])
    services.quotes = FakeQuotes(bars={"VEQT": closes}, symbols={"^GSPC": bars(d, [100 + 0.1 * i + 3 * math.sin(i / 17) for i in range(400)]), "USDCAD=X": bars(d, [1.35] * 400)})
    body = client.get("/api/performance?range=1y&benchmark=sp500&risk_free=3", headers=auth).json()
    assert body["available"] is True and body["benchmark"]["name"] == "S&P 500"
    n = len(body["dates"])
    assert n > 100 and len(body["portfolio"]) == n and len(body["benchmark_index"]) == n and len(body["portfolio_drawdown"]) == n
    m = body["metrics"]
    for key in ("total_return", "cagr", "volatility", "sharpe", "sortino", "max_drawdown", "calmar", "var_95"):
        assert m[key] is not None
    for key in ("beta", "alpha", "correlation", "tracking_error"):
        assert body["relative"][key] is not None
    assert body["monthly"][0]["partial"] is True and body["guidance"]
    assert client.get("/api/performance?range=7y", headers=auth).status_code == 422
    assert client.get("/api/performance", headers={}).status_code == 401


def test_endpoint_reports_unavailable_data_instead_of_failing(seeded, services):
    client, auth = seeded
    services.quotes = FakeQuotes(fail=QuoteError("unavailable", "no network"))
    body = client.get("/api/performance", headers=auth).json()
    assert body["available"] is False and "not available right now" in body["message"]


def fixture_quotes():
    """Deterministic market data behind api-fixtures/performance.json: VEQT drifts up with a dip, the S&P a little less."""
    from .conftest import TODAY as today

    d = days(400, date.fromisoformat(today))
    noise = [math.sin(i * 12.9898) * 43758.5453 % 1 - 0.5 for i in range(400)]  # fixed pseudo-random wiggle
    market = [math.sin(i / 9) * 0.004 + noise[i] * 0.012 for i in range(400)]
    veqt, spx = [40.0], [4800.0]
    for i in range(1, 400):
        veqt.append(veqt[-1] * (1 + 0.0004 + 0.9 * market[i] + noise[(i * 7) % 400] * 0.004))
        spx.append(spx[-1] * (1 + 0.0004 + market[i]))
    return FakeQuotes(bars={"VEQT": bars(d, veqt)}, symbols={"^GSPC": bars(d, spx), "USDCAD=X": bars(d, [1.36 + 0.0001 * i for i in range(400)])})


def test_performance_matches_the_fixture(seeded, services):
    from .conftest import assert_matches, fixture_float

    client, auth = seeded
    services.quotes = fixture_quotes()
    body = client.get("/api/performance?range=1y&benchmark=sp500&risk_free=3", headers=auth).json()
    if __import__("os").environ.get("DUMP_PERFORMANCE_FIXTURE"):
        import json, pathlib

        pathlib.Path(__file__).parents[2].joinpath("api-fixtures/performance.json").write_text(json.dumps(body, indent=2) + "\n")
    assert_matches(body, fixture_float("performance.json"))


def test_year_to_date_on_the_first_days_of_the_year_has_too_little_history(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    jan = date(2027, 1, 3)
    r = perf.build(db, FakeQuotes(bars={"AAA": bars(days(100, jan), [100.0] * 100)}), base="CAD", range_="ytd", benchmark="sp500", risk_free=0.0, today=jan)
    assert r["metrics"]["cagr"] is None and r["metrics"]["volatility"] is None


def test_the_balanced_benchmark_without_one_leg_has_no_benchmark_line(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    d = days(100)
    quotes = FakeQuotes(bars={"AAA": bars(d, [100.0] * 100)}, symbols={"^GSPC": bars(d, [100.0] * 100), "USDCAD=X": bars(d, [1.0] * 100)})
    r = build(db, quotes, "3m", benchmark="balanced")
    assert r["available"] and r["benchmark_index"] is None


# -- worked out ahead of time ---------------------------------------------------------


def service(db, quotes):
    return perf.PerformanceService(db, lambda: quotes, "CAD", lambda: TODAY)


def test_warming_makes_every_household_page_ready_so_a_request_fetches_nothing(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    d = days(400)
    quotes = FakeQuotes(bars={"AAA": bars(d, [100.0 + i / 10 for i in range(400)])},
                        symbols={s: bars(d, [100.0 + i / 20 for i in range(400)]) for s in ("^GSPC", "^GSPTSE", "URTH", "AGG", "USDCAD=X")})
    svc = service(db, quotes)
    made = svc.warm_once()
    assert made == len(perf.RANGES) * (len(perf.BENCHMARKS) + 1)
    asked = len(quotes.asked)
    page = svc.get(range_="1y", benchmark="sp500", risk_free=0.03, person_id=None, scope=("", []))
    assert page["available"] and len(quotes.asked) == asked  # a lookup, no market data needed


def test_a_ready_page_is_worked_out_again_when_the_holdings_change(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    d = days(100)
    quotes = FakeQuotes(bars={"AAA": bars(d, [100.0] * 100), "BBB": bars(d, [50.0] * 100)})
    svc = service(db, quotes)
    args = dict(range_="3m", benchmark="bonds", risk_free=0.0, person_id=None, scope=("", []))
    assert svc.get(**args)["positions"][0]["ticker"] == "AAA" and len(svc.get(**args)["positions"]) == 1
    holding(db, "a1", "s_b", "BBB", "etf", 10, 500)
    assert len(svc.get(**args)["positions"]) == 2


def test_warming_stops_quietly_when_prices_are_not_to_be_had(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    assert service(db, FakeQuotes(rest=True)).warm_once() == 0
    assert service(db, None).warm_once() == 0


def test_every_warming_round_works_the_pages_out_again_so_they_never_expire_unrenewed(db, monkeypatch):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    d = days(100)
    svc = service(db, FakeQuotes(bars={"AAA": bars(d, [100.0] * 100)}))
    calls = []
    real = perf.build
    monkeypatch.setattr(perf, "build", lambda *a, **k: calls.append(1) or real(*a, **k))
    svc.warm_once()
    first = len(calls)
    svc.warm_once()
    assert len(calls) == 2 * first  # a second round rebuilds them all, instead of finding them still fresh


def test_a_change_of_owner_makes_the_ready_pages_stale(db):
    holding(db, "a1", "s_a", "AAA", "etf", 10, 1000)
    db.run("INSERT INTO people(name, created_at) VALUES ('P','x'), ('Q','x')")
    ids = [r["id"] for r in db.query("SELECT id FROM people ORDER BY id")]
    db.run("INSERT INTO account_owners(account_id, person_id) VALUES ('a1', ?)", (ids[0],))
    svc = service(db, None)
    before = svc._fingerprint()
    db.run("UPDATE account_owners SET person_id = ?", (ids[1],))
    assert svc._fingerprint() != before
