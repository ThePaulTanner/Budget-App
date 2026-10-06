"""The Plaid webhook receiver: the only part of this server meant to face the internet.

It exposes exactly one route, `POST /plaid/webhook`, on its own port. Every request is
checked against Plaid's signature before anything else happens:
  1. the `Plaid-Verification` header is a JWT signed with ES256,
  2. its key is one Plaid published (fetched by key id, cached),
  3. it was issued within the last 5 minutes,
  4. the SHA-256 of the request body matches the hash inside the JWT.
A valid webhook only queues "sync this bank". It never returns data, so even a
forged request could do no more than cause one extra sync.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Callable

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import Database
from .plaid import PlaidClient, PlaidError
from .sync import SyncManager

log = logging.getLogger("homefinance.webhook")

MAX_BODY_BYTES = 64 * 1024
MAX_AGE_SECONDS = 5 * 60
KEY_FETCH_MIN_INTERVAL = 10.0
KEY_CACHE_SECONDS = 24 * 3600  # so a key Plaid later retires is noticed
MAX_CLOCK_SKEW_SECONDS = 60
RATE_LIMIT_PER_MINUTE = 120


class InvalidWebhook(Exception):
    pass


class WebhookVerifier:
    def __init__(
        self,
        plaid: PlaidClient,
        clock: Callable[[], float] = time.time,
        key_fetcher: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._fetch = key_fetcher or plaid.webhook_verification_key
        self._clock = clock
        self._keys: dict[str, tuple[dict[str, Any], float]] = {}  # kid -> (key, fetched at)
        self._last_fetch = 0.0
        self._lock = threading.Lock()

    def _key_for(self, kid: str) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            cached = self._keys.get(kid)
            if cached is not None and now - cached[1] < KEY_CACHE_SECONDS:
                return cached[0]
            # Unknown or stale key id: fetch at most once per interval, so junk requests
            # with random key ids cannot make this server hammer Plaid.
            if now - self._last_fetch < KEY_FETCH_MIN_INTERVAL:
                if cached is not None:
                    return cached[0]  # stale beats refusing a real webhook; refreshed soon
                raise InvalidWebhook("key lookup rate limited")
            self._last_fetch = now
            try:
                key = self._fetch(kid)
            except PlaidError as exc:
                if cached is not None and (exc.status == 0 or exc.status >= 500):
                    return cached[0]  # Plaid is down or unreachable: the key we hold still verifies
                raise InvalidWebhook("unknown key") from exc
            self._keys[kid] = (key, now)
            return key

    def verify(self, body: bytes, token: str | None) -> None:
        if not token:
            raise InvalidWebhook("missing signature")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidWebhook("bad token") from exc
        if header.get("alg") != "ES256" or not header.get("kid"):
            raise InvalidWebhook("unexpected algorithm")
        jwk = self._key_for(header["kid"])
        expired_at = jwk.get("expired_at")
        try:
            key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
            claims = jwt.decode(
                token,
                key,
                algorithms=["ES256"],
                options={"verify_exp": False, "require": ["iat"]},
            )
        except (jwt.PyJWTError, ValueError) as exc:
            raise InvalidWebhook("bad signature") from exc
        issued = float(claims["iat"])
        age = self._clock() - issued
        if age > MAX_AGE_SECONDS:
            raise InvalidWebhook("too old")
        if age < -MAX_CLOCK_SKEW_SECONDS:
            raise InvalidWebhook("from the future")
        if expired_at is not None and issued > float(expired_at):
            raise InvalidWebhook("key expired")
        expected = str(claims.get("request_body_sha256", ""))
        actual = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(expected, actual):
            raise InvalidWebhook("body does not match")


class _RateLimit:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.time) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self._window_start = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = self.clock()
            if now - self._window_start >= 60:
                self._window_start, self._count = now, 0
            self._count += 1
            return self._count <= self.per_minute


REAUTH_CODES = {"PENDING_EXPIRATION", "PENDING_DISCONNECT", "USER_PERMISSION_REVOKED"}


def handle_event(db: Database, manager: SyncManager, event: dict[str, Any]) -> None:
    item_id = event.get("item_id")
    if not item_id or db.one("SELECT 1 FROM items WHERE id=?", (item_id,)) is None:
        return  # not one of our banks
    kind, code = event.get("webhook_type"), event.get("webhook_code")
    if kind == "TRANSACTIONS" and code in {
        "SYNC_UPDATES_AVAILABLE",
        "INITIAL_UPDATE",
        "HISTORICAL_UPDATE",
        "DEFAULT_UPDATE",
    }:
        manager.request(item_id)
    elif kind == "ITEM":
        error_code = (event.get("error") or {}).get("error_code")
        if code == "LOGIN_REPAIRED":
            db.run("UPDATE items SET needs_reauth=0, error_code=NULL WHERE id=?", (item_id,))
            manager.request(item_id)
        elif code in REAUTH_CODES or error_code == "ITEM_LOGIN_REQUIRED":
            db.run(
                "UPDATE items SET needs_reauth=1, error_code=? WHERE id=?",
                (error_code or code, item_id),
            )


def create_webhook_app(
    db: Database,
    manager: SyncManager,
    verifier: WebhookVerifier,
    *,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    # No docs, no OpenAPI: nothing here should advertise itself.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    limiter = _RateLimit(RATE_LIMIT_PER_MINUTE, clock)
    refuse = JSONResponse({"detail": "Rejected"}, status_code=401)

    @app.post("/plaid/webhook")
    async def plaid_webhook(request: Request):
        import asyncio

        if not limiter.allow():
            return JSONResponse({"detail": "Rejected"}, status_code=429)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return refuse
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return refuse
        try:
            await asyncio.to_thread(verifier.verify, body, request.headers.get("plaid-verification"))
            event = json.loads(body)
        except (InvalidWebhook, ValueError):
            return refuse
        if isinstance(event, dict):
            await asyncio.to_thread(handle_event, db, manager, event)
        return {}

    return app
