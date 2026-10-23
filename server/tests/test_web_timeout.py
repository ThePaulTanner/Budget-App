"""The web app signs you out after a stretch of no activity, and after a total time limit."""

from __future__ import annotations

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.api import create_api_app

from .conftest import PASSWORD

WEB = {"X-Requested-With": "HomeFinance"}
IDLE, LIMIT = 600, 8 * 3600
GRACE = 15  # the server allows this much beyond the page's own idle clock


@pytest.fixture
def web(services):
    return TestClient(create_api_app(services), base_url="https://testserver")  # the cookie is Secure


def renewed(client):
    """What the page does when its 15-minute token runs out: ask for a new one with the cookie."""
    response = client.post("/web/refresh", headers=WEB)
    return response, {"Authorization": f"Bearer {response.json().get('access_token', '')}"}


def web_login(client, login, clock):
    clock.now += 31  # a fresh authenticator step
    code = pyotp.TOTP(login.secret).at(int(clock() // 30) * 30)
    response = client.post("/web/login", headers=WEB, json={"username": "paul", "password": PASSWORD, "code": code})
    assert response.status_code == 200
    return response, {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_sign_in_tells_the_page_the_limits_and_sets_a_session_cookie(web, login, clock):
    response, _ = web_login(web, login, clock)
    body = response.json()
    assert body["idle_timeout"] == IDLE and 0 < body["session_expires_in"] <= LIMIT
    cookie = response.headers["set-cookie"].lower()
    assert "max-age" not in cookie and "expires" not in cookie and "httponly" in cookie  # gone when the browser closes


def test_idle_web_session_is_refused_and_signed_out(web, login, clock):
    _, headers = web_login(web, login, clock)
    assert web.get("/api/version", headers=headers).status_code == 200
    clock.now += IDLE + GRACE - 5
    assert web.get("/api/version", headers=headers).status_code == 200
    clock.now += 10
    refused = web.get("/api/version", headers=headers)
    assert refused.status_code == 401 and "inactivity" in refused.json()["detail"]
    assert web.get("/auth/sessions", headers=headers).status_code == 401  # the session itself is gone


def test_idle_session_cannot_be_renewed_with_its_cookie(web, login, clock):
    web_login(web, login, clock)
    clock.now += IDLE + GRACE + 1
    first, _ = renewed(web)
    assert first.status_code == 401 and "inactivity" in first.json()["detail"]
    assert renewed(web)[0].status_code == 401  # and it stays over


def test_activity_keeps_it_open_but_background_requests_do_not(web, login, clock):
    _, headers = web_login(web, login, clock)
    for _ in range(3):  # polling for live prices (token renewals and requests) is not "being there"
        clock.now += IDLE // 2
        response, headers = renewed(web)
        if response.status_code == 401:
            break
        assert web.get("/api/version", headers=headers).status_code == 200
    else:
        raise AssertionError("background requests kept an idle session alive")
    _, headers = web_login(web, login, clock)
    for _ in range(12):  # a tap every few minutes does
        clock.now += IDLE // 2
        assert web.post("/web/activity", headers={**headers, **WEB}).status_code == 200
        _, headers = renewed(web)
        assert web.get("/api/version", headers=headers).status_code == 200


def test_total_time_limit_applies_however_busy_you_are(web, login, clock):
    _, headers = web_login(web, login, clock)
    waited = 0
    while waited + IDLE // 2 < LIMIT:
        clock.now += IDLE // 2
        waited += IDLE // 2
        assert web.post("/web/activity", headers={**headers, **WEB}).status_code == 200
        _, headers = renewed(web)
    clock.now += IDLE // 2  # the limit itself
    ended, _ = renewed(web)
    assert ended.status_code == 401 and "time limit" in ended.json()["detail"]


def test_activity_needs_the_custom_header_and_a_login(web, login, clock):
    _, headers = web_login(web, login, clock)
    assert web.post("/web/activity", headers=headers).status_code == 403
    assert web.post("/web/activity", headers=WEB).status_code == 401


def test_app_sessions_are_not_on_the_web_clock(client, login, clock):
    clock.now += LIMIT * 2
    assert client.get("/api/version", headers=login.headers()).status_code == 200


def test_the_limits_can_be_set(tmp_path, monkeypatch):
    from app.config import load_settings

    monkeypatch.setenv("HF_MASTER_KEY", "ab" * 32)
    monkeypatch.setenv("PLAID_CLIENT_ID", "x")
    monkeypatch.setenv("PLAID_SECRET", "y")
    monkeypatch.setenv("HF_DATA_DIR", str(tmp_path))
    assert (load_settings().web_idle_minutes, load_settings().web_max_hours) == (10, 8)
    monkeypatch.setenv("WEB_IDLE_MINUTES", "1")
    monkeypatch.setenv("WEB_MAX_HOURS", "100")
    settings = load_settings()
    assert (settings.web_idle_minutes, settings.web_max_hours) == (2, 24)  # kept within sensible bounds


def test_upgrade_signs_out_old_browser_sessions_once(tmp_path):
    from app.db import Database

    key = "ef" * 32
    db = Database(tmp_path / "old.db", key)
    db.run("INSERT INTO users(id, username, password_hash, totp_secret, created_at) VALUES (1,'paul','x',x'00','2026-01-01T00:00:00Z')")
    for sid, device in (("w", "Web browser"), ("p", "Paul's iPhone")):
        db.run("INSERT INTO sessions(id, user_id, device_name, refresh_hash, created_at, last_used_at) VALUES (?,1,?,'h','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", (sid, device))
    for column in ("kind", "active_at", "expires_at"):
        db.run(f"ALTER TABLE sessions DROP COLUMN {column}")
    db.run("DROP TABLE networth_adjust")
    db.run("ALTER TABLE networth DROP COLUMN estimated")
    db.run("ALTER TABLE merchants DROP COLUMN is_income")
    db.run("ALTER TABLE merchants DROP COLUMN category")
    db.run("PRAGMA user_version = 12")
    db.close()
    upgraded = Database(tmp_path / "old.db", key)
    rows = {r["id"]: r for r in upgraded.query("SELECT id, revoked_at, kind FROM sessions")}
    assert rows["w"]["revoked_at"] is not None and rows["p"]["revoked_at"] is None and rows["p"]["kind"] == "app"
    upgraded.close()


def test_sign_out_notices_state_the_period_and_never_say_sign_in_again():
    from app.auth import span
    assert span(600) == "10 minutes" and span(3600) == "1 hour" and span(8 * 3600) == "8 hours" and span(120) == "2 minutes"
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "app" / "static" / "web"
    assert "Sign in again" not in (web / "app.js").read_text() and 'id="login-notice"' in (web / "index.html").read_text()
