"""Sub-categories: Food & Drink > Fast Food. Money counts in both, totals count it once."""

from __future__ import annotations

from app.rules import is_excluded, parent_category, split_category

FAST = "FOOD_AND_DRINK_FAST_FOOD"


def add(services, tx_id, amount, detailed, primary="FOOD_AND_DRINK", date="2026-10-03", **extra):
    services.db.run(
        "INSERT INTO transactions(id, account_id, date, name, merchant_name, amount, currency, pending, "
        "plaid_category, category_detailed, override_category, rule_category, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tx_id, "acc_visa", date, tx_id, None, str(amount), "CAD", 0, primary, detailed,
         extra.get("override"), extra.get("rule"), "2026-10-03T00:00:00Z"),
    )


def food(client, auth):
    body = client.get("/api/spending?month=2026-10", headers=auth).json()
    return body, next(c for c in body["categories"] if c["category"] == "FOOD_AND_DRINK")


def test_parent_detection():
    assert parent_category(FAST) == "FOOD_AND_DRINK"
    assert parent_category("FOOD_AND_DRINK") is None  # a top-level category has no parent
    assert parent_category("GROCERIES") is None  # a custom one neither
    assert parent_category("FOOD_AND_DRINKS_X") is None  # must be the whole word
    assert parent_category("TRANSFER_IN_DEPOSIT") == "TRANSFER_IN"
    assert parent_category(None) is None
    assert is_excluded("LOAN_PAYMENTS_MORTGAGE_PAYMENT") and is_excluded("INCOME")
    assert not is_excluded(FAST)


def test_split_category():
    row = lambda **kw: {"override_category": None, "rule_category": None, "plaid_category": "FOOD_AND_DRINK",
                        "category_detailed": FAST, **kw}  # noqa: E731
    assert split_category(row()) == ("FOOD_AND_DRINK", FAST)
    assert split_category(row(category_detailed=None)) == ("FOOD_AND_DRINK", None)
    assert split_category(row(category_detailed="TRAVEL_FLIGHTS")) == ("FOOD_AND_DRINK", None)  # not its own
    assert split_category(row(override_category="GROCERIES")) == ("GROCERIES", None)  # a chosen category wins
    assert split_category(row(override_category="FOOD_AND_DRINK")) == ("FOOD_AND_DRINK", None)
    assert split_category(row(override_category="TRAVEL_FLIGHTS")) == ("TRAVEL", "TRAVEL_FLIGHTS")
    assert split_category(row(rule_category=FAST, plaid_category="OTHER")) == ("FOOD_AND_DRINK", FAST)


def test_spending_shows_sub_categories_without_double_counting(services, seeded):
    client, auth = seeded
    add(services, "f1", "12.00", FAST)
    add(services, "f2", "8.50", FAST)
    add(services, "f3", "40.00", "FOOD_AND_DRINK_RESTAURANT")
    add(services, "f4", "5.00", None)  # Plaid gave no detail: counts in the parent only
    body, cat = food(client, auth)
    assert cat["spent"] == 132.47 + 12 + 8.5 + 40 + 5
    subs = {s["category"]: s["spent"] for s in cat["subcategories"]}
    assert subs == {FAST: 20.5, "FOOD_AND_DRINK_RESTAURANT": 40.0, "FOOD_AND_DRINK_GROCERIES": 132.47}
    assert [s["spent"] for s in cat["subcategories"]] == sorted(subs.values(), reverse=True)
    # The month's total is the sum of the top-level categories: nothing is added a second time.
    assert body["total"] == sum(c["spent"] for c in body["categories"]) == 132.47 + 12 + 8.5 + 40 + 5
    summary = client.get("/api/summary", headers=auth).json()
    assert summary["spent_this_month"] == body["total"]
    assert all("subcategory" in t for t in client.get("/api/transactions", headers=auth).json()["transactions"])


def test_budgets_on_parent_and_sub_both_count_but_total_once(services, seeded):
    client, auth = seeded
    add(services, "f1", "12.00", FAST)
    add(services, "f2", "30.00", "FOOD_AND_DRINK_RESTAURANT")
    for category, limit in [("FOOD_AND_DRINK", 600), (FAST, 10), ("FOOD_AND_DRINK_RESTAURANT", 100)]:
        assert client.put(f"/api/budgets/{category}", json={"monthly_limit": limit}, headers=auth).status_code == 200
    budgets = {b["category"]: b for b in client.get("/api/budgets?month=2026-10", headers=auth).json()["budgets"]}
    assert budgets["FOOD_AND_DRINK"]["spent"] == 132.47 + 12 + 30 and budgets["FOOD_AND_DRINK"]["parent"] is None
    assert budgets[FAST]["spent"] == 12 and budgets[FAST]["parent"] == "FOOD_AND_DRINK"
    assert budgets[FAST]["percent"] == 120.0  # over its own limit, while the parent is fine
    summary = client.get("/api/summary", headers=auth).json()
    # Fixture budgets total 1190 including the 600 Food & Drink one; the two sub budgets sit inside it.
    before = 1190.0
    assert summary["budgeted_this_month"] == before
    assert FAST in [b["category"] for b in summary["over_budget"]]
    # A sub budget whose parent has no budget is its own money, so it does count.
    client.delete("/api/budgets/FOOD_AND_DRINK", headers=auth)
    assert client.get("/api/summary", headers=auth).json()["budgeted_this_month"] == before - 600 + 10 + 100


def test_chosen_sub_category_and_filters(services, seeded):
    client, auth = seeded
    add(services, "f1", "12.00", FAST)
    add(services, "f2", "30.00", "FOOD_AND_DRINK_RESTAURANT")
    ids = lambda q: sorted(t["id"] for t in client.get(f"/api/transactions?{q}&limit=1000", headers=auth).json()["transactions"])  # noqa: E731
    assert ids(f"category={FAST}") == ["f1"]
    assert {"f1", "f2", "tx_000"} <= set(ids("category=FOOD_AND_DRINK"))  # the parent includes its subs
    assert ids("category=food_and_drink_fast_food") == ["f1"]  # same cleanup as elsewhere
    # By hand: move the restaurant meal into Fast Food.
    body = client.patch("/api/transactions/f2", json={"category": FAST}, headers=auth).json()
    assert (body["category"], body["subcategory"], body["category_overridden"]) == (FAST, FAST, True)
    assert ids(f"category={FAST}") == ["f1", "f2"]
    _, cat = food(client, auth)
    assert {s["category"]: s["spent"] for s in cat["subcategories"]}[FAST] == 42.0
    # A custom category is not a sub-category and does not match by prefix.
    client.patch("/api/transactions/f1", json={"category": "FOOD_AND_DRINKING_CLUB"}, headers=auth)
    assert ids("category=FOOD_AND_DRINK") != [] and "f1" not in ids("category=FOOD_AND_DRINK")
    assert ids("category=FOOD_AND_DRINKING_CLUB") == ["f1"]
    assert FAST in client.get("/api/categories", headers=auth).json()


def test_sub_categories_of_transfers_and_debt_stay_out_of_spending(services, seeded):
    client, auth = seeded
    add(services, "t1", "900.00", "TRANSFER_OUT_ACCOUNT_TRANSFER", primary="TRANSFER_OUT")
    add(services, "t2", "50.00", FAST, override="TRANSFER_OUT_SAVINGS")  # moved there by hand
    body = client.get("/api/spending?month=2026-10", headers=auth).json()
    assert [c["category"] for c in body["categories"]] == ["FOOD_AND_DRINK"]
    assert body["total"] == 132.47
    monthly = client.get("/api/spending/monthly?months=1", headers=auth).json()
    assert monthly[0]["total"] == 132.47


def test_a_custom_category_with_a_top_level_prefix_is_a_sub_category(services, seeded):
    client, auth = seeded
    add(services, "c1", "25.00", None, primary="OTHER")
    client.patch("/api/transactions/c1", json={"category": "Travel fund"}, headers=auth)
    body = client.get("/api/spending?month=2026-10", headers=auth).json()
    travel = next(c for c in body["categories"] if c["category"] == "TRAVEL")
    assert travel["spent"] == 25.0 and travel["subcategories"] == [{"category": "TRAVEL_FUND", "spent": 25.0}]
    client.patch("/api/transactions/c1", json={"category": "Income side gig"}, headers=auth)
    assert "INCOME" not in [c["category"] for c in client.get("/api/spending?month=2026-10", headers=auth).json()["categories"]]


def test_a_refund_only_sub_category_can_go_negative(services, seeded):
    client, auth = seeded
    add(services, "r1", "-30.00", "TRAVEL_FLIGHTS", primary="TRAVEL")
    body = client.get("/api/spending?month=2026-10", headers=auth).json()
    travel = next(c for c in body["categories"] if c["category"] == "TRAVEL")
    assert travel["spent"] == -30.0 and travel["subcategories"][0]["spent"] == -30.0
    assert body["total"] == 132.47 - 30.0
