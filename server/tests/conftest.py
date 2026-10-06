"""Shared test setup: a real SQLCipher database, fast password hashing, a fake Plaid."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import api as api_module
from app import sync as sync_module
from app.api import create_api_app
from app.auth import AuthService
from app.bootstrap import build_services
from app.config import Settings
from app.crypto import generate_master_key, seal
from app.plaid import PlaidError

FIXTURES = Path(__file__).resolve().parents[2] / "api-fixtures"
TODAY = "2026-10-02"  # the day the fixtures were captured
PASSWORD = "correct horse battery"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(), parse_float=Decimal)


def fixture_float(name: str):
    return json.loads((FIXTURES / name).read_text())


def assert_matches(actual, expected, path="$"):
    """`actual` has everything in `expected` with equal values. Extra keys are allowed
    (the server adds category_source and rule_id), extra list items are not."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected an object"
        for key, value in expected.items():
            assert key in actual, f"{path}: missing key {key!r}"
            assert_matches(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected a list"
        assert len(actual) == len(expected), f"{path}: {len(actual)} items, expected {len(expected)}"
        for i, (a, e) in enumerate(zip(actual, expected)):
            assert_matches(a, e, f"{path}[{i}]")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


class Clock:
    def __init__(self, now: float = 1_790_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePlaid:
    """Plaid replies in Plaid's own JSON shape, with numbers as Decimal like the real client."""

    def __init__(self):
        self.accounts = []
        self.pages = []  # transactions/sync responses, returned in order
        self.holdings = {"accounts": [], "holdings": [], "securities": []}
        self.errors: dict[str, PlaidError] = {}
        self.calls: list[str] = []
        self.removed: list[str] = []

    def _check(self, name: str):
        self.calls.append(name)
        if name in self.errors:
            raise self.errors[name]

    def accounts_get(self, token):
        self._check("accounts_get")
        return {"accounts": self.accounts}

    def transactions_sync(self, token, cursor, count=500):
        self._check("transactions_sync")
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    def investments_holdings_get(self, token):
        self._check("investments_holdings_get")
        return self.holdings

    def link_token_create(self, client_user_id, country_codes, webhook, access_token=None):
        self._check("link_token_create")
        self.last_link = {"webhook": webhook, "update": access_token is not None}
        return {"link_token": "link-sandbox-123", "expiration": "2026-10-03T00:00:00Z"}

    def exchange_public_token(self, public_token):
        self._check("exchange_public_token")
        return {"access_token": "access-sandbox-SECRET", "item_id": "item_new"}

    def item_get(self, access_token):
        return {"item": {"institution_id": "ins_1"}}

    def institution_name(self, institution_id, country_codes):
        return "Desjardins"

    def item_remove(self, access_token):
        self._check("item_remove")
        self.removed.append(access_token)

    def webhook_verification_key(self, key_id):
        raise PlaidError(400, "INVALID_INPUT", "INVALID_KEY", "no such key")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def plaid():
    return FakePlaid()


@pytest.fixture
def services(tmp_path, plaid, clock, monkeypatch):
    monkeypatch.setattr(api_module, "today_str", lambda: TODAY)
    monkeypatch.setattr(sync_module, "today_str", lambda: TODAY)
    settings = Settings(
        data_dir=tmp_path,
        master_key=generate_master_key(),
        plaid_client_id="id",
        plaid_secret="secret",
        webhook_port=0,
        public_webhook_url="https://example.test/plaid/webhook",
    )
    s = build_services(settings, plaid=plaid)  # real SQLCipher database
    fast = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    s.auth = AuthService(s.db, s.keys, clock=clock, hasher=fast)
    yield s
    s.db.close()


@pytest.fixture
def client(services):
    return TestClient(create_api_app(services))


class Login:
    def __init__(self, services, client, clock):
        self.services, self.client, self.clock = services, client, clock
        self.secret, _, self.recovery = services.auth.create_user("paul", PASSWORD)

    def code(self) -> str:
        step = int(self.clock() // 30)
        return pyotp.TOTP(self.secret).at(step * 30)

    def sign_in(self, device="test phone", code=None, password=PASSWORD):
        return self.client.post(
            "/auth/login",
            json={"username": "paul", "password": password, "code": code or self.code(), "device_name": device},
        )

    def headers(self, device="test phone"):
        response = self.sign_in(device)
        assert response.status_code == 200, response.text
        # Consume the code so the next sign-in needs a fresh 30-second step.
        self.clock.advance(31)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def login(services, client, clock):
    return Login(services, client, clock)


@pytest.fixture
def auth(login):
    return login.headers()


def seed_fixture_data(services) -> None:
    """Load exactly the data the fixture files describe (plus 5 older September
    transactions so the totals match: the fixture shows 5 of 10)."""
    db, keys = services.db, services.keys
    items = fixture("items.json")
    for item in items:
        db.run(
            "INSERT INTO items(id, institution, access_token, error_code, needs_reauth, "
            "has_investments, last_synced_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                item["id"],
                item["institution"],
                seal(b"access-sandbox-SECRET", keys.kek, b"item:" + item["id"].encode()),
                item["error_code"],
                int(item["needs_reauth"]),
                int(item["has_investments"]),
                item["last_synced_at"],
                "2026-07-01T00:00:00.000000Z",
            ),
        )
    for a in fixture("accounts.json"):
        db.run(
            "INSERT INTO accounts(id, item_id, name, official_name, mask, type, subtype, "
            "current_balance, available_balance, credit_limit, currency, hidden, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                a["id"], a["item_id"], a["name"], a["official_name"], a["mask"], a["type"],
                a["subtype"], str(a["current_balance"]),
                None if a["available_balance"] is None else str(a["available_balance"]),
                None if a["credit_limit"] is None else str(a["credit_limit"]),
                a["currency"], int(a["hidden"]), a["updated_at"],
            ),
        )
    for t in fixture("transactions.json")["transactions"]:
        db.run(
            "INSERT INTO transactions(id, account_id, date, authorized_date, name, merchant_name, "
            "amount, currency, pending, plaid_category, category_detailed, logo_url, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t["id"], t["account_id"], t["date"], t["authorized_date"], t["name"],
                t["merchant_name"], str(t["amount"]), t["currency"], int(t["pending"]),
                t["plaid_category"], t["category_detailed"], t["logo_url"], "2026-10-02T00:00:00Z",
            ),
        )
    for i, amount in enumerate(["100.00", "100.00", "100.00", "100.00", "21.75"]):
        db.run(
            "INSERT INTO transactions(id, account_id, date, name, merchant_name, amount, currency, "
            "pending, plaid_category, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"tx_fill{i}", "acc_visa", f"2026-09-{10 + i:02d}", "Store", "Store", amount, "CAD", 0,
             "GENERAL_MERCHANDISE", "2026-10-02T00:00:00Z"),
        )
    for b in fixture("budgets.json")["budgets"]:
        db.run("INSERT INTO budgets(category, monthly_limit) VALUES (?,?)",
               (b["category"], str(b["monthly_limit"])))
    for h in fixture("holdings.json")["holdings"]:
        db.run(
            "INSERT INTO holdings(account_id, security_id, name, ticker, type, quantity, price, "
            "value, cost_basis, currency, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                h["account_id"], h["security_id"], h["name"], h["ticker"], h["type"],
                str(h["quantity"]), str(h["price"]), str(h["value"]), str(h["cost_basis"]),
                h["currency"], "2026-10-02T00:00:00Z",
            ),
        )
    for p in fixture("networth.json"):
        db.run(
            "INSERT INTO networth(date, currency, assets, liabilities, investments, net_worth) "
            "VALUES (?,?,?,?,?,?)",
            (p["date"], "CAD", str(p["assets"]), str(p["liabilities"]), str(p["investments"]),
             str(p["net_worth"])),
        )


@pytest.fixture
def seeded(services, auth, client):
    seed_fixture_data(services)
    return client, auth
