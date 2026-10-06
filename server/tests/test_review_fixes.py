"""Regression tests for problems found in review of the server."""

from __future__ import annotations

import json
from decimal import Decimal

from app.webhook import KEY_CACHE_SECONDS

from .test_sync import ACCOUNTS, add_item, page, run, tx
from .test_webhook import EVENT, hook, post, queued  # noqa: F401  (fixture re-export)


def test_budget_delete_and_category_filter_use_the_same_name_cleanup(seeded):
    client, auth = seeded
    assert client.put("/api/budgets/Eating Out", json={"monthly_limit": 80}, headers=auth).json()["category"] == "EATING_OUT"
    client.patch("/api/transactions/tx_000", json={"category": "Eating Out"}, headers=auth)
    for spelling in ("Eating Out", "eating out", "EATING_OUT"):
        found = client.get("/api/transactions", params={"category": spelling}, headers=auth).json()
        assert [t["id"] for t in found["transactions"]] == ["tx_000"], spelling
    assert client.delete("/api/budgets/Eating Out", headers=auth).status_code == 200
    assert client.delete("/api/budgets/Eating Out", headers=auth).status_code == 404
    assert client.delete("/api/budgets/bad;name", headers=auth).status_code == 404
    assert client.get("/api/transactions", params={"category": "bad;name"}, headers=auth).json()["total"] == 0


def test_transaction_for_an_unlisted_account_is_kept(services, plaid, client, auth):
    item = add_item(services)
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(added=[tx("lost", Decimal("9.99"), account="acc_not_listed")], cursor="c9")]
    result = run(services, item)
    assert result["ok"] is True and result["transactions_changed"] == 1
    assert services.db.one("SELECT cursor FROM items")["cursor"] == "c9"  # it will not be re-sent
    assert services.db.one("SELECT amount FROM transactions WHERE id='lost'")["amount"] == "9.99"
    names = {a["id"]: a["name"] for a in client.get("/api/accounts", headers=auth).json()}
    assert names["acc_not_listed"] == "Unknown account"


def test_one_banks_unexpected_failure_does_not_stop_the_others(services, plaid, client, auth):
    add_item(services, "item_a", "Bank A")
    add_item(services, "item_b", "Bank B")
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(cursor="c1")]
    original, calls = plaid.accounts_get, []

    def flaky(token):
        calls.append(1)
        if len(calls) == 1:
            raise KeyError("surprising payload")  # not a PlaidError
        return original(token)

    plaid.accounts_get = flaky
    response = client.post("/api/sync", headers=auth)
    assert response.status_code == 200  # not a 500
    first, second = response.json()
    assert first["ok"] is False and "Unexpected" in first["error"] and "surprising" not in first["error"]
    assert second["ok"] is True
    assert services.db.one("SELECT error_code FROM items WHERE id='item_a'")["error_code"] == "INTERNAL_ERROR"
    assert services.db.one("SELECT needs_reauth FROM items WHERE id='item_a'")["needs_reauth"] == 0


def test_cached_webhook_keys_expire(hook, clock):
    client, signer, services = hook
    body = json.dumps(EVENT).encode()
    assert post(client, body, signer.sign(body)).status_code == 200
    assert post(client, body, signer.sign(body)).status_code == 200
    assert signer.fetches == 1  # cached
    signer.jwk["expired_at"] = int(clock()) + 5  # Plaid retires the key a little later
    clock.advance(KEY_CACHE_SECONDS + 60)
    assert post(client, body, signer.sign(body)).status_code == 401  # re-fetched, now seen as expired
    assert signer.fetches == 2


def test_webhook_timestamps_from_the_future_are_refused(hook, clock):
    client, signer, services = hook
    body = json.dumps(EVENT).encode()
    assert post(client, body, signer.sign(body, iat=clock() + 30)).status_code == 200  # small clock skew is fine
    assert post(client, body, signer.sign(body, iat=clock() + 600)).status_code == 401
