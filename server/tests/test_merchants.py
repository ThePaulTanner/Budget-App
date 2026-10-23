"""The merchant master list: automatic names, names you give to unclear transactions,
and spending tracked per merchant over time."""

from __future__ import annotations

import pytest

from app.merchants import assign_merchants, clean_name

from .test_sync import add_item, page, run, tx as plaid_tx


def add_tx(services, tx_id, name, merchant_name, amount, date="2026-10-02", account="acc_chq",
           category="OTHER", currency="CAD"):
    services.db.run(
        "INSERT INTO transactions(id, account_id, date, name, merchant_name, amount, currency, pending, "
        "plaid_category, updated_at) VALUES (?,?,?,?,?,?,?,0,?,?)",
        (tx_id, account, date, name, merchant_name, str(amount), currency, category, "2026-10-02T00:00:00Z"),
    )
    assign_merchants(services.db, [tx_id])


def merchants(client, auth, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/api/merchants?{query}", headers=auth).json()["merchants"]


def by_name(rows, name):
    return next(m for m in rows if m["name"] == name)


def name_it(client, auth, pattern, name=None, *, field="name", kind="equals", merchant_id=None):
    body = {"match_field": field, "match_type": kind, "pattern": pattern}
    if name is not None:
        body["name"] = name
    if merchant_id is not None:
        body["merchant_id"] = merchant_id
    return client.post("/api/merchants/aliases", json=body, headers=auth)


# -- the list fills itself ---------------------------------------------------------


def test_plaids_merchant_names_become_the_master_list(seeded):
    client, auth = seeded
    rows = merchants(client, auth, period="all")
    assert {m["name"] for m in rows} == {"IGA", "Mortgage payment", "Restaurant Le Local", "Payroll", "Visa payment", "Store"}
    assert all(m["auto"] for m in rows)
    iga = by_name(rows, "IGA")
    assert (iga["transactions"], iga["spent"], iga["first_date"], iga["last_date"]) == (1, 132.47, "2026-10-01", "2026-10-01")
    assert iga["top_category"] == "FOOD_AND_DRINK" and iga["aliases"] == 1


def test_transactions_carry_the_merchant_and_a_display_name(seeded):
    client, auth = seeded
    first = client.get("/api/transactions?limit=1", headers=auth).json()["transactions"][0]
    assert first["merchant"] == "IGA" and first["display_name"] == "IGA" and first["merchant_id"]
    patched = client.patch("/api/transactions/tx_000", json={"category": "groceries"}, headers=auth).json()
    assert patched["merchant"] == "IGA" and patched["display_name"] == "IGA"


# -- unclear names -----------------------------------------------------------------


def test_unclear_names_are_listed_then_named_and_future_ones_follow(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_p1", "POS PURCHASE 4521 TORONTO ON", None, "4.50", date="2026-10-01")
    add_tx(services, "tx_p2", "POS PURCHASE 4521 TORONTO ON", None, "5.25", date="2026-09-20")
    add_tx(services, "tx_p3", "E-TRANSFER 99", None, "20.00")
    todo = client.get("/api/merchants/unassigned", headers=auth).json()
    assert [(t["name"], t["transactions"], t["outflow"]) for t in todo] == [
        ("POS PURCHASE 4521 TORONTO ON", 2, 9.75), ("E-TRANSFER 99", 1, 20.0)]
    preview = client.post("/api/merchants/aliases/preview", headers=auth, json={
        "match_field": "name", "match_type": "contains", "pattern": "pos purchase 4521"}).json()
    assert preview["matching_transactions"] == 2 and preview["would_apply"] == 2
    assert preview["kept_by_other_names"] == 0 and preview["currently_unassigned"] == 2
    assert preview["samples"][0]["name"] == "POS PURCHASE 4521 TORONTO ON" and preview["samples"][0]["account_name"]

    made = name_it(client, auth, "POS PURCHASE 4521", "Corner Store", kind="contains")
    assert made.status_code == 201
    body = made.json()
    assert body["merchant"]["name"] == "Corner Store" and body["updated_transactions"] == 2
    assert body["alias"]["source"] == "user"
    assert [t["name"] for t in client.get("/api/merchants/unassigned", headers=auth).json()] == ["E-TRANSFER 99"]
    shown = client.get(f"/api/transactions?merchant_id={body['merchant']['id']}", headers=auth).json()
    assert shown["total"] == 2 and {t["display_name"] for t in shown["transactions"]} == {"Corner Store"}
    # a later transaction with similar text is named without asking again
    add_tx(services, "tx_p4", "POS PURCHASE 4521 MISSISSAUGA", None, "3.00")
    assert services.db.one("SELECT merchant_id FROM transactions WHERE id='tx_p4'")["merchant_id"] == body["merchant"]["id"]


def test_naming_with_an_existing_merchant_id_and_reusing_a_name(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_q1", "SQ *IGA 2231", None, "10.00")
    iga = by_name(merchants(client, auth), "IGA")
    assert name_it(client, auth, "SQ *IGA 2231", merchant_id=iga["id"]).status_code == 201
    assert by_name(merchants(client, auth, period="all"), "IGA")["transactions"] == 2
    add_tx(services, "tx_q2", "SQ *IGA 9999", None, "7.00")
    assert name_it(client, auth, "SQ *IGA 9999", "iga").json()["merchant"]["id"] == iga["id"]  # same name, any case


def test_a_name_you_give_beats_plaids_and_leaves_no_empty_merchant_behind(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_a1", "AMZN Mktp CA*1", "Amzn Mktp", "30.00", category="GENERAL_MERCHANDISE")
    assert "Amzn Mktp" in {m["name"] for m in merchants(client, auth)}
    name_it(client, auth, "Amzn Mktp", "Amazon", field="merchant_name")
    names = {m["name"] for m in merchants(client, auth)}
    assert "Amazon" in names and "Amzn Mktp" not in names  # the empty automatic merchant is tidied away
    add_tx(services, "tx_a2", "AMZN Mktp CA*2", "Amzn Mktp", "12.00")
    assert by_name(merchants(client, auth, period="all"), "Amazon")["transactions"] == 2


def test_the_preview_uses_the_real_priority(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_v1", "SHELL 12 TORONTO", None, "40.00")
    add_tx(services, "tx_v2", "SHELL 99 OTTAWA", None, "30.00")
    name_it(client, auth, "SHELL 12 TORONTO", "Shell Toronto")  # an exact name you already gave
    preview = client.post("/api/merchants/aliases/preview", headers=auth, json={
        "match_field": "name", "match_type": "contains", "pattern": "shell"}).json()
    assert preview["matching_transactions"] == 2 and preview["would_apply"] == 1
    assert preview["kept_by_other_names"] == 1 and preview["samples"][0]["name"] == "SHELL 99 OTTAWA"
    only_blocked = client.post("/api/merchants/aliases/preview", headers=auth, json={
        "match_field": "name", "match_type": "contains", "pattern": "toronto"}).json()
    assert only_blocked["would_apply"] == 0 and only_blocked["samples"][0]["name"] == "SHELL 12 TORONTO"
    # saving really does what the preview said
    saved = name_it(client, auth, "shell", "Shell Gas", kind="contains").json()
    assert saved["updated_transactions"] == 1


def test_bank_text_is_stored_with_tidy_whitespace(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_w1", "COFFEE   SHOP  9", None, "3.00")
    alias = name_it(client, auth, "  coffee   shop 9 ", "Coffee Shop").json()["alias"]
    assert alias["pattern"] == "coffee shop 9"
    assert client.get("/api/merchants/unassigned", headers=auth).json() == []  # whitespace differences still match
    again = name_it(client, auth, "COFFEE SHOP 9", "Coffee Shop 2").json()
    assert again["alias"]["id"] == alias["id"]  # one row, not two


def test_priority_is_yours_then_equals_then_longest(services):
    from app.merchants import best_alias

    tx = {"name": "STARBUCKS 123 TORONTO", "merchant_name": None}
    def alias(i, kind, pattern, source="user", field="name"):
        return {"id": i, "match_field": field, "match_type": kind, "pattern": pattern, "source": source, "merchant_id": i}

    assert best_alias([alias(1, "contains", "starbucks"), alias(2, "contains", "starbucks 123")], tx)["id"] == 2
    assert best_alias([alias(1, "contains", "starbucks"), alias(2, "equals", "starbucks 123 toronto")], tx)["id"] == 2
    assert best_alias([alias(1, "equals", "starbucks 123 toronto", "auto"), alias(2, "contains", "star")], tx)["id"] == 2
    assert best_alias([alias(1, "equals", "x", field="merchant_name")], tx) is None  # null never matches


def test_renaming_an_automatic_merchant_keeps_later_transactions_attached(seeded, services):
    client, auth = seeded
    iga = by_name(merchants(client, auth), "IGA")
    renamed = client.patch(f"/api/merchants/{iga['id']}", json={"name": "IGA Supermarket"}, headers=auth).json()
    assert renamed["name"] == "IGA Supermarket" and renamed["auto"] is False
    add_tx(services, "tx_r1", "IGA #4411", "IGA", "20.00")
    rows = merchants(client, auth, period="all")
    assert [m["name"] for m in rows if "IGA" in m["name"]] == ["IGA Supermarket"]  # not a second "IGA"
    assert by_name(rows, "IGA Supermarket")["transactions"] == 2


# -- merge, delete, conflicts --------------------------------------------------------


def test_merge_moves_names_and_transactions(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_m1", "UBER *TRIP", "Uber", "15.00")
    add_tx(services, "tx_m2", "UBER EATS", "Uber Eats", "25.00")
    rows = merchants(client, auth)
    uber, eats = by_name(rows, "Uber"), by_name(rows, "Uber Eats")
    assert client.post(f"/api/merchants/{eats['id']}/merge", json={"into_id": uber["id"]}, headers=auth).status_code == 200
    rows = merchants(client, auth, period="all")
    assert "Uber Eats" not in {m["name"] for m in rows}
    assert by_name(rows, "Uber")["transactions"] == 2 and by_name(rows, "Uber")["aliases"] == 2
    add_tx(services, "tx_m3", "UBER EATS 2", "Uber Eats", "9.00")  # Plaid still says "Uber Eats"
    assert by_name(merchants(client, auth, period="all"), "Uber")["transactions"] == 3
    assert client.post(f"/api/merchants/{uber['id']}/merge", json={"into_id": uber["id"]}, headers=auth).status_code == 400
    assert client.post("/api/merchants/9999/merge", json={"into_id": uber["id"]}, headers=auth).status_code == 404


def test_deleting_forgets_your_names(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_d1", "CORNER 1", None, "5.00")
    corner = name_it(client, auth, "CORNER 1", "Corner").json()["merchant"]["id"]
    assert client.delete(f"/api/merchants/{corner}", headers=auth).status_code == 200
    assert [t["name"] for t in client.get("/api/merchants/unassigned", headers=auth).json()] == ["CORNER 1"]
    iga = by_name(merchants(client, auth), "IGA")["id"]
    assert client.delete(f"/api/merchants/{iga}", headers=auth).status_code == 200
    assert "IGA" in {m["name"] for m in merchants(client, auth)}  # Plaid's own name comes straight back
    assert client.delete("/api/merchants/9999", headers=auth).status_code == 404


def test_deleting_one_alias_unnames_its_transactions(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_x1", "CORNER 7", None, "5.00")
    alias = name_it(client, auth, "CORNER 7", "Corner").json()["alias"]
    assert client.delete(f"/api/merchants/aliases/{alias['id']}", headers=auth).status_code == 200
    assert [t["name"] for t in client.get("/api/merchants/unassigned", headers=auth).json()] == ["CORNER 7"]
    assert client.delete("/api/merchants/aliases/9999", headers=auth).status_code == 404


def test_reassigning_the_same_text_moves_it_to_the_new_merchant(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_y1", "SHELL 12", None, "40.00")
    first = name_it(client, auth, "SHELL 12", "Gas").json()
    second = name_it(client, auth, "shell 12", "Shell").json()  # same text, different case
    assert second["alias"]["id"] == first["alias"]["id"] and second["merchant"]["name"] == "Shell"
    assert services.db.one("SELECT count(*) AS n FROM merchant_aliases WHERE pattern LIKE 'shell 12'")["n"] == 1


def test_names_must_be_valid_and_unique(seeded):
    client, auth = seeded
    iga = by_name(merchants(client, auth), "IGA")
    assert client.post("/api/merchants", json={"name": "iga"}, headers=auth).status_code == 409
    assert client.post("/api/merchants", json={"name": "   "}, headers=auth).status_code in (400, 422)
    made = client.post("/api/merchants", json={"name": "  Hydro   One \x07 "}, headers=auth)
    assert made.status_code == 201 and made.json()["name"] == "Hydro One"
    assert client.patch(f"/api/merchants/{iga['id']}", json={"name": "hydro one"}, headers=auth).status_code == 409
    assert client.post("/api/merchants", json={"name": "x" * 81}, headers=auth).status_code == 400
    assert name_it(client, auth, "ABC").status_code == 400  # neither name nor merchant_id
    assert name_it(client, auth, "ABC", "N", merchant_id=iga["id"]).status_code == 400  # both
    assert name_it(client, auth, "ABC", merchant_id=9999).status_code == 404
    assert name_it(client, auth, "   ", "N").status_code in (400, 422)
    assert client.get("/api/merchants/9999", headers=auth).status_code == 404
    with pytest.raises(ValueError):
        clean_name("\x00\x01")


# -- tracking over time ----------------------------------------------------------------


def test_totals_separate_spending_from_money_moved_and_respect_the_period(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_t1", "NETFLIX", "Netflix", "16.99", date="2026-10-01", category="ENTERTAINMENT")
    add_tx(services, "tx_t2", "NETFLIX", "Netflix", "16.99", date="2026-09-01", category="ENTERTAINMENT")
    add_tx(services, "tx_t3", "NETFLIX REFUND", "Netflix", "-5.00", date="2026-09-05", category="ENTERTAINMENT")
    add_tx(services, "tx_t4", "NETFLIX US", "Netflix", "9.99", date="2026-09-06", category="ENTERTAINMENT", currency="USD")
    month = by_name(merchants(client, auth, period="month"), "Netflix")
    assert (month["transactions"], month["spent"], month["outflow"], month["inflow"]) == (1, 16.99, 16.99, 0.0)
    allt = by_name(merchants(client, auth, period="all"), "Netflix")
    assert allt["spent"] == 28.98 and allt["inflow"] == 5.0 and allt["outflow"] == 33.98  # a refund reduces spent
    assert allt["transactions"] == 3 and allt["other_currency_transactions"] == 1  # the USD charge is not mixed in
    mortgage = by_name(merchants(client, auth, period="month"), "Mortgage payment")
    assert (mortgage["spent"], mortgage["outflow"]) == (0.0, 1650.0)  # a loan payment is not "spending"
    payroll = by_name(merchants(client, auth, period="month"), "Payroll")
    assert payroll["spent"] == 0.0 and payroll["inflow"] == 0.0  # September, so not in this month
    assert by_name(merchants(client, auth, period="all"), "Payroll")["inflow"] == 2850.0


def test_the_list_is_ranked_searchable_and_hides_hidden_accounts(seeded, services):
    client, auth = seeded
    rows = merchants(client, auth, period="all")
    spent = [m["spent"] for m in rows]
    assert spent == sorted(spent, reverse=True)
    assert [m["name"] for m in merchants(client, auth, search="le  LOCAL")] == ["Restaurant Le Local"]
    client.patch("/api/accounts/acc_visa", json={"hidden": True}, headers=auth)
    assert by_name(merchants(client, auth, period="all"), "Restaurant Le Local")["transactions"] == 0
    assert client.get("/api/merchants?period=weekly", headers=auth).status_code == 422


def test_detail_has_a_zero_filled_monthly_series_and_the_raw_names(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_e1", "COFFEE 1", "Coffee House", "4.00", date="2026-10-01", category="FOOD_AND_DRINK")
    add_tx(services, "tx_e2", "COFFEE 2", "Coffee House", "6.00", date="2026-08-15", category="FOOD_AND_DRINK")
    cid = by_name(merchants(client, auth), "Coffee House")["id"]
    detail = client.get(f"/api/merchants/{cid}?months=4", headers=auth).json()
    assert [(m["month"], m["total"], m["transactions"]) for m in detail["monthly"]] == [
        ("2026-07", 0.0, 0), ("2026-08", 6.0, 1), ("2026-09", 0.0, 0), ("2026-10", 4.0, 1)]
    assert detail["spent"] == 10.0 and detail["average_transaction"] == 5.0
    assert detail["lifetime"]["transactions"] == 2
    assert detail["alias_list"][0]["pattern"] == "Coffee House" and detail["alias_list"][0]["source"] == "auto"
    assert [c["category"] for c in detail["categories"]] == ["FOOD_AND_DRINK"] and detail["categories"][0]["amount"] == 10.0
    assert [t["date"] for t in detail["recent_transactions"]] == ["2026-10-01", "2026-08-15"]
    assert client.get(f"/api/merchants/{cid}?months=0", headers=auth).status_code == 422


def test_search_finds_a_transaction_by_its_pretty_name(seeded, services):
    client, auth = seeded
    add_tx(services, "tx_s1", "POS 8812", None, "3.00")
    name_it(client, auth, "POS 8812", "Bakery")
    found = client.get("/api/transactions?search=bak", headers=auth).json()
    assert [t["id"] for t in found["transactions"]] == ["tx_s1"]


# -- sync keeps names up to date ------------------------------------------------------


def test_new_synced_transactions_get_a_merchant(services, plaid, client, auth):
    from decimal import Decimal

    from .test_sync import ACCOUNTS

    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(added=[
        plaid_tx("sx1", Decimal("9.50"), "TIM HORTONS #123", merchant="Tim Hortons"),
        plaid_tx("sx2", Decimal("12.00"), "MYSTERY SHOP 77", merchant=None),
    ])]
    run(services, item)
    assert "Tim Hortons" in {m["name"] for m in merchants(client, auth)}
    assert [t["name"] for t in client.get("/api/merchants/unassigned", headers=auth).json()] == ["MYSTERY SHOP 77"]
    # name the mystery one, then a pending charge that later posts keeps the name
    name_it(client, auth, "MYSTERY SHOP 77", "Hardware Store")
    item = services.db.one("SELECT * FROM items")
    plaid.pages = [page(added=[plaid_tx("sx3", Decimal("30.00"), "MYSTERY SHOP 77", merchant=None, pending=True)], cursor="c2")]
    run(services, item)
    item = services.db.one("SELECT * FROM items")
    plaid.pages = [page(added=[plaid_tx("sx4", Decimal("30.00"), "MYSTERY SHOP 77", merchant=None, pending_id="sx3")],
                        removed=["sx3"], cursor="c3")]
    run(services, item)
    row = client.get("/api/transactions?search=mystery", headers=auth).json()["transactions"]
    assert {t["display_name"] for t in row} == {"Hardware Store"} and {t["id"] for t in row} == {"sx2", "sx4"}
    # removing every transaction of an automatic merchant tidies the merchant away
    item = services.db.one("SELECT * FROM items")
    plaid.pages = [page(removed=["sx1"], cursor="c4")]
    run(services, item)  # a sync that only removes still tidies up
    assert "Tim Hortons" not in {m["name"] for m in merchants(client, auth)}


# -- access -------------------------------------------------------------------------


def test_every_merchant_route_needs_a_token(client):
    for method, path in [
        ("get", "/api/merchants"), ("get", "/api/merchants/unassigned"), ("get", "/api/merchants/1"),
        ("post", "/api/merchants"), ("patch", "/api/merchants/1"), ("delete", "/api/merchants/1"),
        ("post", "/api/merchants/1/merge"), ("post", "/api/merchants/aliases"),
        ("post", "/api/merchants/aliases/preview"), ("delete", "/api/merchants/aliases/1"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


# -- the fixtures for the iPhone app ----------------------------------------------------


def _without_clock(value):
    if isinstance(value, dict):
        return {k: _without_clock(v) for k, v in value.items() if k != "created_at"}
    if isinstance(value, list):
        return [_without_clock(v) for v in value]
    return value


def test_merchant_fixtures_match_the_server(seeded, services):
    from .conftest import assert_matches, fixture_float

    client, auth = seeded
    for i, (d, amt) in enumerate([("2026-08-01", "16.99"), ("2026-09-01", "16.99"), ("2026-10-01", "16.99")]):
        add_tx(services, f"tx_nf{i}", "NETFLIX.COM", "Netflix", amt, date=d, category="ENTERTAINMENT")
    add_tx(services, "tx_pos1", "POS PURCHASE 4521 TORONTO ON", None, "4.50", date="2026-10-01")
    add_tx(services, "tx_pos2", "POS PURCHASE 4521 TORONTO ON", None, "5.25", date="2026-09-20")
    add_tx(services, "tx_et1", "E-TRANSFER SENT 99871", None, "20.00", date="2026-09-25")
    check = lambda name, actual: assert_matches(_without_clock(actual), _without_clock(fixture_float(name)))
    check("merchants_unassigned.json", client.get("/api/merchants/unassigned", headers=auth).json())
    check("merchant_alias_preview.json", client.post("/api/merchants/aliases/preview", headers=auth, json={
        "match_field": "name", "match_type": "contains", "pattern": "POS PURCHASE 4521"}).json())
    check("merchant_assign_response.json", name_it(client, auth, "POS PURCHASE 4521", "Corner Store", kind="contains").json())
    check("merchants.json", client.get("/api/merchants?period=12m", headers=auth).json())
    nid = by_name(merchants(client, auth), "Netflix")["id"]
    check("merchant_detail.json", client.get(f"/api/merchants/{nid}?months=6", headers=auth).json())


def test_a_realistic_volume_is_assigned_quickly(services):
    import time

    from .conftest import seed_fixture_data

    seed_fixture_data(services)
    rows = [
        (f"big{i}", "acc_chq", "2026-09-01", f"SHOP {i % 1500} TORONTO", f"Shop {i % 1500}" if i % 3 else None,
         "12.34", "CAD", 0, "OTHER", "2026-10-02T00:00:00Z")
        for i in range(6000)
    ]
    with services.db.transaction():
        for r in rows:
            services.db.run(
                "INSERT INTO transactions(id, account_id, date, name, merchant_name, amount, currency, pending, "
                "plaid_category, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", r)
    started = time.time()
    assign_merchants(services.db)
    first = time.time() - started
    started = time.time()
    assign_merchants(services.db)  # what happens on every server start afterwards
    again = time.time() - started
    assert first < 10 and again < 3, (first, again)
    assert services.db.one("SELECT count(*) AS n FROM merchants")["n"] >= 1000  # a third have no Plaid name


# -- a saved category on the merchant -------------------------------------------------


def category_of(client, auth, tx_id):
    rows = client.get("/api/transactions?limit=1000", headers=auth).json()["transactions"]
    return next(t for t in rows if t["id"] == tx_id)


def test_a_merchants_saved_category_applies_to_its_transactions(seeded, services):
    client, auth = seeded
    iga = by_name(merchants(client, auth, period="all"), "IGA")
    assert iga["category"] is None
    r = client.patch(f"/api/merchants/{iga['id']}", json={"category": "groceries"}, headers=auth)
    assert r.status_code == 200 and r.json()["category"] == "GROCERIES" and r.json()["name"] == "IGA"
    tx = category_of(client, auth, "tx_000")
    assert (tx["category"], tx["category_source"], tx["rule_id"], tx["category_overridden"]) == ("GROCERIES", "merchant", None, False)
    # Future transactions follow too.
    add_tx(services, "tx_new", "IGA #2", "IGA", 5)
    from app.rules import assign_rules
    assign_rules(services.db, ["tx_new"])
    assert category_of(client, auth, "tx_new")["category"] == "GROCERIES"
    # Clearing it goes back to the bank's category.
    client.patch(f"/api/merchants/{iga['id']}", json={"category": None}, headers=auth)
    tx = category_of(client, auth, "tx_000")
    assert tx["category"] == tx["plaid_category"] and tx["category_source"] == "plaid"


def test_a_rule_and_a_manual_change_beat_the_merchant_category(seeded):
    client, auth = seeded
    iga = by_name(merchants(client, auth, period="all"), "IGA")
    client.patch(f"/api/merchants/{iga['id']}", json={"category": "GROCERIES"}, headers=auth)
    rule = client.post("/api/rules", json={"match_field": "merchant_name", "match_type": "equals", "pattern": "IGA", "category": "PANTRY", "apply_to_past": True}, headers=auth).json()
    tx = category_of(client, auth, "tx_000")
    assert (tx["category"], tx["category_source"], tx["rule_id"]) == ("PANTRY", "rule", rule["id"])
    client.delete(f"/api/rules/{rule['id']}", headers=auth)
    assert category_of(client, auth, "tx_000")["category"] == "GROCERIES"  # falls back to the merchant's
    client.patch("/api/transactions/tx_000", json={"category": "TREATS"}, headers=auth)
    assert category_of(client, auth, "tx_000")["category"] == "TREATS"


def test_a_category_can_be_set_when_naming_or_adding_a_merchant(seeded):
    client, auth = seeded
    r = client.post("/api/merchants", json={"name": "Corner Store", "category": "Snacks"}, headers=auth)
    assert r.status_code == 201 and r.json()["category"] == "SNACKS"
    r = name_it(client, auth, "STORE", "Corner Store")
    assert r.status_code == 201
    assert by_name(merchants(client, auth, period="all"), "Corner Store")["category"] == "SNACKS"  # not sent: kept
    body = {"match_field": "name", "match_type": "equals", "pattern": "STORE", "name": "Corner Store", "category": "Treats"}
    client.post("/api/merchants/aliases", json=body, headers=auth)
    assert by_name(merchants(client, auth, period="all"), "Corner Store")["category"] == "TREATS"


def test_editing_a_merchant_needs_something_and_checks_the_category(seeded):
    client, auth = seeded
    iga = by_name(merchants(client, auth, period="all"), "IGA")
    assert client.patch(f"/api/merchants/{iga['id']}", json={}, headers=auth).status_code == 400
    assert client.patch(f"/api/merchants/{iga['id']}", json={"category": "bad!!"}, headers=auth).status_code == 400
    assert client.patch(f"/api/merchants/{iga['id']}", json={"name": "IGA Market"}, headers=auth).json()["name"] == "IGA Market"


def test_merging_keeps_a_category(seeded):
    client, auth = seeded
    rows = merchants(client, auth, period="all")
    iga, store = by_name(rows, "IGA"), by_name(rows, "Store")
    client.patch(f"/api/merchants/{iga['id']}", json={"category": "GROCERIES"}, headers=auth)
    client.post(f"/api/merchants/{iga['id']}/merge", json={"into_id": store["id"]}, headers=auth)
    assert by_name(merchants(client, auth, period="all"), "Store")["category"] == "GROCERIES"


def test_cash_flow_names_the_merchant_as_the_source(seeded):
    client, auth = seeded
    iga = by_name(merchants(client, auth, period="all"), "IGA")
    client.patch(f"/api/merchants/{iga['id']}", json={"category": "GROCERIES"}, headers=auth)
    detail = client.get("/api/cashflow/transactions?period_start=2026-10-01&period_end=2026-10-31&range=all", headers=auth).json()
    rows = [t for t in detail["transactions"] + detail["left_out"] if t["id"] == "tx_000"]
    assert rows and rows[0]["category"] == "GROCERIES" and rows[0]["category_source"] == "merchant"
    assert category_of(client, auth, "tx_000")["category_source"] == "merchant"


# -- money coming in from a merchant counts as income when it is switched on ----------


def cash_flow_row(client, auth, tx_id):
    detail = client.get("/api/cashflow/transactions?period_start=2026-10-01&period_end=2026-10-31&range=all", headers=auth).json()
    return next(t for t in detail["transactions"] + detail["left_out"] if t["id"] == tx_id)


def test_income_switch_defaults_off_and_changes_nothing(seeded):
    client, auth = seeded
    rows = merchants(client, auth, period="all")
    assert all(m["income"] is False for m in rows)
    before = category_of(client, auth, "tx_000")
    assert before["category_source"] == "plaid"


def test_income_switch_makes_money_in_income_but_not_money_out(seeded, services):
    client, auth = seeded
    add_tx(services, "pay_in", "ACME PAYROLL", "Acme Corp", -2000, category="OTHER")
    add_tx(services, "pay_out", "ACME PAYROLL REVERSAL", "Acme Corp", 50, category="OTHER")
    acme = by_name(merchants(client, auth, period="all"), "Acme Corp")
    r = client.patch(f"/api/merchants/{acme['id']}", json={"income": True}, headers=auth)
    assert r.status_code == 200 and r.json()["income"] is True
    money_in = category_of(client, auth, "pay_in")
    assert (money_in["category"], money_in["category_source"], money_in["rule_id"]) == ("INCOME", "merchant", None)
    assert cash_flow_row(client, auth, "pay_in")["side"] == "income"
    assert category_of(client, auth, "pay_out")["category"] == "OTHER"  # money going out keeps its category
    client.patch(f"/api/merchants/{acme['id']}", json={"income": False}, headers=auth)
    assert category_of(client, auth, "pay_in")["category"] == "OTHER"


def test_money_back_in_a_spending_category_reduces_expenses_not_income(seeded, services):
    client, auth = seeded
    add_tx(services, "claim", "BENEFITS CLAIM", "Sun Benefits", -80, category="OTHER")
    sun = by_name(merchants(client, auth, period="all"), "Sun Benefits")
    assert cash_flow_row(client, auth, "claim")["side"] == "income"  # Plaid called it Other
    client.patch(f"/api/merchants/{sun['id']}", json={"category": "MEDICAL"}, headers=auth)  # switch left off
    row = cash_flow_row(client, auth, "claim")
    assert row["side"] == "expense" and row["value"] == -80  # a negative expense: it reduces what you spent


def test_a_rule_and_a_manual_change_beat_the_income_switch(seeded, services):
    client, auth = seeded
    add_tx(services, "pay2", "ACME PAYROLL", "Acme Corp", -1000, category="OTHER")
    acme = by_name(merchants(client, auth, period="all"), "Acme Corp")
    client.patch(f"/api/merchants/{acme['id']}", json={"income": True}, headers=auth)
    client.patch("/api/transactions/pay2", json={"category": "MEDICAL"}, headers=auth)
    assert category_of(client, auth, "pay2")["category"] == "MEDICAL"


def test_income_switch_can_be_set_when_adding_and_survives_a_merge(seeded):
    client, auth = seeded
    a = client.post("/api/merchants", json={"name": "Employer", "income": True}, headers=auth).json()
    assert a["income"] is True
    b = client.post("/api/merchants", json={"name": "Employer Inc"}, headers=auth).json()
    client.post(f"/api/merchants/{a['id']}/merge", json={"into_id": b["id"]}, headers=auth)
    assert by_name(merchants(client, auth, period="all"), "Employer Inc")["income"] is True
