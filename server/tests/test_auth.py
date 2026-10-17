from __future__ import annotations

import jwt
import pytest

from app.auth import ACCESS_TTL_SECONDS, REFRESH_GRACE_SECONDS

from .conftest import PASSWORD


def test_every_data_route_needs_a_token(client):
    for method, path in [
        ("get", "/api/summary"),
        ("get", "/api/accounts"),
        ("get", "/api/transactions"),
        ("get", "/api/budgets"),
        ("get", "/api/holdings"),
        ("get", "/api/items"),
        ("post", "/api/sync"),
        ("get", "/api/rules"),
        ("post", "/api/link/token"),
        ("get", "/auth/sessions"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


def test_health_and_link_page_are_public(client):
    assert client.get("/health").json() == {"ok": True, "plaid_env": "sandbox"}
    page = client.get("/")
    assert page.status_code == 200 and "Content-Security-Policy" in page.headers
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]


def test_no_docs_or_openapi(client):
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert client.get(path).status_code == 404


def test_login_and_use_token(login, client):
    response = login.sign_in()
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == ACCESS_TTL_SECONDS and body["token_type"] == "bearer"
    ok = client.get("/api/items", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert ok.status_code == 200
    assert ok.headers["Cache-Control"] == "no-store"


def test_wrong_password_and_wrong_code_look_identical(login):
    bad_password = login.sign_in(password="wrong wrong wrong")
    bad_code = login.sign_in(code="000000")
    no_user = login.client.post(
        "/auth/login", json={"username": "nobody", "password": PASSWORD, "code": login.code()}
    )
    assert {r.status_code for r in (bad_password, bad_code, no_user)} == {401}
    assert bad_password.json() == bad_code.json() == no_user.json()


def test_a_code_works_only_once(login, clock):
    code = login.code()
    assert login.sign_in(code=code).status_code == 200
    assert login.sign_in(code=code).status_code == 401  # replay inside the same 30 seconds
    clock.advance(31)
    assert login.sign_in().status_code == 200  # the next code works


def test_recovery_code_works_once(login):
    code = login.recovery[0]
    assert login.sign_in(code=code).status_code == 200
    assert login.sign_in(code=code).status_code == 401
    assert login.sign_in(code=login.recovery[1].upper()).status_code == 200  # case and dashes ignored


def test_failed_logins_lock_the_account_with_growing_delay(login, clock):
    for _ in range(5):
        assert login.sign_in(password="nope nope nope").status_code == 401
    locked = login.sign_in()  # even the right credentials wait out the lockout
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
    clock.advance(31)
    assert login.sign_in().status_code == 200


def test_access_token_expires(login, client, clock):
    token = login.sign_in().json()["access_token"]
    clock.advance(ACCESS_TTL_SECONDS + 60)
    assert client.get("/api/items", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_forged_and_tampered_tokens_are_rejected(login, client, services):
    token = login.sign_in().json()["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    forged = jwt.encode(claims, "x" * 32, algorithm="HS256")
    none_alg = jwt.encode(claims, None, algorithm="none")
    for bad in (forged, none_alg, token[:-3] + "abc", "garbage"):
        assert client.get("/api/items", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    assert client.get("/api/items", headers={"Authorization": "Basic abc"}).status_code == 401


def test_refresh_rotates_the_token(login, client):
    first = login.sign_in().json()
    second = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]}).json()
    assert second["refresh_token"] != first["refresh_token"]
    assert client.get("/api/items", headers={"Authorization": f"Bearer {second['access_token']}"}).status_code == 200


def test_lost_refresh_response_can_be_retried_within_grace(login, client, clock):
    first = login.sign_in().json()
    client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})  # response "lost"
    clock.advance(REFRESH_GRACE_SECONDS - 5)
    retry = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert retry.status_code == 200


def test_old_refresh_token_after_grace_revokes_only_that_device(login, client, clock):
    phone = login.sign_in("phone").json()
    clock.advance(31)
    tablet = login.sign_in("tablet").json()
    client.post("/auth/refresh", json={"refresh_token": phone["refresh_token"]})
    clock.advance(REFRESH_GRACE_SECONDS + 1)
    stolen = client.post("/auth/refresh", json={"refresh_token": phone["refresh_token"]})
    assert stolen.status_code == 401
    # The phone's session is gone, even its newest token; the tablet is untouched.
    assert client.get("/api/items", headers={"Authorization": f"Bearer {phone['access_token']}"}).status_code == 401
    assert client.get("/api/items", headers={"Authorization": f"Bearer {tablet['access_token']}"}).status_code == 200


def test_unknown_refresh_token(client, login):
    assert client.post("/auth/refresh", json={"refresh_token": "nope"}).status_code == 401


def test_devices_list_and_revoke(login, client, clock):
    phone = login.sign_in("phone").json()
    clock.advance(31)
    tablet = login.sign_in("tablet").json()
    headers = {"Authorization": f"Bearer {phone['access_token']}"}
    devices = client.get("/auth/sessions", headers=headers).json()
    assert {d["device_name"] for d in devices} == {"phone", "tablet"}
    assert [d["device_name"] for d in devices if d["current"]] == ["phone"]
    assert client.delete(f"/auth/sessions/{tablet['session_id']}", headers=headers).status_code == 200
    assert client.get("/api/items", headers={"Authorization": f"Bearer {tablet['access_token']}"}).status_code == 401
    assert client.delete(f"/auth/sessions/{tablet['session_id']}", headers=headers).status_code == 404


def test_logout_revokes_the_session(login, client):
    token = login.sign_in().json()
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/items", headers=headers).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": token["refresh_token"]}).status_code == 401


def test_strong_password_and_unique_names(services):
    auth = services.auth
    with pytest.raises(ValueError, match="at least"):
        auth.create_user("a", "short")
    auth.create_user("a", PASSWORD)
    auth.create_user("b", PASSWORD)  # a second login is allowed (see test_logins.py)
    with pytest.raises(ValueError, match="already taken"):
        auth.create_user("A", PASSWORD)


def test_password_is_not_stored_or_reversible(login, services):
    row = services.db.one("SELECT password_hash FROM users")
    assert row["password_hash"].startswith("$argon2id$") and PASSWORD not in row["password_hash"]
