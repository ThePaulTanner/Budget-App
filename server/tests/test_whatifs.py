"""What if you had not sold: each sale played out against the money, and the endpoint behind the page."""

from __future__ import annotations

import os
from datetime import date

import pytest

from app import whatifs
from app.performance import DataUnavailable, PerformanceService
from app.quotes import Wanted

from .test_performance import TODAY, FakeQuotes, bars, days, db, holding  # noqa: F401  (db is a fixture)


class WatchedQuotes(FakeQuotes):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.wanted: list[Wanted] = []

    def daily_closes(self, wanted, range_):
        self.wanted.append(wanted)
        return super().daily_closes(wanted, range_)


def sell(db, id_, day, ticker, qty, price, currency="CAD", sec="s_a", mic=None, account="a1"):
    db.run(
        "INSERT INTO investment_transactions(id, account_id, security_id, date, name, ticker, type, subtype, quantity, price, "
        "amount, currency, updated_at, mic) VALUES (?,?,?,?,?,?,'sell','sell',?,?,?,?,'x',?)",
        (id_, account, sec, day, f"Sell {ticker}", ticker, str(-qty), str(price), str(-qty * price), currency, mic),
    )


def buy(db, id_, day, ticker, qty, price, currency="CAD", sec="s_b", mic=None, account="a1"):
    db.run(
        "INSERT INTO investment_transactions(id, account_id, security_id, date, name, ticker, type, subtype, quantity, price, "
        "amount, currency, updated_at, mic) VALUES (?,?,?,?,?,?,'buy','buy',?,?,?,?,'x',?)",
        (id_, account, sec, day, f"Buy {ticker}", ticker, str(qty), str(price), str(qty * price), currency, mic),
    )


def play(db, quotes, range_="all", **kwargs):
    return whatifs.build(db, quotes, base="CAD", range_=range_, today=TODAY, **kwargs)


D = days(100)  # the last is TODAY


def test_a_sale_before_a_rise_is_a_missed_gain(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    r = play(db, FakeQuotes(bars={"AAA": bars(D, [100 + 0.2 * i for i in range(100)])}))
    s = r["sells"][0]
    last, at_sale = 100 + 0.2 * 99, 100 + 0.2 * 10
    kept = 1000 * last / at_sale  # what was paid, riding the share's moves since that day
    assert r["available"] and s["priced"] and s["verdict"] == "missed_gain"
    assert s["kept_now"] == pytest.approx(kept, abs=0.01) and s["proceeds_now"] == 1000.0
    assert s["gap"] == pytest.approx(kept - 1000, abs=0.01) and s["gap_percent"] == pytest.approx((kept - 1000) / 1000, abs=1e-4)
    assert s["kept_path"][0] == s["proceeds_path"][0] == 1000.0  # level on the sale day
    assert r["totals"]["gap"] == s["gap"] and r["totals"]["missed"] == 1 and r["totals"]["good"] == 0
    # the per-sale paths start at the sale date and line up with the dates
    assert r["dates"][s["start"]] == D[10].isoformat() and len(s["kept_path"]) == len(r["dates"]) - s["start"]
    assert r["gap_total"][-1] == s["gap"] and r["gap_total"][0] == 0


def test_a_sale_before_a_fall_was_a_good_call(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 400)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    r = play(db, FakeQuotes(bars={"AAA": bars(D, [100 - 0.2 * i for i in range(100)])}))
    s = r["sells"][0]
    assert s["verdict"] == "good_call" and s["gap"] < 0 and r["totals"]["good"] == 1


def test_the_real_value_and_the_kept_line_differ_by_the_gaps(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    r = play(db, FakeQuotes(bars={"AAA": bars(D, [100 + 0.2 * i for i in range(100)])}))
    assert len(r["actual"]) == len(r["kept"]) == len(r["dates"])
    for a, k, g in zip(r["actual"], r["kept"], r["gap_total"]):
        assert k - a == pytest.approx(g, abs=0.02)


def test_a_sale_in_another_currency_follows_the_exchange_rate_on_both_sides(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    rates = [1.20 + 0.20 * i / 99 for i in range(100)]
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100, currency="USD")
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100)}, symbols={"USDCAD=X": bars(D, rates)})
    r = play(db, quotes)
    s = r["sells"][0]
    assert s["proceeds"] == pytest.approx(1000 * rates[10], abs=0.01)  # converted at the sale date's rate
    assert s["gap"] == pytest.approx(0.0, abs=0.01) and s["verdict"] == "even"  # the price did not move; the cash moved with the dollar too


def test_small_sales_are_hidden_unless_asked_for(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    sell(db, "x2", D[20].isoformat(), "AAA", 1, 10)
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100)})
    r = play(db, quotes)
    assert [s["id"] for s in r["sells"]] == ["x1"] and r["coverage"]["hidden_small"] == 1 and r["coverage"]["sells"] == 2
    assert [s["id"] for s in play(db, quotes, min_amount=0)["sells"]] == ["x1", "x2"]


def test_a_sale_without_prices_is_listed_but_not_counted(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    sell(db, "x2", D[20].isoformat(), "ZZZ", 10, 50, sec="s_z")
    r = play(db, FakeQuotes(bars={"AAA": bars(D, [100.0] * 100)}))
    z = next(s for s in r["sells"] if s["id"] == "x2")
    assert z["priced"] is False and z["gap"] is None and z["reason"]
    assert r["coverage"]["unpriced"][0]["ticker"] == "ZZZ" and r["coverage"]["priced"] == 1
    only = play(db, FakeQuotes(), min_amount=0)
    assert only["available"] is False and "price history" in only["message"]


def test_a_sold_out_security_is_priced_on_the_market_found_for_it(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    db.run("UPDATE holdings SET mic = 'XTSE' WHERE security_id = 's_a'")
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)  # the sale itself carries no market
    quotes = WatchedQuotes(bars={"AAA": bars(D, [100.0] * 100)})
    play(db, quotes)
    assert quotes.wanted[0].ticker == "AAA" and quotes.wanted[0].mic == "XTSE"
    db.run("DELETE FROM holdings")
    quotes = WatchedQuotes(bars={"AAA": bars(D, [100.0] * 100)})
    db.run("UPDATE investment_transactions SET mic = 'XNAS' WHERE id = 'x1'")
    play(db, quotes)
    assert quotes.wanted[0].mic == "XNAS"


def test_no_sales_in_the_period_says_so_and_the_period_limits_which_are_seen(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    assert "not sold anything" in play(db, FakeQuotes())["message"]
    sell(db, "old", "2026-01-05", "AAA", 10, 100)
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100)})
    assert play(db, quotes, range_="3m")["available"] is False
    assert play(db, quotes, range_="all")["sells"][0]["id"] == "old"


def test_prices_resting_raises(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    with pytest.raises(DataUnavailable):
        play(db, FakeQuotes(rest=True))


def test_an_account_the_person_does_not_own_is_left_out(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    sell(db, "x2", D[10].isoformat(), "AAA", 10, 100, account="a2")
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100)})
    r = play(db, quotes, scope=("a.id IN ('a1')", []))
    assert [s["id"] for s in r["sells"]] == ["x1"]
    assert [s["id"] for s in play(db, quotes, account_id="a2")["sells"]] == ["x2"]


# -- kept ready, and the endpoint ----------------------------------------------------


def test_the_page_is_kept_and_worked_out_again_when_activity_changes(db, monkeypatch):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100)})
    svc = PerformanceService(db, lambda: quotes, "CAD", lambda: TODAY)
    calls = []
    real = whatifs.build
    monkeypatch.setattr(whatifs, "build", lambda *a, **k: calls.append(1) or real(*a, **k))
    args = dict(range_="all", min_amount=50.0, person_id=None, account_id=None, scope=("", []))
    svc.get_whatifs(**args)
    svc.get_whatifs(**args)
    assert len(calls) == 1
    sell(db, "x2", D[20].isoformat(), "AAA", 10, 100)
    assert len(svc.get_whatifs(**args)["sells"]) == 2 and len(calls) == 2
    svc.warm_once()
    assert len(calls) >= 2 + len(whatifs.RANGES) - 0  # every period is renewed by a warming round


def test_endpoint_without_prices_says_so(seeded, services):
    client, auth = seeded
    services.quotes = None
    body = client.get("/api/whatifs", headers=auth).json()
    assert body["available"] is False and "switched off" in body["message"]
    assert client.get("/api/whatifs", headers={}).status_code == 401
    assert client.get("/api/whatifs?range=7y", headers=auth).status_code == 422


def test_whatifs_matches_the_fixture(seeded, services):
    from .conftest import assert_matches, fixture_float
    from .test_performance import fixture_quotes

    client, auth = seeded
    services.quotes = fixture_quotes()
    # one more sale whose money went straight into a purchase, so the fixture shows a followed sale as well
    for row in (("wi_sell", "2026-07-01", "Sell VEQT", "sell", "sell", "-10", "44", "-440"), ("wi_buy", "2026-07-03", "Buy VEQT", "buy", "buy", "10", "44.5", "445")):
        services.db.run(
            "INSERT INTO investment_transactions(id, account_id, security_id, date, name, ticker, type, subtype, quantity, price, amount, fees, currency, updated_at) "
            "VALUES (?,?,?,?,?,'VEQT',?,?,?,?,?,'0','CAD','2026-10-02T00:00:00Z')",
            (row[0], "acc_tfsa", "sec_veqt", row[1], row[2], row[3], row[4], row[5], row[6], row[7]),
        )
    body = client.get("/api/whatifs?range=all&min_amount=0", headers=auth).json()
    assert body["available"] is True and [s["proceeds_mode"] for s in body["sells"]] == ["followed", "cash"]
    if os.environ.get("DUMP_PERFORMANCE_FIXTURE"):
        import json
        import pathlib

        pathlib.Path(__file__).parents[2].joinpath("api-fixtures/whatifs.json").write_text(json.dumps(body, indent=2) + "\n")
    assert_matches(body, fixture_float("whatifs.json"))


def test_a_zero_price_on_the_sale_day_makes_the_sale_unpriced_instead_of_failing(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    sell(db, "x2", D[20].isoformat(), "BBB", 10, 100, sec="s_b")
    closes = [100.0] * 100
    r = play(db, FakeQuotes(bars={"AAA": bars(D, closes), "BBB": bars(D, [0.0] * 100)}))
    bad = next(s for s in r["sells"] if s["id"] == "x2")
    assert bad["priced"] is False and "not usable" in bad["reason"] and r["available"] is True


# -- following the money into what it bought ------------------------------------------


def rising(start, per_day):
    return [start + per_day * i for i in range(100)]


def test_money_put_into_another_holding_is_followed_there(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    holding(db, "a1", "s_b", "BBB", "etf", 10, 2000)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    buy(db, "b1", D[12].isoformat(), "BBB", 10, 100)  # the whole 1,000, two days later
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100), "BBB": bars(D, [100.0] * 12 + rising(100, 1.0)[:88])})
    s = play(db, quotes)["sells"][0]
    bought_at, last = 100.0, 100.0 + 87.0
    assert s["proceeds_mode"] == "followed" and s["followed"][0]["ticker"] == "BBB" and s["followed"][0]["share"] == 1.0
    assert s["proceeds_now"] == pytest.approx(1000 * last / bought_at, abs=0.01)  # the money rode BBB
    assert s["kept_now"] == pytest.approx(1000.0, abs=0.01) and s["verdict"] == "good_call"  # AAA stood still
    cash = play(db, quotes, proceeds="cash")["sells"][0]
    assert cash["proceeds_mode"] == "cash" and cash["followed"] == [] and cash["proceeds_now"] == 1000.0 and cash["verdict"] == "even"


def test_the_kept_line_takes_out_what_the_money_bought(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    holding(db, "a1", "s_b", "BBB", "etf", 10, 2000)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    buy(db, "b1", D[12].isoformat(), "BBB", 10, 100)
    r = play(db, FakeQuotes(bars={"AAA": bars(D, [100.0] * 100), "BBB": bars(D, rising(100, 1.0))}))
    # actual (which holds BBB) + AAA kept - BBB: the purchase's growth is out of the picture, not counted twice
    s = r["sells"][0]
    assert r["gap_total"][-1] == pytest.approx(s["kept_now"] - s["proceeds_now"], abs=0.02) and r["gap_total"][-1] < -700  # BBB rose 78% after the buy
    for a, k, g in zip(r["actual"], r["kept"], r["gap_total"]):
        assert k - a == pytest.approx(g, abs=0.02)


def test_money_only_partly_reinvested_keeps_the_rest_as_cash(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    buy(db, "b1", D[11].isoformat(), "BBB", 4, 100)  # 400 of the 1,000
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100), "BBB": bars(D, [100.0] * 12 + [200.0] * 88)})
    s = play(db, quotes)["sells"][0]
    assert s["proceeds_mode"] == "partly" and s["followed"][0]["share"] == pytest.approx(0.4)
    assert s["proceeds_now"] == pytest.approx(600 + 400 * 2, abs=0.01)  # 600 as cash, 400 doubled


def test_a_buy_is_matched_once_and_only_inside_the_window_and_account(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    sell(db, "x2", D[11].isoformat(), "AAA", 10, 100)
    buy(db, "b1", D[12].isoformat(), "BBB", 10, 100)         # 1,000: only enough for the first sale
    buy(db, "b2", D[30].isoformat(), "BBB", 10, 100)         # far too late
    buy(db, "b3", D[12].isoformat(), "BBB", 10, 100, account="a2", sec="s_b2")  # another account
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100), "BBB": bars(D, [100.0] * 100)})
    first, second = play(db, quotes)["sells"]
    assert [f["id"] for f in first["followed"]] == ["b1"] and second["followed"] == [] and second["proceeds_mode"] == "cash"


def test_a_buy_that_cannot_be_priced_leaves_the_money_as_cash(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    buy(db, "b1", D[12].isoformat(), "ZZZ", 10, 100, sec="s_z")
    s = play(db, FakeQuotes(bars={"AAA": bars(D, [100.0] * 100)}))["sells"][0]
    assert s["followed"] == [] and s["proceeds_mode"] == "cash"


def test_the_two_ways_are_kept_separately_and_the_default_follows_the_money(db):
    holding(db, "a1", "s_a", "AAA", "etf", 5, 600)
    sell(db, "x1", D[10].isoformat(), "AAA", 10, 100)
    quotes = FakeQuotes(bars={"AAA": bars(D, [100.0] * 100)})
    svc = PerformanceService(db, lambda: quotes, "CAD", lambda: TODAY)
    base = dict(range_="all", min_amount=50.0, person_id=None, account_id=None, scope=("", []))
    assert svc.get_whatifs(**base)["proceeds"] == "auto"
    assert svc.get_whatifs(**base, proceeds="cash")["proceeds"] == "cash"
