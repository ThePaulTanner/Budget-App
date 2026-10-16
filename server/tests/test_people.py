"""People and account owners: a shared account has several owners but is never counted twice."""

from __future__ import annotations

from app.sync import snapshot_networth

HOUSEHOLD_NET_WORTH = -267649.75  # the fixture accounts: 3200.50 + 42000 - 850.25 - 312000


def add_people(client, auth):
    paul = client.post("/api/people", json={"name": "Paul"}, headers=auth).json()["id"]
    sam = client.post("/api/people", json={"name": "Sam"}, headers=auth).json()["id"]
    return paul, sam


def own(client, auth, account, *people):
    r = client.put(f"/api/accounts/{account}/owners", json={"person_ids": list(people)}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def assign(client, auth):
    """Paul: chequing and the card. Sam: the TFSA. The mortgage is shared."""
    paul, sam = add_people(client, auth)
    own(client, auth, "acc_chq", paul)
    own(client, auth, "acc_visa", paul)
    own(client, auth, "acc_tfsa", sam)
    own(client, auth, "acc_mtg", paul, sam)
    return paul, sam


def worth(client, auth, person=None):
    q = "" if person is None else f"?person_id={person}"
    return client.get(f"/api/summary{q}", headers=auth).json()["net_worth"][0]["net_worth"]


def test_people_crud(seeded):
    client, auth = seeded
    assert client.get("/api/people", headers=auth).json() == []
    made = client.post("/api/people", json={"name": "  Paul  "}, headers=auth)
    assert made.status_code == 201 and made.json() == {"id": made.json()["id"], "name": "Paul", "accounts": 0}
    assert client.post("/api/people", json={"name": "paul"}, headers=auth).status_code == 409
    assert client.post("/api/people", json={"name": "  "}, headers=auth).status_code == 400
    assert client.post("/api/people", json={"name": "x" * 41}, headers=auth).status_code == 400
    pid = made.json()["id"]
    assert client.patch(f"/api/people/{pid}", json={"name": "Paul T"}, headers=auth).json()["name"] == "Paul T"
    assert client.patch("/api/people/999", json={"name": "Z"}, headers=auth).status_code == 404
    sam = client.post("/api/people", json={"name": "Sam"}, headers=auth).json()["id"]
    assert client.patch(f"/api/people/{sam}", json={"name": "paul t"}, headers=auth).status_code == 409
    assert client.delete("/api/people/999", headers=auth).status_code == 404
    assert client.delete(f"/api/people/{sam}", headers=auth).json() == {"ok": True}
    assert [p["name"] for p in client.get("/api/people", headers=auth).json()] == ["Paul T"]


def test_requires_sign_in(client):
    for method, path in [("get", "/api/people"), ("post", "/api/people"), ("put", "/api/accounts/x/owners")]:
        assert getattr(client, method)(path).status_code == 401


def test_owners_are_listed_on_accounts(seeded):
    client, auth = seeded
    paul, sam = assign(client, auth)
    owners = {a["id"]: a["owner_ids"] for a in client.get("/api/accounts", headers=auth).json()}
    assert owners == {"acc_chq": [paul], "acc_visa": [paul], "acc_tfsa": [sam], "acc_mtg": sorted([paul, sam])}
    assert {p["name"]: p["accounts"] for p in client.get("/api/people", headers=auth).json()} == {"Paul": 3, "Sam": 2}
    assert own(client, auth, "acc_mtg", paul)["owner_ids"] == [paul]  # replaces, never adds up
    assert own(client, auth, "acc_mtg")["owner_ids"] == []
    assert client.put("/api/accounts/acc_chq/owners", json={"person_ids": [999]}, headers=auth).status_code == 404
    assert client.put("/api/accounts/nope/owners", json={"person_ids": []}, headers=auth).status_code == 404


def test_a_shared_account_is_counted_once_in_the_household(seeded):
    client, auth = seeded
    assert worth(client, auth) == HOUSEHOLD_NET_WORTH
    paul, sam = assign(client, auth)
    assert worth(client, auth) == HOUSEHOLD_NET_WORTH  # owners change nothing for the household
    assert len(client.get("/api/accounts", headers=auth).json()) == 4
    # Each owner sees the shared mortgage in full; that is why the two do not add up to the household.
    assert worth(client, auth, paul) == 3200.5 - 850.25 - 312000
    assert worth(client, auth, sam) == 42000 - 312000
    assert worth(client, auth, paul) + worth(client, auth, sam) == HOUSEHOLD_NET_WORTH - 312000
    # Making everything shared by both still leaves the household alone.
    for account in ("acc_chq", "acc_visa", "acc_tfsa", "acc_mtg"):
        own(client, auth, account, paul, sam)
    assert worth(client, auth) == HOUSEHOLD_NET_WORTH == worth(client, auth, paul) == worth(client, auth, sam)


def test_people_with_no_accounts_and_unowned_accounts(seeded):
    client, auth = seeded
    paul, sam = add_people(client, auth)
    assert client.get(f"/api/summary?person_id={paul}", headers=auth).json()["net_worth"] == []
    assert client.get(f"/api/accounts?person_id={paul}", headers=auth).json() == []
    own(client, auth, "acc_chq", paul)
    assert [a["id"] for a in client.get(f"/api/accounts?person_id={paul}", headers=auth).json()] == ["acc_chq"]
    assert len(client.get("/api/accounts", headers=auth).json()) == 4  # unowned ones stay in the household


def test_transactions_spending_holdings_and_cards_follow_the_owner(seeded):
    client, auth = seeded
    paul, sam = assign(client, auth)
    everything = client.get("/api/transactions?limit=1000", headers=auth).json()["total"]
    mine = client.get(f"/api/transactions?limit=1000&person_id={paul}", headers=auth).json()["transactions"]
    assert {t["account_id"] for t in mine} <= {"acc_chq", "acc_visa", "acc_mtg"}
    sams = client.get(f"/api/transactions?limit=1000&person_id={sam}", headers=auth).json()["transactions"]
    assert {t["account_id"] for t in sams} <= {"acc_tfsa", "acc_mtg"}
    assert client.get("/api/transactions?limit=1000", headers=auth).json()["total"] == everything  # household unchanged
    house = client.get("/api/spending?month=2026-10", headers=auth).json()["total"]
    assert client.get(f"/api/spending?month=2026-10&person_id={paul}", headers=auth).json()["total"] == house
    assert client.get(f"/api/spending?month=2026-10&person_id={sam}", headers=auth).json()["total"] == 0
    assert [m["total"] for m in client.get(f"/api/spending/monthly?months=1&person_id={sam}", headers=auth).json()] == [0.0]
    held = lambda who: client.get(f"/api/holdings?person_id={who}", headers=auth).json()["holdings"]  # noqa: E731
    assert held(paul) == [] and len(held(sam)) == len(client.get("/api/holdings", headers=auth).json()["holdings"]) > 0
    assert [c["account_id"] for c in client.get(f"/api/payments-due?person_id={paul}", headers=auth).json()["cards"]] == ["acc_visa"]
    assert client.get(f"/api/payments-due?person_id={sam}", headers=auth).json()["cards"] == []
    live = client.get(f"/api/holdings/live?person_id={paul}", headers=auth).json()
    assert live["holdings"] == [] and live["totals"] == {}


def test_a_persons_view_leaves_out_the_households_budgets(seeded):
    client, auth = seeded
    paul, _ = assign(client, auth)
    house = client.get("/api/summary", headers=auth).json()
    assert house["budgeted_this_month"] == 1190.0
    mine = client.get(f"/api/summary?person_id={paul}", headers=auth).json()
    assert mine["budgeted_this_month"] is None and mine["over_budget"] == []
    assert mine["spent_this_month"] == house["spent_this_month"]


def test_an_unknown_person_is_a_404_everywhere(seeded):
    client, auth = seeded
    for path in ["summary", "accounts", "transactions", "spending", "spending/monthly", "holdings",
                 "holdings/live", "networth", "payments-due"]:
        assert client.get(f"/api/{path}?person_id=999", headers=auth).status_code == 404, path


def test_bank_wide_owners(seeded):
    client, auth = seeded
    paul, sam = add_people(client, auth)
    r = client.put("/api/items/item_desj/owners", json={"person_ids": [paul, sam]}, headers=auth).json()
    assert r["accounts"] == 4 and r["owner_ids"] == sorted([paul, sam])
    assert all(a["owner_ids"] == sorted([paul, sam]) for a in client.get("/api/accounts", headers=auth).json())
    assert client.put("/api/items/nope/owners", json={"person_ids": []}, headers=auth).status_code == 404


def test_history_is_kept_per_person_and_the_household_is_never_summed(services, seeded):
    client, auth = seeded
    paul, sam = assign(client, auth)
    snapshot_networth(services.db, "2026-10-02")
    rows = services.db.query("SELECT person_id, net_worth FROM networth WHERE date='2026-10-02' ORDER BY person_id")
    assert [r["person_id"] for r in rows] == [0, *sorted([paul, sam])]
    history = lambda who: client.get(f"/api/networth?days=30{who}", headers=auth).json()  # noqa: E731
    assert history("")[-1]["net_worth"] == HOUSEHOLD_NET_WORTH  # the household line is its own row
    assert history(f"&person_id={sam}")[-1]["net_worth"] == 42000 - 312000
    assert len(history("")) == len({p["date"] for p in history("")})  # one point per day, not one per person
    snapshot_networth(services.db, "2026-10-02")  # re-running overwrites
    assert services.db.one("SELECT count(*) AS n FROM networth WHERE date='2026-10-02'")["n"] == 3
    client.delete(f"/api/people/{sam}", headers=auth)
    assert services.db.one("SELECT count(*) AS n FROM networth WHERE person_id=?", (sam,))["n"] == 0
    assert history("")[-1]["net_worth"] == HOUSEHOLD_NET_WORTH
    assert len(client.get("/api/accounts", headers=auth).json()) == 4  # deleting a person keeps their accounts
    owners = {a["id"]: a["owner_ids"] for a in client.get("/api/accounts", headers=auth).json()}
    assert owners["acc_tfsa"] == [] and owners["acc_mtg"] == [paul]


def test_owners_survive_a_bank_sync(services, plaid, seeded):
    from .test_sync import ACCOUNTS, page, run

    client, auth = seeded
    paul, _ = assign(client, auth)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page()]
    item = services.db.one("SELECT * FROM items WHERE id='item_desj'")
    assert run(services, item)["ok"] is True
    assert {a["id"]: a["owner_ids"] for a in client.get("/api/accounts", headers=auth).json()}["acc_chq"] == [paul]


def test_fixtures_match(seeded):
    from .conftest import assert_matches, fixture_float

    client, auth = seeded
    paul, sam = add_people(client, auth)
    own(client, auth, "acc_chq", paul)
    own(client, auth, "acc_visa", paul)
    own(client, auth, "acc_tfsa", sam)
    shared = client.put("/api/accounts/acc_mtg/owners", json={"person_ids": [paul, sam]}, headers=auth).json()
    assert_matches(shared, fixture_float("account_owners_response.json"))
    assert_matches(client.get("/api/people", headers=auth).json(), fixture_float("people.json"))
