"""The API reproduces the JSON in api-fixtures/ when loaded with the same data."""

from __future__ import annotations

from .conftest import assert_matches, fixture_float


def test_accounts(seeded):
    client, auth = seeded
    assert_matches(client.get("/api/accounts", headers=auth).json(), fixture_float("accounts.json"))


def test_accounts_exclude_hidden(seeded):
    client, auth = seeded
    client.patch("/api/accounts/acc_tfsa", json={"hidden": True}, headers=auth)
    visible = client.get("/api/accounts?include_hidden=false", headers=auth).json()
    assert "acc_tfsa" not in [a["id"] for a in visible]
    assert len(client.get("/api/accounts", headers=auth).json()) == 4


def test_account_patch_has_null_institution(seeded):
    client, auth = seeded
    body = client.patch("/api/accounts/acc_chq", json={"hidden": True}, headers=auth).json()
    assert body["institution"] is None and body["hidden"] is True


def test_transactions(seeded):
    client, auth = seeded
    body = client.get("/api/transactions?limit=5", headers=auth).json()
    expected = fixture_float("transactions.json")
    assert body["total"] == expected["total"] == 10
    assert (body["limit"], body["offset"]) == (5, 0)
    assert_matches(body["transactions"], expected["transactions"])


def test_transaction_pagination_and_filters(seeded):
    client, auth = seeded
    page2 = client.get("/api/transactions?limit=5&offset=5", headers=auth).json()
    assert len(page2["transactions"]) == 5
    assert client.get("/api/transactions?account_id=acc_chq", headers=auth).json()["total"] == 4
    assert client.get("/api/transactions?search=iga", headers=auth).json()["total"] == 1
    assert client.get("/api/transactions?category=income", headers=auth).json()["total"] == 1
    window = client.get("/api/transactions?start=2026-09-29&end=2026-09-30", headers=auth).json()
    assert [t["id"] for t in window["transactions"]] == ["tx_001", "tx_002"]


def test_search_treats_percent_literally(seeded):
    client, auth = seeded
    assert client.get("/api/transactions?search=%25", headers=auth).json()["total"] == 0


def test_patch_transaction_matches_fixture(seeded):
    client, auth = seeded
    body = client.patch("/api/transactions/tx_001", json={"category": "groceries"}, headers=auth).json()
    assert_matches(body, fixture_float("transaction_patch_response.json"))
    assert body["category_source"] == "manual"


def test_patch_null_resets_to_plaid(seeded):
    client, auth = seeded
    client.patch("/api/transactions/tx_001", json={"category": "GROCERIES"}, headers=auth)
    body = client.patch("/api/transactions/tx_001", json={"category": None}, headers=auth).json()
    assert body["category"] == "FOOD_AND_DRINK" and body["category_overridden"] is False


def test_categories(seeded):
    client, auth = seeded
    client.patch("/api/transactions/tx_001", json={"category": "GROCERIES"}, headers=auth)
    assert client.get("/api/categories", headers=auth).json() == fixture_float("categories.json")


def test_spending(seeded):
    client, auth = seeded
    assert_matches(client.get("/api/spending?month=2026-10", headers=auth).json(), fixture_float("spending.json"))


def test_spending_excludes_transfers_loans_and_income(seeded):
    client, auth = seeded
    body = client.get("/api/spending?month=2026-09", headers=auth).json()
    names = [c["category"] for c in body["categories"]]
    assert "INCOME" not in names and "LOAN_PAYMENTS" not in names
    assert body["total"] == 485.95


def test_spending_monthly(seeded):
    client, auth = seeded
    body = client.get("/api/spending/monthly?months=6", headers=auth).json()
    assert_matches(body, fixture_float("spending_monthly.json"))


def test_budgets(seeded):
    client, auth = seeded
    assert_matches(client.get("/api/budgets?month=2026-10", headers=auth).json(), fixture_float("budgets.json"))


def test_budget_put_matches_fixture(seeded):
    client, auth = seeded
    body = client.put("/api/budgets/travel", json={"monthly_limit": 200}, headers=auth).json()
    assert_matches(body, fixture_float("budget_put_response.json"))
    assert client.put("/api/budgets/TRAVEL", json={"monthly_limit": 250}, headers=auth).json()["monthly_limit"] == 250.0


def test_budget_limit_must_be_positive(seeded):
    client, auth = seeded
    response = client.put("/api/budgets/TRAVEL", json={"monthly_limit": 0}, headers=auth)
    assert response.status_code == 422 and isinstance(response.json()["detail"], str)


def test_budget_delete(seeded):
    client, auth = seeded
    assert client.delete("/api/budgets/TRANSPORTATION", headers=auth).status_code == 200
    assert client.delete("/api/budgets/TRANSPORTATION", headers=auth).status_code == 404


def test_over_budget_percent_can_exceed_100(seeded):
    client, auth = seeded
    client.put("/api/budgets/FOOD_AND_DRINK", json={"monthly_limit": 100}, headers=auth)
    entry = [b for b in client.get("/api/budgets?month=2026-10", headers=auth).json()["budgets"]
             if b["category"] == "FOOD_AND_DRINK"][0]
    assert entry["percent"] == 132.5 and entry["remaining"] == -32.47
    assert client.get("/api/summary", headers=auth).json()["over_budget"][0]["category"] == "FOOD_AND_DRINK"


def test_holdings(seeded):
    client, auth = seeded
    assert_matches(client.get("/api/holdings", headers=auth).json(), fixture_float("holdings.json"))


def test_networth(seeded):
    client, auth = seeded
    body = client.get("/api/networth?days=36500", headers=auth).json()
    assert_matches(body, fixture_float("networth.json"))
    assert body[0]["net_worth"] < 0  # a mortgage makes net worth negative


def test_items(seeded):
    client, auth = seeded
    assert_matches(client.get("/api/items", headers=auth).json(), fixture_float("items.json"))


def test_summary(seeded):
    client, auth = seeded
    assert_matches(client.get("/api/summary", headers=auth).json(), fixture_float("summary.json"))


def test_bad_month_matches_error_fixture(seeded):
    client, auth = seeded
    response = client.get("/api/budgets?month=oct", headers=auth)
    expected = fixture_float("error_400.json")
    assert response.status_code == expected["status"]
    assert response.json() == expected["body"]


def test_missing_credentials_error_shape(seeded):
    client, _ = seeded
    response = client.get("/api/summary")
    assert response.status_code == fixture_float("error_401.json")["status"]
    assert isinstance(response.json()["detail"], str)


def test_cashflow(seeded):
    client, auth = seeded
    body = client.get("/api/cashflow?range=custom&start=2026-09-01&end=2026-10-02&group=week", headers=auth).json()
    assert_matches(body, fixture_float("cashflow.json"))


def test_cashflow_transactions(seeded):
    client, auth = seeded
    body = client.get("/api/cashflow/transactions?period_start=2026-09-28&period_end=2026-10-02", headers=auth).json()
    assert_matches(body, fixture_float("cashflow_transactions.json"))


def test_search_matches_any_part_of_a_transaction(seeded):
    client, auth = seeded

    def names(q):
        found = client.get("/api/transactions", params={"search": q, "limit": 50}, headers=auth).json()
        return [t["name"] for t in found["transactions"]]

    assert names("132.47") == ["IGA"]                          # an amount
    assert names("$132.47") == ["IGA"]                         # typed with the dollar sign
    assert names("-132.47") == ["IGA"]                         # shown as money that left
    assert names("+132.47") == []                              # nothing came in for that amount
    assert names("+2,850") == ["Payroll"]                      # the deposit
    assert "Payroll" in names("2026-09-29")                    # a date (the date posted or authorized)
    on_visa = names("9876")                                    # an account's last digits: everything on that card
    assert "Restaurant Le Local" in on_visa and "Payroll" not in on_visa
    assert set(on_visa) < set(names("visa")) | {"Visa payment"} and "Visa payment" in names("visa")  # its name, or the bank's text
    assert names("food and drink") == ["IGA", "Restaurant Le Local"]  # the category, spelled out
    assert names("food 64.20") == ["Restaurant Le Local"]      # every word has to match
    assert names("pending") == ["IGA"]                         # the word pending
    assert names("desjardins mortgage") == ["Mortgage payment"]  # the bank plus the text
