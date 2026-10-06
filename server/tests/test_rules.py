from __future__ import annotations

from decimal import Decimal

from app.rules import normalize_category

from .test_sync import ACCOUNTS, add_item, page, run, tx


def make_rule(client, auth, **over):
    body = {"match_field": "merchant_name", "match_type": "equals", "pattern": "IGA",
            "category": "GROCERIES", **over}
    response = client.post("/api/rules", json=body, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


def category_of(client, auth, tx_id="tx_000"):
    rows = client.get("/api/transactions?limit=1000", headers=auth).json()["transactions"]
    return next(t for t in rows if t["id"] == tx_id)


def test_rule_applies_to_past_on_request_only(seeded):
    client, auth = seeded
    rule = make_rule(client, auth)
    assert category_of(client, auth)["category"] == "FOOD_AND_DRINK"  # not yet applied
    applied = client.post(f"/api/rules/{rule['id']}/apply", headers=auth).json()
    assert applied == {"updated_transactions": 1}
    row = category_of(client, auth)
    assert (row["category"], row["category_source"], row["rule_id"]) == ("GROCERIES", "rule", rule["id"])
    assert row["category_overridden"] is False and row["plaid_category"] == "FOOD_AND_DRINK"


def test_apply_to_past_flag_on_create(seeded):
    client, auth = seeded
    response = client.post("/api/rules", headers=auth, json={
        "match_field": "name", "match_type": "contains", "pattern": "iga", "category": "groceries",
        "apply_to_past": True})
    assert response.json()["updated_transactions"] == 1 and response.json()["category"] == "GROCERIES"


def test_manual_beats_rule_and_reset_falls_back_to_rule(seeded):
    client, auth = seeded
    make_rule(client, auth, apply_to_past=True)
    client.patch("/api/transactions/tx_000", json={"category": "TREATS"}, headers=auth)
    assert category_of(client, auth)["category_source"] == "manual"
    client.patch("/api/transactions/tx_000", json={"category": None}, headers=auth)
    assert category_of(client, auth)["category"] == "GROCERIES"  # rule, then Plaid


def test_deleting_a_rule_goes_back_to_plaid(seeded):
    client, auth = seeded
    rule = make_rule(client, auth, apply_to_past=True)
    assert client.delete(f"/api/rules/{rule['id']}", headers=auth).status_code == 200
    row = category_of(client, auth)
    assert (row["category"], row["category_source"]) == ("FOOD_AND_DRINK", "plaid")
    assert client.delete(f"/api/rules/{rule['id']}", headers=auth).status_code == 404


def test_deleting_a_rule_falls_back_to_the_next_matching_rule(seeded):
    client, auth = seeded
    broad = make_rule(client, auth, match_type="contains", pattern="IG", category="BROAD")
    exact = make_rule(client, auth, category="EXACT")
    client.post(f"/api/rules/{exact['id']}/apply", headers=auth)
    assert category_of(client, auth)["category"] == "EXACT"  # equals beats contains
    client.delete(f"/api/rules/{exact['id']}", headers=auth)
    assert category_of(client, auth)["category"] == "BROAD"
    assert broad["id"] != exact["id"]


def test_longest_contains_pattern_wins_then_newest(seeded):
    client, auth = seeded
    make_rule(client, auth, match_type="contains", pattern="IG", category="SHORT")
    make_rule(client, auth, match_type="contains", pattern="IGA", category="LONG")
    make_rule(client, auth, match_type="contains", pattern="iga", category="NEWEST_SAME_LENGTH")
    client.post("/api/rules", headers=auth, json={
        "match_field": "name", "match_type": "contains", "pattern": "x", "category": "X", "apply_to_past": True})
    assert category_of(client, auth)["category"] == "NEWEST_SAME_LENGTH"  # longest ties, newest wins


def test_null_merchant_name_never_matches(seeded, services):
    client, auth = seeded
    services.db.run("UPDATE transactions SET merchant_name=NULL WHERE id='tx_000'")
    make_rule(client, auth, match_type="contains", pattern="IGA", apply_to_past=True)
    assert category_of(client, auth)["category_source"] == "plaid"
    make_rule(client, auth, match_field="name", pattern="IGA", apply_to_past=True)
    assert category_of(client, auth)["category_source"] == "rule"  # name still matches


def test_rule_scoped_to_an_account(seeded):
    client, auth = seeded
    make_rule(client, auth, account_id="acc_visa", apply_to_past=True)
    assert category_of(client, auth)["category_source"] == "plaid"  # IGA is on chequing
    assert client.post("/api/rules", headers=auth, json={
        "match_field": "name", "match_type": "equals", "pattern": "x", "category": "X",
        "account_id": "nope"}).status_code == 400


def test_editing_a_rule_updates_its_transactions(seeded):
    client, auth = seeded
    rule = make_rule(client, auth, apply_to_past=True)
    patched = client.patch(f"/api/rules/{rule['id']}", json={"category": "Supermarket"}, headers=auth)
    assert patched.json()["category"] == "SUPERMARKET"
    assert category_of(client, auth)["category"] == "SUPERMARKET"
    client.patch(f"/api/rules/{rule['id']}", json={"pattern": "NOT IGA"}, headers=auth)
    assert category_of(client, auth)["category_source"] == "plaid"  # no longer matches


def test_preview_counts_and_budget_warnings(seeded):
    client, auth = seeded
    preview = client.post("/api/rules/preview", headers=auth, json={
        "match_field": "name", "match_type": "contains", "pattern": "mortgage", "category": "HOUSING"}).json()
    assert preview == {"matching_transactions": 1, "would_change": 1, "skipped_manual": 0,
                       "moves_into_excluded": 0, "moves_out_of_excluded": 1}  # LOAN_PAYMENTS -> HOUSING
    into = client.post("/api/rules/preview", headers=auth, json={
        "match_field": "merchant_name", "match_type": "equals", "pattern": "IGA",
        "category": "TRANSFER_OUT"}).json()
    assert into["moves_into_excluded"] == 1
    client.patch("/api/transactions/tx_000", json={"category": "MINE"}, headers=auth)
    skipped = client.post("/api/rules/preview", headers=auth, json={
        "match_field": "merchant_name", "match_type": "equals", "pattern": "IGA", "category": "X"}).json()
    assert skipped["skipped_manual"] == 1 and skipped["would_change"] == 0
    assert client.get("/api/rules", headers=auth).json() == []  # preview saves nothing


def test_new_transactions_get_rules_during_sync(services, plaid, client, auth):
    make_rule(client, auth)
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(added=[tx("n1", Decimal("5.00"))])]
    run(services, item)
    rows = client.get("/api/transactions", headers=auth).json()["transactions"]
    assert (rows[0]["category"], rows[0]["category_source"]) == ("GROCERIES", "rule")


def test_category_names_are_normalized_and_validated(client, auth):
    assert normalize_category("eating  out") == "EATING_OUT"
    for bad in ("", "   ", "x" * 61, "drop;table"):
        try:
            normalize_category(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")
    assert client.put("/api/budgets/bad;name", json={"monthly_limit": 5}, headers=auth).status_code == 400


def test_rule_bodies_are_validated(client, auth):
    for body in (
        {"match_field": "amount", "match_type": "equals", "pattern": "x", "category": "X"},
        {"match_field": "name", "match_type": "regex", "pattern": "x", "category": "X"},
        {"match_field": "name", "match_type": "equals", "pattern": "", "category": "X"},
    ):
        response = client.post("/api/rules", json=body, headers=auth)
        assert response.status_code == 422 and isinstance(response.json()["detail"], str)
