"""Categories and sub-categories you add yourself (Settings > Categories)."""

from __future__ import annotations



def add(services, tx_id, amount, detailed, primary="FOOD_AND_DRINK", date="2026-10-03", **extra):
    services.db.run(
        "INSERT INTO transactions(id, account_id, date, name, merchant_name, amount, currency, pending, "
        "plaid_category, category_detailed, override_category, rule_category, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tx_id, "acc_visa", date, tx_id, None, str(amount), "CAD", 0, primary, detailed,
         extra.get("override"), extra.get("rule"), "2026-10-03T00:00:00Z"),
    )


def make(client, auth, name, parent=None):
    return client.post("/api/categories", json={"name": name, "parent": parent}, headers=auth)


def test_add_a_category_and_a_sub_category(services, seeded):
    client, auth = seeded
    r = make(client, auth, "Pets")
    assert r.status_code == 201 and r.json()["code"] == "PETS" and r.json()["parent"] is None
    r = make(client, auth, "Vet bills", "PETS")
    assert r.status_code == 201 and r.json()["code"] == "PETS_VET_BILLS" and r.json()["parent"] == "PETS"
    r = make(client, auth, "Coffee", "FOOD_AND_DRINK")  # under a built-in one
    assert r.json()["code"] == "FOOD_AND_DRINK_COFFEE"
    body = client.get("/api/categories/custom", headers=auth).json()
    assert {c["code"] for c in body["categories"]} == {"PETS", "PETS_VET_BILLS", "FOOD_AND_DRINK_COFFEE"}
    assert "PETS" in body["primaries"] and "FOOD_AND_DRINK" in body["primaries"]
    codes = client.get("/api/categories", headers=auth).json()
    assert "PETS" in codes and "PETS_VET_BILLS" in codes


def test_rejects_bad_overlapping_and_duplicate_names(services, seeded):
    client, auth = seeded
    assert make(client, auth, "   ").status_code == 400
    assert make(client, auth, "Food").status_code == 400  # would swallow FOOD_AND_DRINK
    assert make(client, auth, "Food and drink extras").status_code == 400  # starts with a built-in's code
    assert make(client, auth, "Travel").status_code == 409  # built-in already
    assert make(client, auth, "Coffee", "NOT_A_TOP_LEVEL").status_code == 400
    assert make(client, auth, "Pets").status_code == 201
    assert make(client, auth, "pets").status_code == 409
    assert make(client, auth, "Vet", "PETS").status_code == 201
    assert make(client, auth, "Vet", "PETS").status_code == 409
    assert make(client, auth, "Cheap", "PETS_VET").status_code == 400  # no sub-sub-categories


def test_a_custom_sub_category_counts_in_its_parent_once(services, seeded):
    client, auth = seeded
    make(client, auth, "Pets")
    make(client, auth, "Vet", "PETS")
    add(services, "p1", "60.00", None, primary="PETS", override="PETS_VET")
    add(services, "p2", "15.00", None, primary="PETS", override="PETS")
    body = client.get("/api/spending?month=2026-10", headers=auth).json()
    pets = next(c for c in body["categories"] if c["category"] == "PETS")
    assert pets["spent"] == 75 and pets["subcategories"] == [{"category": "PETS_VET", "spent": 60.0}]
    total = sum(c["spent"] for c in body["categories"])
    assert body["total"] == total
    # Filtering by the top-level category includes its sub-categories; the sub-category only itself.
    both = client.get("/api/transactions?category=PETS", headers=auth).json()
    only = client.get("/api/transactions?category=PETS_VET", headers=auth).json()
    assert both["total"] == 2 and only["total"] == 1


def test_delete_only_when_unused(services, seeded):
    client, auth = seeded
    make(client, auth, "Pets")
    make(client, auth, "Vet", "PETS")
    assert client.delete("/api/categories/PETS", headers=auth).status_code == 409  # has a sub-category
    add(services, "p1", "60.00", None, primary="PETS", override="PETS_VET")
    r = client.delete("/api/categories/PETS_VET", headers=auth)
    assert r.status_code == 409 and "1 transaction(s)" in r.json()["detail"]
    assert client.put("/api/budgets/PETS_VET", json={"monthly_limit": 50}, headers=auth).status_code == 200
    services.db.run("DELETE FROM transactions WHERE id = 'p1'")
    assert client.delete("/api/categories/PETS_VET", headers=auth).status_code == 409  # a budget uses it
    client.delete("/api/budgets/PETS_VET", headers=auth)
    assert client.delete("/api/categories/PETS_VET", headers=auth).status_code == 204
    assert client.delete("/api/categories/PETS", headers=auth).status_code == 204
    assert client.delete("/api/categories/FOOD_AND_DRINK", headers=auth).status_code == 404  # built-in
    assert client.get("/api/categories/custom", headers=auth).json()["categories"] == []


def test_requires_login(services, seeded):
    client, _ = seeded
    assert client.post("/api/categories", json={"name": "x"}).status_code == 401
    assert client.get("/api/categories/custom").status_code == 401


def test_overlap_with_an_existing_sub_category_and_restart(services, seeded):
    from app.api import create_api_app
    from app.rules import parent_category

    client, auth = seeded
    make(client, auth, "Pets")
    make(client, auth, "Vet", "PETS")
    services.db.run("DELETE FROM custom_categories WHERE code = 'PETS'")  # a leftover sub-category
    create_api_app(services)  # a restart reloads the categories you added
    assert make(client, auth, "Pets").status_code == 400  # PETS_VET would turn into a sub of it
    services.db.run("INSERT INTO custom_categories(code, parent, created_at) VALUES ('PETS', NULL, 'x')")
    create_api_app(services)
    assert parent_category("PETS_VET") == "PETS"
