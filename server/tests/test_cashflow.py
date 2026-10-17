"""Cash flow: periods, transfer matching, what counts, net worth of the accounts in view, overrides."""

from __future__ import annotations

import random
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import cashflow as cf
from app.crypto import seal
from app.transfers import find_pairs, match_transfers

from .conftest import TODAY

TODAY_D = date.fromisoformat(TODAY)
P = cf.Params


# -- a two-year household -------------------------------------------------------------------------

ACCOUNTS = [  # id, type, balance today, currency
    ("chq", "depository", "6200.00", "CAD"),
    ("sav", "depository", "18500.00", "CAD"),
    ("visa", "credit", "742.18", "CAD"),
    ("tfsa", "investment", "42000.00", "CAD"),
    ("usd", "depository", "1000.00", "USD"),
]


def seed_household(db, keys, days=730):
    """Two years of pay, a mortgage, daycare by e-Transfer, card spending paid from chequing,
    savings and TFSA transfers, refunds, two pending purchases and some bait for false matches."""
    rnd = random.Random(7)
    db.run(
        "INSERT INTO items(id, institution, access_token, has_investments, created_at, logo_checked) VALUES (?,?,?,?,?,1)",
        ("item_h", "Desjardins", seal(b"tok", keys.kek, b"item:item_h"), 1, "2024-01-01T00:00:00Z"),
    )
    for acct, kind, balance, currency in ACCOUNTS:
        db.run(
            "INSERT INTO accounts(id, item_id, name, type, current_balance, currency, hidden, updated_at) VALUES (?,?,?,?,?,?,0,?)",
            (acct, "item_h", acct.upper(), kind, balance, currency, "2026-10-02T00:00:00Z"),
        )
    rows, n = [], [0]

    def add(day, acct, amount, cat, name, detailed=None, pending=False):
        if start <= day <= TODAY_D:
            n[0] += 1
            rows.append((f"h{n[0]:05d}", acct, day.isoformat(), name, name, f"{Decimal(str(amount)):.2f}",
                         "USD" if acct == "usd" else "CAD", int(pending), cat, detailed or f"{cat}_OTHER"))

    start = TODAY_D - timedelta(days=days)
    visa_month: dict[str, Decimal] = {}
    d = start
    while d <= TODAY_D:
        if d.weekday() == 4 and ((d - start).days // 7) % 2 == 0:
            add(d, "chq", -3150, "INCOME", "Payroll", "INCOME_WAGES")
        if d.day == 1:
            add(d, "chq", 2100, "LOAN_PAYMENTS", "Mortgage", "LOAN_PAYMENTS_MORTGAGE_PAYMENT")
        if d.day == 2:
            add(d, "chq", 500, "TRANSFER_OUT", "To savings", "TRANSFER_OUT_SAVINGS")
            add(d, "sav", -500, "TRANSFER_IN", "From chequing", "TRANSFER_IN_ACCOUNT_TRANSFER")
        if d.day == 3:
            add(d, "chq", 300, "TRANSFER_OUT", "TFSA contribution", "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS")
        if d.day == 5:
            add(d, "chq", -800, "TRANSFER_IN", "e-Transfer from partner", "TRANSFER_IN_DEPOSIT")
        if d.day == 15:
            add(d, "chq", 950, "TRANSFER_OUT", "e-Transfer to daycare", "TRANSFER_OUT_OTHER_TRANSFER_OUT")
        if d.day == 18:
            add(d, "chq", round(rnd.uniform(85, 160), 2), "RENT_AND_UTILITIES", "Hydro")
        if d.day == 28:
            add(d, "sav", -round(rnd.uniform(9, 15), 2), "INCOME", "Interest", "INCOME_INTEREST_EARNED")
        if d.weekday() in (1, 5):
            add(d, "visa", round(rnd.uniform(45, 230), 2), "FOOD_AND_DRINK", rnd.choice(["IGA", "Metro"]), "FOOD_AND_DRINK_GROCERIES")
        if rnd.random() < 0.08:
            add(d, "visa", round(rnd.uniform(15, 160), 2), "GENERAL_MERCHANDISE", "Amazon")
        if d.day == 11 and d.month % 4 == 0:
            add(d, "visa", -round(rnd.uniform(15, 60), 2), "GENERAL_MERCHANDISE", "Amazon refund")
        if d.day == 20:
            prev = (d.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            if visa_month.get(prev):
                add(d, "chq", visa_month[prev], "LOAN_PAYMENTS", "Visa payment", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
                add(d + timedelta(days=1), "visa", -visa_month[prev], "LOAN_PAYMENTS", "Payment received")
        if d.day == 12 and d.month % 3 == 0:
            add(d, "usd", 40, "GENERAL_SERVICES", "US subscription")
        nxt = d + timedelta(days=1)
        if nxt.day == 1:
            month = d.strftime("%Y-%m")
            visa_month[month] = sum(
                (Decimal(r[5]) for r in rows if r[1] == "visa" and r[2].startswith(month) and Decimal(r[5]) > 0 and r[8] != "LOAN_PAYMENTS"),
                Decimal(0),
            )
        d = nxt
    add(TODAY_D, "visa", 64.37, "FOOD_AND_DRINK", "Metro", "FOOD_AND_DRINK_GROCERIES", pending=True)
    add(TODAY_D - timedelta(days=1), "visa", 32.10, "FOOD_AND_DRINK", "Cafe", "FOOD_AND_DRINK_COFFEE", pending=True)
    # Bait: a $50 grocery purchase and an unrelated $50 e-Transfer from a friend the next day.
    add(TODAY_D - timedelta(days=40), "chq", 50, "FOOD_AND_DRINK", "Fruiterie", "FOOD_AND_DRINK_GROCERIES")
    add(TODAY_D - timedelta(days=39), "sav", -50, "TRANSFER_IN", "e-Transfer from Marc", "TRANSFER_IN_DEPOSIT")
    with db.transaction():
        for r in rows:
            db.run(
                "INSERT INTO transactions(id, account_id, date, name, merchant_name, amount, currency, pending, "
                "plaid_category, category_detailed, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (*r, "2026-10-02T00:00:00Z"),
            )
    match_transfers(db)
    return rows


@pytest.fixture
def household(services, client, auth):
    seed_household(services.db, services.keys)
    return client, auth, services.db


def get(client, auth, query="", status=200):
    response = client.get(f"/api/cashflow{'?' + query if query else ''}", headers=auth)
    assert response.status_code == status, response.text
    return response.json()


def tx_named(db, name):
    return db.one("SELECT * FROM transactions WHERE name = ? ORDER BY date DESC LIMIT 1", (name,))


# -- periods --------------------------------------------------------------------------------------


def test_month_arithmetic_clamps_and_handles_leap_years():
    assert cf.add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert cf.add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)
    assert cf.add_months(date(2026, 1, 15), -5) == date(2025, 8, 15)


def test_ranges_line_up_with_the_calendar():
    t = date(2026, 10, 17)
    assert cf.resolve_range(P(range="mtd"), date(2026, 10, 1), None) == (date(2026, 10, 1), date(2026, 10, 1))
    assert cf.resolve_range(P(range="6m"), t, None) == (date(2026, 5, 1), t)
    assert cf.resolve_range(P(range="12m"), t, None) == (date(2025, 11, 1), t)
    assert cf.resolve_range(P(range="ytd"), t, None) == (date(2026, 1, 1), t)
    assert cf.resolve_range(P(range="all"), t, date(2024, 10, 1)) == (date(2024, 10, 1), t)
    assert cf.resolve_range(P(range="custom", start=date(2026, 9, 1), end=date(2027, 1, 1)), t, None) == (date(2026, 9, 1), t)
    for bad in (P(range="custom"), P(range="custom", start=date(2026, 10, 9), end=date(2026, 10, 1)), P(range="weird")):
        with pytest.raises(cf.CashflowError):
            cf.resolve_range(bad, t, None)


def test_previous_range_is_one_step_earlier():
    assert cf.previous_range("mtd", date(2026, 10, 1), date(2026, 10, 17)) == (date(2026, 9, 1), date(2026, 9, 17))
    assert cf.previous_range("mtd", date(2026, 3, 1), date(2026, 3, 31)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert cf.previous_range("6m", date(2026, 5, 1), date(2026, 10, 17)) == (date(2025, 11, 1), date(2026, 4, 17))
    assert cf.previous_range("ytd", date(2026, 1, 1), date(2026, 10, 17)) == (date(2025, 1, 1), date(2025, 10, 17))
    assert cf.previous_range("custom", date(2026, 10, 1), date(2026, 10, 10)) == (date(2026, 9, 21), date(2026, 9, 30))
    assert cf.previous_range("all", date(2024, 1, 1), date(2026, 1, 1)) is None


def test_weeks_clip_to_the_range_and_cross_the_year():
    b = cf.make_buckets(date(2025, 12, 24), date(2026, 1, 9), "week", 0)
    assert [x["start"] for x in b] == [date(2025, 12, 24), date(2025, 12, 29), date(2026, 1, 5)]
    assert [x["partial"] for x in b] == [True, False, True]
    assert b[1]["end"] == date(2026, 1, 4) and b[1]["label"] == "Week of Dec 29 – Jan 4, 2026"
    sundays = cf.make_buckets(date(2026, 10, 1), date(2026, 10, 17), "week", 6)
    assert all(x["start"].weekday() == 6 for x in sundays[1:])


def test_days_cross_the_clock_change_and_are_capped_at_a_year():
    days = cf.make_buckets(date(2026, 10, 28), date(2026, 11, 4), "day", 0)  # Toronto's clocks change Nov 1
    assert len(days) == 8 and len({x["start"] for x in days}) == 8
    months = cf.make_buckets(date(2025, 11, 1), date(2026, 10, 17), "month", 0)
    assert len(months) == 12 and months[-1]["partial"] and (months[0]["short_label"], months[0]["label"]) == ("Nov ’25", "November 2025")
    with pytest.raises(cf.CashflowError):
        cf.make_buckets(date(2024, 1, 1), date(2026, 1, 1), "day", 0)


# -- transfer matching ----------------------------------------------------------------------------


def _row(tx_id, acct, kind, day, amount, cat, currency="CAD"):
    return {"id": tx_id, "account_id": acct, "account_type": kind, "date": day.isoformat(),
            "amount": str(amount), "currency": currency, "category": cat}


def test_matcher_pairs_own_transfers_and_ignores_lookalikes():
    d = date(2026, 10, 10)
    rows = [
        _row("a", "chq", "depository", d, 500, "TRANSFER_OUT"), _row("b", "sav", "depository", d, -500, "TRANSFER_IN"),
        _row("c", "chq", "depository", d, 900, "LOAN_PAYMENTS"), _row("d", "visa", "credit", d + timedelta(3), -900, "LOAN_PAYMENTS"),
        _row("e", "chq", "depository", d, 75, "TRANSFER_OUT"), _row("f", "sav", "depository", d + timedelta(4), -75, "TRANSFER_IN"),
        _row("g", "chq", "depository", d, 50, "FOOD_AND_DRINK"), _row("h", "sav", "depository", d, -50, "TRANSFER_IN"),
        _row("i", "chq", "depository", d, 40, "FOOD_AND_DRINK"), _row("j", "visa", "credit", d, -40, "FOOD_AND_DRINK"),
        _row("k", "chq", "depository", d, 200, "TRANSFER_OUT"), _row("l", "chq", "depository", d, -200, "TRANSFER_IN"),
        _row("m", "chq", "depository", d, 300, "TRANSFER_OUT"), _row("n", "sav", "depository", d, -300, "TRANSFER_IN", "USD"),
        _row("o1", "chq", "depository", d, 100, "TRANSFER_OUT"), _row("o2", "chq", "depository", d, 100, "TRANSFER_OUT"),
        _row("p1", "sav", "depository", d, -100, "TRANSFER_IN"), _row("p2", "sav", "depository", d, -100, "TRANSFER_IN"),
        _row("q", "chq", "depository", d, "123.45", "TRANSFER_OUT"), _row("r", "visa", "credit", d + timedelta(1), "-123.45", None),
        _row("s", "chq", "depository", d, 60, "TRANSFER_OUT"), _row("u1", "sav", "depository", d + timedelta(2), -60, "TRANSFER_IN"),
        _row("u2", "sav", "depository", d + timedelta(1), -60, "TRANSFER_IN"),
    ]
    pairs = find_pairs(rows)
    assert (pairs["a"], pairs["c"], pairs["q"], pairs["s"]) == ("b", "d", "r", "u2")
    for lone in "efghijklmn":  # too far apart, a purchase, a card refund, same account, other currency
        assert lone not in pairs, lone
    assert {pairs["o1"], pairs["o2"]} == {"p1", "p2"} and "u1" not in pairs
    assert find_pairs(list(reversed(rows))) == pairs  # order doesn't matter


def test_matcher_on_real_rows_is_idempotent(household):
    _, _, db = household
    assert tx_named(db, "To savings")["transfer_pair_id"] and tx_named(db, "Visa payment")["transfer_pair_id"]
    for lone in ("TFSA contribution", "e-Transfer to daycare", "Fruiterie", "e-Transfer from Marc", "Mortgage"):
        assert tx_named(db, lone)["transfer_pair_id"] is None, lone
    before = db.query("SELECT id, transfer_pair_id FROM transactions ORDER BY id")
    match_transfers(db)
    assert db.query("SELECT id, transfer_pair_id FROM transactions ORDER BY id") == before


# -- the chart data ---------------------------------------------------------------------------------


def test_needs_sign_in_and_rejects_bad_input(household, client):
    assert client.get("/api/cashflow").status_code == 401
    c, auth, _ = household
    for bad in ("range=nope", "range=custom", "range=all&group=day", "group=hour", "week_start=tue",
                "accounts=zzz", "start=2026-13-01&range=custom&end=2026-10-01", "exclude_categories=%21%21"):
        get(c, auth, bad, 400)
    get(c, auth, "person_id=999", 404)


@pytest.mark.parametrize("query", [
    "range=mtd&group=day", "range=mtd&group=week", "range=6m&group=day", "range=6m&group=week",
    "range=6m&group=month", "range=12m&group=week", "range=ytd&group=month", "range=all&group=week",
    "range=all&group=month", "range=custom&start=2026-01-15&end=2026-03-10",
])
def test_periods_add_up_and_never_overlap(household, query):
    c, auth, _ = household
    body = get(c, auth, query)
    b, t = body["buckets"], body["totals"]
    assert b[0]["start"] == body["range"]["start"] and b[-1]["end"] == body["range"]["end"] == TODAY or "custom" in query
    for prev, nxt in zip(b, b[1:]):
        assert (date.fromisoformat(nxt["start"]) - date.fromisoformat(prev["end"])).days == 1
    assert sum(x["income"] for x in b) == pytest.approx(t["income"], abs=0.01)
    assert sum(x["expenses"] for x in b) == pytest.approx(t["expenses"], abs=0.01)
    assert b[-1]["cum_net"] == pytest.approx(t["net"], abs=0.01)
    assert all(len(x["top_expenses"]) <= 5 and len(x["top_income"]) <= 5 for x in b)


def test_defaults_and_comparison(household):
    c, auth, _ = household
    assert get(c, auth)["group"] == "day"
    assert get(c, auth, "range=all")["group"] == "month"
    assert get(c, auth, "range=custom&start=2026-09-10&end=2026-10-01")["group"] == "day"
    body = get(c, auth, "range=6m")
    assert body["previous"]["start"] == "2025-11-01" and body["previous"]["end"] == "2026-04-02"
    assert get(c, auth, "range=all")["previous"] is None


def test_what_counts(household):
    c, auth, _ = household
    body = get(c, auth, "range=6m&group=month")
    sep = next(b for b in body["buckets"] if b["start"] == "2026-09-01")
    detail = c.get(f"/api/cashflow/transactions?period_start={sep['start']}&period_end={sep['end']}", headers=auth).json()
    counted = {t["name"] for t in detail["transactions"]}
    left = {t["name"]: t["reason"] for t in detail["left_out"]}
    assert left["To savings"] == left["From chequing"] == left["Visa payment"] == "Transfer between your accounts"
    assert left["TFSA contribution"] == "Moved to or from your investments"
    assert {"e-Transfer to daycare", "Mortgage", "Payroll", "e-Transfer from partner", "Interest"} <= counted
    assert detail["income"] == pytest.approx(sep["income"]) and detail["expenses"] == pytest.approx(sep["expenses"])
    assert any("USD" in note for note in body["notes"])  # the USD account is left out, and said so
    sides = c.get(f"/api/cashflow/transactions?period_start={sep['start']}&period_end={sep['end']}&side=income", headers=auth).json()
    assert all(t["side"] == "income" for t in sides["transactions"] + sides["left_out"])
    # Refunds reduce expenses rather than counting as income.
    everything = c.get(f"/api/cashflow/transactions?period_start=2024-01-01&period_end={TODAY}&range=all", headers=auth).json()
    refunds = [t for t in everything["transactions"] if t["name"] == "Amazon refund"]
    assert refunds and all(t["side"] == "expense" and t["value"] < 0 for t in refunds)


def test_filters(household):
    c, auth, _ = household
    base = get(c, auth, "range=6m&group=month")["totals"]
    no_pending = get(c, auth, "range=6m&group=month&include_pending=false")["totals"]
    assert base["expenses"] - no_pending["expenses"] == pytest.approx(64.37 + 32.10, abs=0.01)
    with_transfers = get(c, auth, "range=6m&group=month&include_transfers=true")["totals"]
    assert with_transfers["income"] > base["income"] and with_transfers["expenses"] > base["expenses"]
    no_food = get(c, auth, "range=6m&group=month&exclude_categories=food_and_drink")
    assert no_food["totals"]["expenses"] < base["expenses"]
    assert all(cat["category"] != "FOOD_AND_DRINK" for b in no_food["buckets"] for cat in b["top_categories"])
    savings_only = get(c, auth, "range=6m&group=month&accounts=sav")["totals"]
    assert savings_only["expenses"] == 0 and savings_only["income"] > 0


def test_net_worth_follows_the_accounts_in_view(household):
    """Investment accounts are out of view by default, so the line is today's balances of the
    accounts shown (the card counts against it), walked back through their transactions."""
    c, auth, db = household
    body = get(c, auth, "range=6m&group=week")
    today_worth = 6200.00 + 18500.00 - 742.18
    assert body["buckets"][-1]["net_worth"] == pytest.approx(today_worth)
    assert body["totals"]["net_worth_end"] == pytest.approx(today_worth)
    ids = ("chq", "sav", "visa")
    for b in body["buckets"][::5] + [body["buckets"][0]]:
        later = sum(Decimal(r["amount"]) for r in db.query(
            "SELECT amount FROM transactions WHERE account_id IN (?,?,?) AND pending = 0 AND date > ?", (*ids, b["end"])))
        assert b["net_worth"] == pytest.approx(today_worth + float(later), abs=0.01), b["end"]
    assert body["totals"]["net_worth_change"] == pytest.approx(today_worth - body["totals"]["net_worth_start"])
    with_tfsa = get(c, auth, "range=6m&group=week&accounts=chq,sav,visa,tfsa")
    assert with_tfsa["buckets"][-1]["net_worth"] == pytest.approx(today_worth + 42000)


def test_person_scope(household):
    c, auth, _ = household
    paul = c.post("/api/people", json={"name": "Paul"}, headers=auth).json()["id"]
    assert c.put("/api/accounts/visa/owners", json={"person_ids": [paul]}, headers=auth).status_code == 200
    mine = get(c, auth, f"range=6m&group=month&person_id={paul}")
    assert mine["filters"]["accounts"] == ["visa"] and mine["totals"]["income"] == 0
    assert mine["buckets"][-1]["net_worth"] == pytest.approx(-742.18)


def test_overrides_win_and_keep_the_category(household):
    c, auth, db = household
    daycare, savings = tx_named(db, "e-Transfer to daycare"), tx_named(db, "To savings")
    before = get(c, auth, "range=all")["totals"]["expenses"]
    c.patch(f"/api/transactions/{daycare['id']}", json={"category": "childcare"}, headers=auth)
    out = c.patch(f"/api/transactions/{daycare['id']}", json={"cashflow": "exclude"}, headers=auth).json()
    assert out["cashflow_override"] == "exclude" and out["category"] == "CHILDCARE"  # category untouched
    assert c.patch(f"/api/transactions/{savings['id']}", json={"cashflow": "include"}, headers=auth).status_code == 200
    after = get(c, auth, "range=all")["totals"]["expenses"]
    assert before - after == pytest.approx(950 - 500)
    match_transfers(db)  # the matcher never undoes your choice
    assert tx_named(db, "To savings")["cashflow_override"] == "include"
    back = c.patch(f"/api/transactions/{daycare['id']}", json={"cashflow": None}, headers=auth).json()
    assert back["cashflow_override"] is None and back["category"] == "CHILDCARE"
    assert c.patch(f"/api/transactions/{daycare['id']}", json={"cashflow": "maybe"}, headers=auth).status_code == 422
    assert c.patch(f"/api/transactions/{daycare['id']}", json={}, headers=auth).status_code == 400
    assert c.patch("/api/transactions/nope", json={"cashflow": "exclude"}, headers=auth).status_code == 404


def test_two_years_by_week_is_quick(household):
    c, auth, _ = household
    started = time.perf_counter()
    body = get(c, auth, "range=all&group=week")
    assert len(body["buckets"]) > 100
    assert time.perf_counter() - started < 1.5
