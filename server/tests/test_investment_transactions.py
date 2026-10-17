"""Buys, sells and dividends: the sync from Plaid and the API behind the Investments page."""

from __future__ import annotations

import json
from decimal import Decimal

from app.plaid import PlaidError

from .conftest import assert_matches, fixture_float
from .test_sync import ACCOUNTS, add_item, page, pj, run

SECURITIES = [{"security_id": "s1", "name": "Vanguard All-Equity ETF Portfolio", "ticker_symbol": "VEQT"}]


def plaid_tx(n, **fields):
    base = pj(json.dumps({
        "investment_transaction_id": f"pit_{n}", "account_id": "acc_tfsa", "security_id": "s1",
        "date": "2026-09-15", "name": "Buy VEQT", "type": "buy", "subtype": "buy",
        "quantity": 100, "price": 46.8, "amount": 4680.0, "fees": 0, "iso_currency_code": "CAD"}))
    return {**base, **{k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in fields.items()}}


def setup(services, plaid, txs):
    item = add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]
    plaid.investment_txns = {"investment_transactions": txs, "securities": SECURITIES,
                             "total_investment_transactions": len(txs)}
    return item


def test_matches_fixture(seeded):
    client, auth = seeded
    body = client.get("/api/investments/transactions", headers=auth).json()
    assert_matches(body, fixture_float("investment_transactions.json"))
    assert [t["id"] for t in body["transactions"]] == ["it_div", "it_buy", "it_sell"]  # newest first


def test_requires_sign_in(client):
    assert client.get("/api/investments/transactions").status_code == 401


def test_filters_and_paging(seeded):
    client, auth = seeded
    get = lambda q="": client.get(f"/api/investments/transactions{q}", headers=auth).json()  # noqa: E731
    assert get("?type=buy")["total"] == 1 and get("?type=SELL")["transactions"][0]["id"] == "it_sell"
    assert get("?security_id=sec_veqt")["total"] == 3 and get("?security_id=nope")["total"] == 0
    assert get("?account_id=acc_chq")["total"] == 0
    assert [t["id"] for t in get("?start=2026-09-01&end=2026-09-20")["transactions"]] == ["it_buy"]
    first = get("?limit=2")
    assert first["total"] == 3 and len(first["transactions"]) == 2
    assert get("?limit=2&offset=2")["transactions"][0]["id"] == "it_sell"
    assert client.get("/api/investments/transactions?start=nope", headers=auth).status_code == 400


def test_hidden_accounts_are_left_out(seeded):
    client, auth = seeded
    client.patch("/api/accounts/acc_tfsa", json={"hidden": True}, headers=auth)
    assert client.get("/api/investments/transactions", headers=auth).json()["total"] == 0


def test_signs_follow_plaid(seeded):
    client, auth = seeded
    by_id = {t["id"]: t for t in client.get("/api/investments/transactions", headers=auth).json()["transactions"]}
    assert by_id["it_buy"]["amount"] > 0 and by_id["it_buy"]["quantity"] > 0  # cash out, shares in
    assert by_id["it_sell"]["amount"] < 0 and by_id["it_sell"]["quantity"] < 0
    assert by_id["it_div"]["amount"] < 0  # cash in


def test_person_view(seeded):
    client, auth = seeded
    paul = client.post("/api/people", json={"name": "Paul"}, headers=auth).json()["id"]
    sam = client.post("/api/people", json={"name": "Sam"}, headers=auth).json()["id"]
    client.put("/api/accounts/acc_tfsa/owners", json={"person_ids": [sam]}, headers=auth)
    assert client.get(f"/api/investments/transactions?person_id={sam}", headers=auth).json()["total"] == 3
    assert client.get(f"/api/investments/transactions?person_id={paul}", headers=auth).json()["total"] == 0


def test_sync_stores_exact_values_and_is_repeatable(services, plaid):
    item = setup(services, plaid, [plaid_tx(1), plaid_tx(2, type="sell", subtype="sell", quantity=-20, amount=-900.0, date="2026-09-20")])
    assert run(services, item)["ok"] is True
    rows = services.db.query("SELECT * FROM investment_transactions ORDER BY id")
    assert [r["id"] for r in rows] == ["pit_1", "pit_2"]
    assert rows[0]["amount"] == "4680.0" and rows[0]["price"] == "46.8" and rows[0]["ticker"] == "VEQT"
    assert rows[0]["currency"] == "CAD" and rows[0]["name"] == "Buy VEQT"
    # The first sync looks back two years; a later one only the last 60 days. Re-reading is harmless.
    plaid.pages = [page(cursor="c2")]
    run(services, services.db.one("SELECT * FROM items"))
    assert services.db.one("SELECT count(*) AS n FROM investment_transactions")["n"] == 2
    first, second = plaid.investment_calls[0], plaid.investment_calls[1]
    assert first["start"] < second["start"] and first["end"] == second["end"]


def test_sync_reads_every_page(services, plaid):
    item = setup(services, plaid, [plaid_tx(i) for i in range(1, 8)])
    import app.sync as sync
    sync.INVESTMENT_PAGE = 3
    try:
        run(services, item)
    finally:
        sync.INVESTMENT_PAGE = 500
    assert services.db.one("SELECT count(*) AS n FROM investment_transactions")["n"] == 7
    assert [c["offset"] for c in plaid.investment_calls] == [0, 3, 6]


def test_a_bank_that_cannot_give_them_still_syncs(services, plaid):
    item = setup(services, plaid, [])
    plaid.errors["investments_transactions_get"] = PlaidError(400, "ITEM_ERROR", "PRODUCT_NOT_READY", "later")
    assert run(services, item)["ok"] is True
    plaid.pages = [page(cursor="c2")]
    plaid.errors["investments_transactions_get"] = PlaidError(500, "API_ERROR", "INTERNAL_SERVER_ERROR", "boom")
    assert run(services, services.db.one("SELECT * FROM items"))["ok"] is True
    assert services.db.one("SELECT count(*) AS n FROM investment_transactions")["n"] == 0


def test_skips_banks_without_investments(services, plaid):
    item = setup(services, plaid, [plaid_tx(1)])
    plaid.errors["investments_holdings_get"] = PlaidError(400, "ITEM_ERROR", "PRODUCTS_NOT_SUPPORTED", "no")
    run(services, item)
    assert "investments_transactions_get" not in plaid.calls


def test_a_row_for_another_banks_account_is_ignored(services, plaid):
    item = setup(services, plaid, [plaid_tx(1, account_id="acc_elsewhere"), plaid_tx(2)])
    run(services, item)
    assert [r["id"] for r in services.db.query("SELECT id FROM investment_transactions")] == ["pit_2"]


def test_decimal_amounts_stay_exact(services, plaid):
    item = setup(services, plaid, [plaid_tx(1, price=Decimal("10.10"), amount=Decimal("1010.10"), quantity=Decimal("100"))])
    run(services, item)
    row = services.db.one("SELECT * FROM investment_transactions")
    assert (row["price"], row["amount"]) == ("10.10", "1010.10")
