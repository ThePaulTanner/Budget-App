"""Credit card payments due: the API, the Plaid sync behind it, and edge cases."""

from __future__ import annotations

from decimal import Decimal

from app.plaid import PlaidError

from .conftest import assert_matches, fixture_float
from .test_sync import ACCOUNTS, add_item, page, pj, run


def set_dues(services, **fields):
    row = {"due_date": "2026-10-25", "minimum_payment": "35.00", "statement_balance": "612.40",
           "statement_date": "2026-09-28", "last_payment_amount": "500.00",
           "last_payment_date": "2026-09-10", "is_overdue": 0, "updated_at": "2026-10-02T00:00:00Z", **fields}
    services.db.run(
        "INSERT OR REPLACE INTO card_dues(account_id, due_date, minimum_payment, statement_balance, "
        "statement_date, last_payment_amount, last_payment_date, is_overdue, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("acc_visa", row["due_date"], row["minimum_payment"], row["statement_balance"],
         row["statement_date"], row["last_payment_amount"], row["last_payment_date"],
         row["is_overdue"], row["updated_at"]),
    )


def visa(client, auth):
    body = client.get("/api/payments-due", headers=auth).json()
    return body, body["cards"][0]


def test_payments_due_matches_fixture(seeded):
    client, auth = seeded
    assert_matches(client.get("/api/payments-due", headers=auth).json(), fixture_float("payments_due.json"))


def test_requires_sign_in(client):
    assert client.get("/api/payments-due").status_code == 401


def test_statuses(services, seeded):
    client, auth = seeded
    assert visa(client, auth)[1]["status"] == "upcoming"
    set_dues(services, due_date="2026-10-05")  # three days away
    assert visa(client, auth)[1]["status"] == "due_soon"
    set_dues(services, due_date="2026-09-30")  # already past, statement not paid
    assert visa(client, auth)[1]["status"] == "overdue"
    set_dues(services, is_overdue=1)  # the bank says so
    assert visa(client, auth)[1]["status"] == "overdue"
    set_dues(services, last_payment_amount="612.40", last_payment_date="2026-10-01")  # paid in full since the statement
    body, card = visa(client, auth)
    assert (card["status"], card["statement_paid"]) == ("paid", True)
    assert body["totals"]["CAD"]["statement_due"] == 0.0 and body["totals"]["CAD"]["minimum_due"] == 0.0
    set_dues(services, last_payment_amount="612.40", last_payment_date="2026-09-10")  # paid before the statement closed
    assert visa(client, auth)[1]["statement_paid"] is False
    set_dues(services, last_payment_amount="100.00", last_payment_date="2026-10-01")  # partly paid
    assert visa(client, auth)[1]["statement_paid"] is False


def test_nothing_owed_and_unknown(services, seeded):
    client, auth = seeded
    services.db.run("DELETE FROM card_dues")
    body, card = visa(client, auth)
    assert (card["status"], card["dues_known"], card["due_date"], card["days_until_due"]) == ("unknown", False, None, None)
    assert body["dues_available"] is False
    assert body["totals"]["CAD"] == {"owed": 850.25, "statement_due": 0.0, "minimum_due": 0.0}
    services.db.run("UPDATE accounts SET current_balance='0' WHERE id='acc_visa'")
    assert visa(client, auth)[1]["status"] == "nothing_owed"


def test_hidden_cards_and_other_accounts_are_left_out(services, seeded):
    client, auth = seeded
    assert [c["account_id"] for c in client.get("/api/payments-due", headers=auth).json()["cards"]] == ["acc_visa"]
    client.patch("/api/accounts/acc_visa", json={"hidden": True}, headers=auth)
    body = client.get("/api/payments-due", headers=auth).json()
    assert body["cards"] == [] and body["totals"] == {}


def test_cards_sort_overdue_first_then_by_due_date(services, seeded):
    client, auth = seeded
    for acc, due, bal in [("acc_a", "2026-10-20", "10"), ("acc_b", "2026-10-09", "20"), ("acc_c", "2026-09-20", "30")]:
        services.db.run(
            "INSERT INTO accounts(id, item_id, name, type, current_balance, currency, hidden, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (acc, "item_desj", acc, "credit", bal, "CAD", 0, "2026-10-02T00:00:00Z"))
        services.db.run("INSERT INTO card_dues(account_id, due_date, updated_at) VALUES (?,?,?)",
                        (acc, due, "2026-10-02T00:00:00Z"))
    cards = client.get("/api/payments-due", headers=auth).json()["cards"]
    assert [c["account_id"] for c in cards] == ["acc_c", "acc_b", "acc_a", "acc_visa"]
    assert [c["status"] for c in cards] == ["overdue", "due_soon", "upcoming", "upcoming"]


def test_sync_stores_card_dues_exactly(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page()]
    plaid.liabilities = pj("""{"liabilities":{"credit":[
      {"account_id":"acc_visa","is_overdue":false,"last_payment_amount":500.10,"last_payment_date":"2026-09-10",
       "last_statement_balance":612.40,"last_statement_issue_date":"2026-09-28",
       "minimum_payment_amount":35.00,"next_payment_due_date":"2026-10-25"},
      {"account_id":"acc_not_ours","next_payment_due_date":"2026-10-01"}]}}""")
    assert run(services, item)["ok"] is True
    row = services.db.one("SELECT * FROM card_dues")
    assert (row["account_id"], row["due_date"], row["statement_balance"], row["minimum_payment"],
            row["last_payment_amount"], row["is_overdue"]) == ("acc_visa", "2026-10-25", "612.40", "35.00", "500.10", 0)
    assert services.db.one("SELECT COUNT(*) AS n FROM card_dues")["n"] == 1  # another bank's account ignored
    card = client.get("/api/payments-due", headers=auth).json()["cards"][0]
    assert card["statement_balance"] == 612.4 and Decimal(str(card["last_payment_amount"])) == Decimal("500.10")
    # A later sync that no longer lists the card clears it instead of showing stale dates.
    plaid.pages = [page(cursor="c2")]
    plaid.liabilities = {"liabilities": {"credit": []}}
    run(services, item)
    assert services.db.query("SELECT * FROM card_dues") == []


def test_banks_without_liabilities_still_sync(services, plaid):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page()]
    plaid.errors["liabilities_get"] = PlaidError(400, "ITEM_ERROR", "PRODUCTS_NOT_SUPPORTED", "nope")
    assert run(services, item)["ok"] is True
    assert services.db.query("SELECT * FROM card_dues") == []
    # Even a transient Plaid failure on this one call must not fail the bank's sync.
    plaid.pages = [page(cursor="c2")]
    plaid.errors["liabilities_get"] = PlaidError(500, "API_ERROR", "INTERNAL_SERVER_ERROR", "boom")
    assert run(services, item)["ok"] is True


def test_link_token_asks_for_liabilities_only_as_optional():
    from app.plaid import PlaidClient

    sent = {}

    class Recorder(PlaidClient):
        def _post(self, path, body):
            sent.update(body)
            return {}

    Recorder("id", "secret", "https://example.test").link_token_create("u", ("CA",), None)
    assert sent["products"] == ["transactions"] and "liabilities" in sent["optional_products"]


def test_zero_balance_beats_the_banks_overdue_flag(services, seeded):
    client, auth = seeded
    set_dues(services, is_overdue=1)
    assert visa(client, auth)[1]["status"] == "overdue"
    services.db.run("UPDATE accounts SET current_balance='0' WHERE id='acc_visa'")
    assert visa(client, auth)[1]["status"] == "nothing_owed"  # paid after the bank's last update


def test_a_past_due_date_with_no_refresh_since_is_unconfirmed(services, seeded):
    client, auth = seeded
    set_dues(services, due_date="2026-09-30", updated_at="2026-09-20T00:00:00Z")  # last refreshed before it fell due
    body, card = visa(client, auth)
    assert card["status"] == "unknown" and card["dues_updated_at"] == "2026-09-20T00:00:00Z"
    set_dues(services, due_date="2026-09-30", updated_at="2026-09-20T00:00:00Z", is_overdue=1)  # the bank says so
    assert visa(client, auth)[1]["status"] == "overdue"
    set_dues(services, due_date="2026-09-30", updated_at="2026-10-01T00:00:00Z")  # refreshed after it fell due
    assert visa(client, auth)[1]["status"] == "overdue"


def test_a_failed_refresh_keeps_the_old_dates(services, plaid):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page()]
    plaid.liabilities = pj('{"liabilities":{"credit":[{"account_id":"acc_visa","next_payment_due_date":"2026-10-25"}]}}')
    run(services, item)
    plaid.pages = [page(cursor="c2")]
    plaid.errors["liabilities_get"] = PlaidError(500, "API_ERROR", "INTERNAL_SERVER_ERROR", "boom")
    run(services, item)
    assert services.db.one("SELECT due_date FROM card_dues")["due_date"] == "2026-10-25"
