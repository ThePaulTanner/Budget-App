from __future__ import annotations

import json
import threading
from decimal import Decimal

from app.crypto import seal
from app.db import utcnow
from app.plaid import PlaidError
from app.sync import SyncBusy, sync_item


def pj(text):
    return json.loads(text, parse_float=Decimal)  # numbers as Decimal, like the real client


ACCOUNTS = pj("""[
 {"account_id":"acc_chq","name":"Chequing","official_name":"Desjardins Chequing","mask":"1234",
  "type":"depository","subtype":"checking",
  "balances":{"current":3200.50,"available":3100.00,"limit":null,"iso_currency_code":"CAD"}},
 {"account_id":"acc_visa","name":"Visa","mask":"9876","type":"credit","subtype":"credit card",
  "balances":{"current":850.25,"available":4149.75,"limit":5000,"iso_currency_code":"CAD"}},
 {"account_id":"acc_mtg","name":"Mortgage","mask":"7777","type":"loan","subtype":"mortgage",
  "balances":{"current":312000.00,"available":null,"limit":null,"iso_currency_code":"CAD"}},
 {"account_id":"acc_tfsa","name":"TFSA","mask":"5555","type":"brokerage","subtype":"tfsa",
  "balances":{"current":42000.00,"available":null,"limit":null,"iso_currency_code":"CAD"}}
]""")


def tx(tx_id, amount, name="IGA", *, account="acc_chq", date="2026-10-01", pending=False,
       category="FOOD_AND_DRINK", pending_id=None, merchant="IGA"):
    return {
        "transaction_id": tx_id, "account_id": account, "date": date, "authorized_date": date,
        "name": name, "merchant_name": merchant, "amount": amount, "iso_currency_code": "CAD",
        "pending": pending, "pending_transaction_id": pending_id,
        "personal_finance_category": {"primary": category, "detailed": category + "_OTHER"},
        "logo_url": None,
    }


def page(added=(), modified=(), removed=(), cursor="c1", more=False):
    return {"added": list(added), "modified": list(modified),
            "removed": [{"transaction_id": r} for r in removed],
            "next_cursor": cursor, "has_more": more}


def add_item(services, item_id="item_desj", name="Desjardins"):
    services.db.run(
        "INSERT INTO items(id, institution, access_token, created_at) VALUES (?,?,?,?)",
        (item_id, name, seal(b"access-sandbox-SECRET", services.keys.kek, b"item:" + item_id.encode()), utcnow()),
    )
    return services.db.one("SELECT * FROM items WHERE id=?", (item_id,))


def run(services, item):
    return sync_item(services.db, services.plaid, services.keys, item, "CAD")


def test_full_sync_stores_exact_amounts_and_builds_net_worth(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(added=[tx("t1", Decimal("132.47"), pending=True),
                               tx("t2", Decimal("-2850.00"), "Payroll", category="INCOME", merchant=None)])]
    result = run(services, item)
    assert result == {"item_id": "item_desj", "institution": "Desjardins",
                      "transactions_changed": 2, "holdings": 0, "ok": True}
    stored = services.db.one("SELECT amount FROM transactions WHERE id='t1'")
    assert stored["amount"] == "132.47"  # kept exact as text, never a binary float
    assert services.db.one("SELECT cursor FROM items")["cursor"] == "c1"

    types = {a["id"]: a["type"] for a in client.get("/api/accounts", headers=auth).json()}
    assert types["acc_tfsa"] == "investment"  # Plaid's "brokerage" maps to investment
    summary = client.get("/api/summary", headers=auth).json()["net_worth"][0]
    assert summary == {"currency": "CAD", "assets": 45200.5, "liabilities": 312850.25,
                       "investments": 42000.0, "net_worth": -267649.75}

    from app.sync import snapshot_networth
    snapshot_networth(services.db)
    assert client.get("/api/networth?days=30", headers=auth).json()[0]["net_worth"] == -267649.75


def test_pending_charge_keeps_category_when_it_posts(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(added=[tx("pend1", Decimal("64.20"), pending=True)])]
    run(services, item)
    client.patch("/api/transactions/pend1", json={"category": "TREATS"}, headers=auth)
    item = services.db.one("SELECT * FROM items")
    plaid.pages = [page(added=[tx("post1", Decimal("64.20"), pending_id="pend1")], removed=["pend1"], cursor="c2")]
    run(services, item)
    assert services.db.one("SELECT 1 FROM transactions WHERE id='pend1'") is None
    row = services.db.one("SELECT override_category FROM transactions WHERE id='post1'")
    assert row["override_category"] == "TREATS"


def test_modified_keeps_override_and_removed_deletes(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(added=[tx("a", Decimal("10.00")), tx("b", Decimal("20.00"))])]
    run(services, item)
    client.patch("/api/transactions/a", json={"category": "MINE"}, headers=auth)
    plaid.pages = [page(modified=[tx("a", Decimal("11.50"))], removed=["b"], cursor="c2")]
    run(services, services.db.one("SELECT * FROM items"))
    row = services.db.one("SELECT amount, override_category FROM transactions WHERE id='a'")
    assert (row["amount"], row["override_category"]) == ("11.50", "MINE")
    assert services.db.one("SELECT 1 FROM transactions WHERE id='b'") is None


def test_pages_are_followed_and_mutation_restarts(services, plaid):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [
        page(added=[tx("a", Decimal("1.00"))], cursor="p1", more=True),
        PlaidError(400, "TRANSACTIONS_ERROR", "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "changed"),
        page(added=[tx("a", Decimal("1.00")), tx("b", Decimal("2.00"))], cursor="p2", more=False),
    ]
    assert run(services, item)["transactions_changed"] == 2
    assert services.db.one("SELECT cursor FROM items")["cursor"] == "p2"


def test_login_required_flags_the_bank_then_clears(services, plaid, client, auth):
    item = add_item(services)
    plaid.errors["accounts_get"] = PlaidError(400, "ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "relink")
    result = run(services, item)
    assert result["ok"] is False and "error" in result
    assert client.get("/api/summary", headers=auth).json()["items_needing_attention"] == ["Desjardins"]
    assert client.get("/api/items", headers=auth).json()[0]["needs_reauth"] is True
    del plaid.errors["accounts_get"]
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]
    assert run(services, services.db.one("SELECT * FROM items"))["ok"] is True
    assert client.get("/api/items", headers=auth).json()[0]["needs_reauth"] is False


def test_other_errors_do_not_ask_for_reconnect(services, plaid):
    item = add_item(services)
    plaid.errors["accounts_get"] = PlaidError(500, "API_ERROR", "INTERNAL_SERVER_ERROR", "oops")
    run(services, item)
    row = services.db.one("SELECT needs_reauth, error_code FROM items")
    assert (row["needs_reauth"], row["error_code"]) == (0, "INTERNAL_SERVER_ERROR")


def test_holdings_sync_and_gain(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page()]
    plaid.holdings = pj("""{"accounts":[],"securities":[
      {"security_id":"s1","name":"Vanguard All-Equity ETF Portfolio","ticker_symbol":"VEQT","type":"etf"},
      {"security_id":"s2","name":"Cash","ticker_symbol":null,"type":"cash"}],
     "holdings":[
      {"account_id":"acc_tfsa","security_id":"s1","quantity":880,"institution_price":47.2,
       "institution_value":41536.0,"cost_basis":38000.0,"iso_currency_code":"CAD"},
      {"account_id":"acc_tfsa","security_id":"s2","quantity":464,"institution_price":1.0,
       "institution_value":464.0,"cost_basis":null,"iso_currency_code":"CAD"}]}""")
    assert run(services, item)["holdings"] == 2
    body = client.get("/api/holdings", headers=auth).json()
    assert body["totals"] == {"CAD": 42000.0}
    assert [h["ticker"] for h in body["holdings"]] == ["VEQT", None]
    assert body["holdings"][0]["gain"] == 3536.0 and body["holdings"][1]["gain"] is None


def test_bank_without_investments_is_not_an_error(services, plaid):
    item = add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]
    plaid.errors["investments_holdings_get"] = PlaidError(400, "ITEM_ERROR", "PRODUCTS_NOT_SUPPORTED", "no")
    result = run(services, item)
    assert result["ok"] is True and result["holdings"] == 0
    assert services.db.one("SELECT has_investments FROM items")["has_investments"] == 0


def test_hidden_flag_survives_resync(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]
    run(services, item)
    client.patch("/api/accounts/acc_chq", json={"hidden": True}, headers=auth)
    plaid.pages = [page(cursor="c2")]
    run(services, services.db.one("SELECT * FROM items"))
    assert services.db.one("SELECT hidden FROM accounts WHERE id='acc_chq'")["hidden"] == 1


def test_sync_endpoint_runs_and_reports_busy(services, plaid, client, auth):
    add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]
    ok = client.post("/api/sync", headers=auth)
    assert ok.status_code == 200 and ok.json()[0]["ok"] is True
    services.sync._lock.acquire()
    try:
        busy = client.post("/api/sync", headers=auth)
    finally:
        services.sync._lock.release()
    assert busy.status_code == 409 and isinstance(busy.json()["detail"], str)


def test_link_exchange_stores_token_encrypted_and_queues_a_sync(services, plaid, client, auth):
    token = client.post("/api/link/token", json={}, headers=auth).json()
    assert token["link_token"] == "link-sandbox-123"
    assert plaid.last_link == {"webhook": "https://example.test/plaid/webhook", "update": False}
    done = client.post("/api/link/exchange", json={"public_token": "public-sandbox-1"}, headers=auth)
    assert done.json() == {"item_id": "item_new", "institution": "Desjardins"}
    row = services.db.one("SELECT access_token FROM items WHERE id='item_new'")
    assert b"access-sandbox-SECRET" not in bytes(row["access_token"])
    assert services.sync._queue.get_nowait() == "item_new"
    client.post("/api/link/token", json={"item_id": "item_new"}, headers=auth)
    assert plaid.last_link["update"] is True  # reconnect uses update mode


def test_link_complete_clears_warning(services, client, auth):
    add_item(services)
    services.db.run("UPDATE items SET needs_reauth=1, error_code='ITEM_LOGIN_REQUIRED'")
    assert client.post("/api/link/complete", json={"item_id": "item_desj"}, headers=auth).status_code == 200
    assert services.db.one("SELECT needs_reauth FROM items")["needs_reauth"] == 0
    assert client.post("/api/link/complete", json={"item_id": "nope"}, headers=auth).status_code == 404


def test_removing_a_bank_revokes_access_and_deletes_data(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page(added=[tx("a", Decimal("1.00"))])]
    run(services, item)
    assert client.delete("/api/items/item_desj", headers=auth).status_code == 200
    assert plaid.removed == ["access-sandbox-SECRET"]
    for table in ("items", "accounts", "transactions"):
        assert services.db.one(f"SELECT count(*) AS n FROM {table}")["n"] == 0
    assert client.delete("/api/items/item_desj", headers=auth).status_code == 404


def test_concurrent_manual_syncs_do_not_overlap(services, plaid):
    add_item(services)
    plaid.accounts, plaid.pages = ACCOUNTS, [page()]
    gate, entered = threading.Event(), threading.Event()
    original = plaid.accounts_get

    def slow(token):
        entered.set()
        gate.wait(5)
        return original(token)

    plaid.accounts_get = slow
    worker = threading.Thread(target=lambda: services.sync.sync_items(blocking=True))
    worker.start()
    entered.wait(5)
    try:
        try:
            services.sync.sync_items(blocking=False)
            raised = False
        except SyncBusy:
            raised = True
        assert raised
    finally:
        gate.set()
        worker.join(5)


def test_holding_currency_prefers_the_banks_then_the_security_then_its_market():
    from app.sync import holding_currency
    assert holding_currency({"iso_currency_code": "CAD"}, {"iso_currency_code": "USD"}, "CAD") == "CAD"
    assert holding_currency({}, {"iso_currency_code": "USD"}, "CAD") == "USD"
    assert holding_currency({}, {"market_identifier_code": "XNAS"}, "CAD") == "USD"
    assert holding_currency({}, {"market_identifier_code": "XTSE"}, "USD") == "CAD"
    assert holding_currency({}, {}, "CAD") == "CAD"


def test_a_us_etf_with_no_bank_currency_is_not_labelled_in_the_base_currency(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page()]
    plaid.holdings = pj("""{"accounts":[],"securities":[
      {"security_id":"s1","name":"ProShares UltraPro QQQ","ticker_symbol":"TQQQ","type":"etf","market_identifier_code":"XNAS"}],
     "holdings":[{"account_id":"acc_tfsa","security_id":"s1","quantity":28.1695,"institution_price":null,
       "institution_value":null,"cost_basis":null,"iso_currency_code":null}]}""")
    run(services, item)
    row = services.db.one("SELECT currency FROM holdings WHERE ticker='TQQQ'")
    assert row["currency"] == "USD"


def test_a_us_listing_the_bank_cannot_price_is_shown_in_usd_even_if_labelled_cad(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page()]
    plaid.holdings = pj("""{"accounts":[],"securities":[
      {"security_id":"s1","name":"ProShares UltraPro QQQ","ticker_symbol":"TQQQ","type":"etf","market_identifier_code":"XNAS"},
      {"security_id":"s2","name":"Vanguard All-Equity ETF Portfolio","ticker_symbol":"VEQT","type":"etf","market_identifier_code":"XTSE"}],
     "holdings":[
      {"account_id":"acc_tfsa","security_id":"s1","quantity":21.1279,"institution_price":null,
       "institution_value":null,"cost_basis":null,"iso_currency_code":"CAD"},
      {"account_id":"acc_tfsa","security_id":"s2","quantity":10,"institution_price":47.0,
       "institution_value":470.0,"cost_basis":null,"iso_currency_code":"CAD"}]}""")
    run(services, item)
    services.quotes = None  # no live prices here: the test must not depend on reaching Yahoo
    by_ticker = {h["ticker"]: h for h in client.get("/api/holdings", headers=auth).json()["holdings"]}
    assert by_ticker["TQQQ"]["currency"] == "USD" and by_ticker["VEQT"]["currency"] == "CAD"
    live = {h["ticker"]: h for h in client.get("/api/holdings/live", headers=auth).json()["holdings"]}
    assert live["TQQQ"]["currency"] == "USD" and live["VEQT"]["currency"] == "CAD"
    # the unpriced position starts no USD total of its own
    assert list(client.get("/api/holdings/live", headers=auth).json()["totals"]) == ["CAD"]
    assert client.get("/api/holdings", headers=auth).json()["totals"] == {"CAD": 470.0}
