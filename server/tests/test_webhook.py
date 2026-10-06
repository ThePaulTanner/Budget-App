from __future__ import annotations

import hashlib
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.plaid import PlaidError
from app.webhook import WebhookVerifier, create_webhook_app

from .test_sync import add_item

EVENT = {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item_desj"}


class Signer:
    def __init__(self, clock):
        self.clock = clock
        self.private = ec.generate_private_key(ec.SECP256R1())
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.private.public_key()))
        self.jwk = {**jwk, "alg": "ES256", "use": "sig", "kid": "key1", "created_at": 0, "expired_at": None}
        self.fetches = 0

    def fetch(self, kid):
        self.fetches += 1
        if kid != "key1":
            raise PlaidError(400, "INVALID_INPUT", "INVALID_KEY", "unknown")
        return self.jwk

    def sign(self, body: bytes, *, iat=None, key=None, kid="key1", algorithm="ES256", sha=None):
        claims = {"iat": int(self.clock() if iat is None else iat),
                  "request_body_sha256": sha or hashlib.sha256(body).hexdigest()}
        return jwt.encode(claims, key or self.private, algorithm=algorithm, headers={"kid": kid})


@pytest.fixture
def hook(services, clock, plaid):
    signer = Signer(clock)
    verifier = WebhookVerifier(plaid, clock=clock, key_fetcher=signer.fetch)
    add_item(services)
    client = TestClient(create_webhook_app(services.db, services.sync, verifier, clock=clock))
    return client, signer, services


def post(client, body: bytes, token):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Plaid-Verification"] = token
    return client.post("/plaid/webhook", content=body, headers=headers)


def queued(services):
    try:
        return services.sync._queue.get_nowait()
    except Exception:
        return None


def test_valid_webhook_queues_a_sync_and_returns_nothing(hook):
    client, signer, services = hook
    body = json.dumps(EVENT).encode()
    response = post(client, body, signer.sign(body))
    assert response.status_code == 200 and response.json() == {}
    assert queued(services) == "item_desj"


def test_tampered_body_is_rejected(hook):
    client, signer, services = hook
    body = json.dumps(EVENT).encode()
    token = signer.sign(body)
    assert post(client, body + b" ", token).status_code == 401
    assert queued(services) is None


def test_missing_old_and_forged_signatures_are_rejected(hook, clock):
    client, signer, services = hook
    body = json.dumps(EVENT).encode()
    assert post(client, body, None).status_code == 401
    assert post(client, body, "not.a.jwt").status_code == 401
    assert post(client, body, signer.sign(body, iat=clock() - 301)).status_code == 401  # over 5 minutes
    assert post(client, body, signer.sign(body, key=ec.generate_private_key(ec.SECP256R1()))).status_code == 401
    assert post(client, body, signer.sign(body, algorithm="HS256", key="s" * 32)).status_code == 401
    assert post(client, body, signer.sign(body, sha="0" * 64)).status_code == 401
    assert queued(services) is None


def test_expired_key_is_refused(hook, clock):
    client, signer, services = hook
    signer.jwk["expired_at"] = int(clock()) - 10
    body = json.dumps(EVENT).encode()
    assert post(client, body, signer.sign(body)).status_code == 401


def test_unknown_key_ids_cannot_make_us_hammer_plaid(hook):
    client, signer, services = hook
    body = json.dumps(EVENT).encode()
    for kid in ("a", "b", "c", "d"):
        assert post(client, body, signer.sign(body, kid=kid)).status_code == 401
    assert signer.fetches == 1  # one lookup per interval, however many junk requests arrive


def test_events_for_unknown_banks_are_ignored(hook):
    client, signer, services = hook
    body = json.dumps({**EVENT, "item_id": "someone_elses"}).encode()
    assert post(client, body, signer.sign(body)).status_code == 200
    assert queued(services) is None


def test_item_errors_flag_reconnect_and_repair_clears_it(hook):
    client, signer, services = hook
    err = json.dumps({"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": "item_desj",
                      "error": {"error_code": "ITEM_LOGIN_REQUIRED"}}).encode()
    post(client, err, signer.sign(err))
    assert services.db.one("SELECT needs_reauth FROM items")["needs_reauth"] == 1
    fixed = json.dumps({"webhook_type": "ITEM", "webhook_code": "LOGIN_REPAIRED", "item_id": "item_desj"}).encode()
    post(client, fixed, signer.sign(fixed))
    assert services.db.one("SELECT needs_reauth FROM items")["needs_reauth"] == 0
    assert queued(services) == "item_desj"


def test_only_the_webhook_route_exists(hook):
    client, signer, services = hook
    for path in ("/", "/health", "/api/summary", "/auth/login", "/docs", "/openapi.json", "/static/link.js"):
        assert client.get(path).status_code == 404, path
    assert client.get("/plaid/webhook").status_code == 405


def test_oversized_and_garbage_bodies_are_rejected(hook):
    client, signer, services = hook
    big = b"{" + b" " * 70_000 + b"}"
    assert post(client, big, signer.sign(big)).status_code == 401
    garbage = b"not json"
    assert post(client, garbage, signer.sign(garbage)).status_code == 401


def test_flood_is_rate_limited(hook):
    client, signer, services = hook
    statuses = {post(client, b"{}", None).status_code for _ in range(130)}
    assert 429 in statuses
