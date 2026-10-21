"""Net worth for days before the server began recording it, rebuilt from transactions and market prices."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db import Database
from app.history import Unavailable, rebuild_networth_history
from app.quotes import QuoteError

TODAY = date(2026, 10, 10)


class FakeQuotes:
    """Stands in for the market-data service: closes by ticker, and by Yahoo symbol for exchange rates."""

    def __init__(self, bars=None, fx=None, fail=None):
        self.bars, self.fx, self.fail = bars or {}, fx or {}, fail

    def resting(self):
        return False

    def daily_closes(self, wanted, range_):
        if self.fail:
            raise self.fail
        return self.bars.get(wanted.ticker, [])

    def long_closes(self, symbol, range_):
        if self.fail:
            raise self.fail
        return self.fx.get(symbol, [])


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "h.db", None, insecure_plaintext=True)
    database.run(
        "INSERT INTO items(id, institution, access_token, needs_reauth, has_investments, created_at, logo_checked) "
        "VALUES ('item_h','Test',x'00',0,1,'2026-01-01T00:00:00Z',1)"
    )
    return database


def account(db, id_, kind, balance, currency="CAD"):
    db.run(
        "INSERT INTO accounts(id, item_id, name, type, current_balance, currency, hidden, updated_at) VALUES (?,?,?,?,?,?,0,'x')",
        (id_, "item_h", id_, kind, str(balance), currency),
    )


def tx(db, id_, account_id, day, amount, pending=0):
    db.run(
        "INSERT INTO transactions(id, account_id, date, name, amount, currency, pending, plaid_category, updated_at) "
        "VALUES (?,?,?,?,?,'CAD',?,'OTHER','x')",
        (id_, account_id, day, id_, str(amount), pending),
    )


def row(db, day, person=0, currency="CAD"):
    return db.one("SELECT * FROM networth WHERE date=? AND person_id=? AND currency=?", (day, person, currency))


def bars(prices):
    """{day: close} from ('2026-10-01', 40) pairs."""
    return [(date.fromisoformat(d), Decimal(str(p))) for d, p in prices]


def test_bank_and_card_balances_are_worked_back_from_today(db):
    account(db, "chq", "depository", 1000)
    account(db, "visa", "credit", 300)
    tx(db, "t1", "chq", "2026-10-08", 100)            # money out
    tx(db, "t2", "chq", "2026-10-05", -500)           # money in
    tx(db, "t3", "chq", "2026-10-09", 40, pending=1)  # a deposit account's balance does not include pending
    tx(db, "c1", "visa", "2026-10-07", 50)
    tx(db, "c2", "visa", "2026-10-09", 20, pending=1)  # a card's balance does
    assert rebuild_networth_history(db, None, today=TODAY) > 0
    before = row(db, "2026-10-04")  # the day before the first transaction
    assert (before["assets"], before["liabilities"], before["net_worth"]) == ("600.00", "230.00", "370.00")
    assert row(db, "2026-10-05")["assets"] == "1100.00"
    assert row(db, "2026-10-07")["liabilities"] == "280.00"
    last = row(db, "2026-10-09")
    assert (last["assets"], last["liabilities"], last["net_worth"]) == ("1000.00", "300.00", "700.00")
    assert row(db, "2026-10-10") is None  # today comes from a real snapshot, not an estimate
    assert all(r["estimated"] == 1 for r in db.query("SELECT estimated FROM networth"))


def test_investments_use_the_quantity_held_that_day_and_that_days_close(db):
    account(db, "inv", "investment", 500)
    db.run(
        "INSERT INTO holdings(account_id, security_id, name, ticker, mic, type, quantity, price, value, currency, updated_at) "
        "VALUES ('inv','sec_a','AAA','AAA','XTSE','etf','10','50','500','CAD','x')"
    )
    db.run(
        "INSERT INTO investment_transactions(id, account_id, security_id, date, name, type, quantity, amount, currency, updated_at) "
        "VALUES ('b1','inv','sec_a','2026-10-06','Buy','buy','5','200','CAD','x')"
    )
    closes = bars([(f"2026-10-{n:02d}", 40 + n - 1) for n in range(1, 11)])  # 40 on the 1st .. 49 on the 10th
    quotes = FakeQuotes(bars={"AAA": closes})
    rebuild_networth_history(db, quotes, today=TODAY)
    # 5 units held on the 5th at 44, scaled so that today (10 x 49 = 490) matches the bank's 500
    expected = (Decimal(5) * 44 * Decimal(500) / Decimal(490)).quantize(Decimal("0.01"))
    assert Decimal(row(db, "2026-10-05")["investments"]) == expected
    assert row(db, "2026-10-05")["assets"] == row(db, "2026-10-05")["investments"]


def test_a_position_the_bank_cannot_price_is_valued_in_its_markets_currency_with_the_days_rate(db):
    account(db, "inv", "investment", 280)  # 2 units x 100 USD x 1.40
    db.run(
        "INSERT INTO holdings(account_id, security_id, name, ticker, mic, type, quantity, currency, updated_at) "
        "VALUES ('inv','sec_t','TQQQ','TQQQ','XNAS','etf','2','CAD','x')"  # the bank labels it CAD but sends no price
    )
    db.run(
        "INSERT INTO investment_transactions(id, account_id, security_id, date, name, type, quantity, amount, currency, updated_at) "
        "VALUES ('b1','inv','sec_t','2026-10-06','Buy','buy','1','140','CAD','x')"
    )
    days = [f"2026-10-{n:02d}" for n in range(1, 11)]
    quotes = FakeQuotes(bars={"TQQQ": bars([(d, 100) for d in days])}, fx={"USDCAD=X": bars([(d, "1.4") for d in days])})
    rebuild_networth_history(db, quotes, today=TODAY)
    assert Decimal(row(db, "2026-10-05")["investments"]) == Decimal("140.00")  # 1 unit x 100 x 1.40


def test_recorded_days_are_never_replaced_and_a_rebuild_replaces_the_estimates(db):
    account(db, "chq", "depository", 1000)
    tx(db, "t1", "chq", "2026-10-08", 100)
    tx(db, "t0", "chq", "2026-10-04", 50)
    db.run("INSERT INTO networth(date, currency, person_id, assets, liabilities, investments, net_worth) VALUES ('2026-10-08','CAD',0,'1','0','0','1')")
    first = rebuild_networth_history(db, None, today=TODAY)
    assert row(db, "2026-10-08")["estimated"] == 0 and row(db, "2026-10-08")["assets"] == "1"  # the recorded day stays
    assert row(db, "2026-10-07")["estimated"] == 1 and row(db, "2026-10-09") is None  # estimates stop at the first recorded day
    assert rebuild_networth_history(db, None, today=TODAY) == first  # again: replaced, not doubled
    assert db.one("SELECT count(*) AS n FROM networth WHERE estimated=1")["n"] == first


def test_each_person_gets_their_own_history_from_the_accounts_they_own(db):
    account(db, "mine", "depository", 1000)
    account(db, "theirs", "depository", 5000)
    tx(db, "t1", "mine", "2026-10-08", 100)
    tx(db, "t2", "theirs", "2026-10-08", 700)
    db.run("INSERT INTO people(id, name, created_at) VALUES (1,'Sam','x')")
    db.run("INSERT INTO account_owners(account_id, person_id) VALUES ('mine',1)")
    rebuild_networth_history(db, None, today=TODAY)
    assert row(db, "2026-10-07")["assets"] == "6800.00"            # the household: 1100 + 5700
    assert row(db, "2026-10-07", person=1)["assets"] == "1100.00"  # Sam owns only the first account


def test_when_prices_cannot_be_had_the_last_estimate_is_kept(db):
    account(db, "inv", "investment", 500)
    db.run(
        "INSERT INTO holdings(account_id, security_id, name, ticker, mic, type, quantity, price, value, currency, updated_at) "
        "VALUES ('inv','sec_a','AAA','AAA','XTSE','etf','10','50','500','CAD','x')"
    )
    db.run(
        "INSERT INTO investment_transactions(id, account_id, security_id, date, name, type, quantity, amount, currency, updated_at) "
        "VALUES ('b1','inv','sec_a','2026-10-06','Buy','buy','5','200','CAD','x')"
    )
    db.run("INSERT INTO networth(date, currency, person_id, assets, liabilities, investments, net_worth, estimated) VALUES ('2026-10-01','CAD',0,'9','0','0','9',1)")
    with pytest.raises(Unavailable):
        rebuild_networth_history(db, FakeQuotes(fail=QuoteError("unavailable", "Yahoo Finance answered 429")), today=TODAY)
    assert row(db, "2026-10-01")["assets"] == "9"


def test_nothing_is_built_without_transactions(db):
    account(db, "chq", "depository", 1000)
    assert rebuild_networth_history(db, None, today=TODAY) == 0


def test_the_networth_endpoint_says_which_days_are_estimates(services, client, auth):
    services.db.run("INSERT INTO networth(date, currency, person_id, assets, liabilities, investments, net_worth, estimated) VALUES ('2026-09-30','CAD',0,'1','0','0','1',1)")
    services.db.run("INSERT INTO networth(date, currency, person_id, assets, liabilities, investments, net_worth) VALUES ('2026-10-02','CAD',0,'2','0','0','2')")
    body = client.get("/api/networth?days=365", headers=auth).json()
    assert body[0]["date"] == "2026-09-30" and body[0]["estimated"] is True
    assert body[-1]["estimated"] is False


def test_a_sync_rebuilds_history_at_most_every_few_hours_unless_you_press_sync_now(services, plaid, monkeypatch):
    from tests.test_sync import ACCOUNTS, add_item, page

    add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]
    calls = []
    monkeypatch.setattr("app.history.rebuild_networth_history", lambda db, quotes: calls.append(1) or 0)
    def sync(**kwargs):
        services.sync.sync_items(blocking=True, **kwargs)
        services.sync._history_thread.join()  # it runs in the background

    sync()
    sync()
    assert len(calls) == 1  # the second sync is within the few hours
    sync(force_history=True)
    assert len(calls) == 2


def test_a_failure_while_rebuilding_history_never_fails_the_sync(services, plaid, monkeypatch):
    from tests.test_sync import ACCOUNTS, add_item, page

    add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]

    def boom(db, quotes):
        raise RuntimeError("surprise")

    monkeypatch.setattr("app.history.rebuild_networth_history", boom)
    results = services.sync.sync_items(blocking=True)
    services.sync._history_thread.join()
    assert results[0]["ok"] is True


def test_a_failed_rebuild_is_retried_soon_not_in_six_hours(services, plaid, monkeypatch):
    import time

    from tests.test_sync import ACCOUNTS, add_item, page

    add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]

    def waiting(db, quotes):
        raise Unavailable("Yahoo is resting")

    monkeypatch.setattr("app.history.rebuild_networth_history", waiting)
    services.sync.sync_items(blocking=True)
    services.sync._history_thread.join()
    assert 0 < services.sync._history_next - time.time() <= 15 * 60 + 1


def test_the_rebuild_does_not_ask_yahoo_while_live_prices_are_resting(db):
    account(db, "chq", "depository", 1000)
    tx(db, "t1", "chq", "2026-10-08", 100)

    class Resting(FakeQuotes):
        def resting(self):
            return True

    with pytest.raises(Unavailable):
        rebuild_networth_history(db, Resting(), today=TODAY)


def test_a_reconstruction_far_from_the_banks_balance_is_left_unscaled(db):
    account(db, "inv", "investment", 5000)  # the bank says 5000, the positions at Yahoo's prices come to 490
    db.run(
        "INSERT INTO holdings(account_id, security_id, name, ticker, mic, type, quantity, price, value, currency, updated_at) "
        "VALUES ('inv','sec_a','AAA','AAA','XTSE','etf','10','50','5000','CAD','x')"
    )
    db.run(
        "INSERT INTO investment_transactions(id, account_id, security_id, date, name, type, quantity, amount, currency, updated_at) "
        "VALUES ('b1','inv','sec_a','2026-10-06','Buy','buy','5','200','CAD','x')"
    )
    closes = bars([(f"2026-10-{n:02d}", 40 + n - 1) for n in range(1, 11)])
    rebuild_networth_history(db, FakeQuotes(bars={"AAA": closes}), today=TODAY)
    assert Decimal(row(db, "2026-10-05")["investments"]) == Decimal("220.00")  # 5 x 44, not scaled up by 10


def test_a_person_with_no_accounts_gets_no_history_and_nothing_breaks(db):
    account(db, "chq", "depository", 1000)
    tx(db, "t1", "chq", "2026-10-08", 100)
    db.run("INSERT INTO people(id, name, created_at) VALUES (1,'Sam','x')")
    rebuild_networth_history(db, None, today=TODAY)
    assert row(db, "2026-10-07") is not None and row(db, "2026-10-07", person=1) is None
