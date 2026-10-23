"""The sign-in log and the hard lock after too many failed attempts."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app import auth as auth_module
from app import cli
from app.auth import LOCKED_STATUS
from app.bootstrap import build_services
from app.config import Settings
from app.crypto import generate_master_key
from app.db import SCHEMA_VERSION, Database

from .conftest import PASSWORD


def attempt(client, password=PASSWORD, code="000000", username="paul", headers=None):
    return client.post(
        "/auth/login",
        json={"username": username, "password": password, "code": code, "device_name": "test"},
        headers=headers or {},
    )


def results(services):
    return [e["result"] for e in services.auth.login_log(500)["events"]]


def test_failures_and_successes_are_logged_without_secrets(login, client, services, clock):
    attempt(client, password="wrong-password-123", code="111111")
    attempt(client, code="222222")  # right password, wrong code
    attempt(client, username="hunter2-not-a-user")  # a password typed into the username box
    assert login.sign_in().status_code == 200
    events = services.auth.login_log()["events"]
    assert [e["result"] for e in events] == ["success", "unknown_user", "bad_code", "bad_password"]
    assert events[0]["method"] == "totp" and events[0]["user_known"] is True
    assert events[1]["user_known"] is False
    blob = repr(events) + repr(services.db.query("SELECT * FROM login_events"))
    for secret in ("wrong-password-123", "111111", "222222", "hunter2-not-a-user", PASSWORD):
        assert secret not in blob, secret


def test_recovery_code_sign_in_is_marked(login, client, services):
    response = client.post(
        "/auth/login",
        json={"username": "paul", "password": PASSWORD, "code": login.recovery[0], "device_name": "x"},
    )
    assert response.status_code == 200
    assert services.auth.login_log()["events"][0]["method"] == "recovery"


def test_hard_lock_after_max_failures_refuses_even_the_right_credentials(login, client, services, clock):
    services.auth.max_failed_logins = 4  # below the temporary-delay threshold of 5
    existing = login.headers()
    for _ in range(4):
        assert attempt(client).status_code == 401
    state = services.auth.login_log()
    assert state["locked"] is True and state["failed_streak"] == 4
    assert results(services)[:3] == ["lockout", "bad_code", "bad_code"]
    response = login.sign_in()  # correct password and a valid code
    assert response.status_code == LOCKED_STATUS
    assert "unlock" in response.json()["detail"].lower()
    assert "retry-after" not in response.headers
    # people already signed in are not kicked out, and can read the log
    assert client.get("/api/summary", headers=existing).status_code == 200
    assert client.get("/auth/login-log", headers=existing).json()["locked"] is True


def test_refused_attempts_while_locked_collapse_into_one_row(login, client, services):
    services.auth.max_failed_logins = 3
    for _ in range(3):
        attempt(client)
    for _ in range(6):
        attempt(client)
    rows = [e for e in services.auth.login_log()["events"] if e["result"] == "locked"]
    assert len(rows) == 1 and rows[0]["count"] == 6


def test_unlock_restores_sign_in_and_resets_the_count(login, client, services):
    services.auth.max_failed_logins = 3
    for _ in range(3):
        attempt(client)
    assert login.sign_in().status_code == LOCKED_STATUS
    assert services.auth.unlock() is True
    assert services.auth.unlock() is False  # already unlocked
    state = services.auth.login_log()
    assert state["locked"] is False and state["failed_streak"] == 0
    assert "unlocked" in results(services)
    services.auth.user_limiter._state.clear()  # the in-memory delay is separate and short-lived
    assert login.sign_in().status_code == 200


def test_success_resets_the_streak(login, client, services, clock):
    services.auth.max_failed_logins = 4
    for _ in range(3):
        attempt(client)
    assert login.sign_in().status_code == 200
    assert services.auth.login_log()["failed_streak"] == 0
    clock.advance(31)
    for _ in range(3):
        attempt(client)
    assert services.auth.login_log()["locked"] is False


def test_guessing_other_usernames_never_locks_the_real_account(login, client, services):
    services.auth.max_failed_logins = 3
    for i in range(10):
        attempt(client, username=f"someone{i}")
    assert services.auth.login_log()["locked"] is False
    assert services.auth.login_log()["failed_streak"] == 0


def test_a_burst_of_parallel_guesses_cannot_exceed_the_limit(login, services):
    services.auth.max_failed_logins = 4
    codes = []

    def guess():
        try:
            services.auth.login("paul", "wrong-password-123", "000000", "burst", "10.0.0.1")
        except auth_module.AuthError as exc:
            codes.append(exc.status)

    threads = [threading.Thread(target=guess) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(401) == 4 and codes.count(LOCKED_STATUS) == 16
    assert services.auth.login_log()["failed_streak"] == 4


def test_temporary_delay_is_logged_once_per_run(login, client, services):
    services.auth.max_failed_logins = 50
    for _ in range(9):
        attempt(client)
    rate = [e for e in services.auth.login_log()["events"] if e["result"] == "rate_limited"]
    assert len(rate) == 1 and rate[0]["count"] == 4  # attempts 6 to 9 were delayed


def test_log_is_bounded(login, client, services, monkeypatch):
    monkeypatch.setattr(auth_module, "LOG_MAX_ROWS", 5)
    services.auth.max_failed_logins = 100
    services.auth.user_limiter.max_failures = 1000
    for i in range(12):
        attempt(client, username=f"nobody{i}")
    assert len(services.db.query("SELECT id FROM login_events")) == 5


def test_log_endpoint_needs_a_token_and_shows_the_proxy_origin(login, client, services):
    assert client.get("/auth/login-log").status_code == 401
    attempt(client, headers={"X-Forwarded-For": "100.64.1.2", "Tailscale-User-Login": "paul@example.com"})
    headers = login.headers()
    data = client.get("/auth/login-log?limit=5", headers=headers).json()
    assert set(data) == {"locked", "locked_at", "failed_streak", "max_failed_logins", "events"}
    failed = [e for e in data["events"] if e["result"] == "bad_code"][0]
    assert "100.64.1.2" in failed["origin"] and "paul@example.com" in failed["origin"]
    assert client.get("/auth/login-log?limit=0", headers=headers).status_code == 422


def test_web_login_is_logged_and_locked_the_same_way(services, login):
    from fastapi.testclient import TestClient

    from app.api import create_api_app

    web = TestClient(create_api_app(services), base_url="https://testserver")
    services.auth.max_failed_logins = 3
    for _ in range(3):
        web.post("/web/login", json={"username": "paul", "password": "nope-nope-nope", "code": "000000"},
                 headers={"X-Requested-With": "HomeFinance"})
    response = web.post("/web/login", json={"username": "paul", "password": PASSWORD, "code": login.code()},
                        headers={"X-Requested-With": "HomeFinance"})
    assert response.status_code == LOCKED_STATUS and "set-cookie" not in response.headers


def test_upgrade_from_schema_1_keeps_the_user_and_reaches_the_latest_schema(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
          totp_secret BLOB NOT NULL, totp_last_step INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO users(username, password_hash, totp_secret, created_at) VALUES ('paul', 'h', x'00', 't');
        CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, device_name TEXT NOT NULL, refresh_hash TEXT NOT NULL,
          prev_refresh_hash TEXT, prev_valid_until REAL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL, revoked_at TEXT);
        CREATE TABLE holdings (account_id TEXT, security_id TEXT, ticker TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE items (id TEXT PRIMARY KEY, institution TEXT);
        INSERT INTO items VALUES ('item_1', 'TD');
        CREATE TABLE networth (date TEXT NOT NULL, currency TEXT NOT NULL, assets TEXT NOT NULL,
          liabilities TEXT NOT NULL, investments TEXT NOT NULL, net_worth TEXT NOT NULL, PRIMARY KEY (date, currency));
        INSERT INTO networth VALUES ('2026-10-01', 'CAD', '10', '4', '0', '6');
        PRAGMA user_version = 1;
        """
    )
    old.close()
    db = Database(path, None, insecure_plaintext=True)
    assert db.one("PRAGMA user_version")[0] == SCHEMA_VERSION == 17
    assert "mic" in [c[1] for c in db.query("PRAGMA table_info(holdings)")]
    tx_columns = [c[1] for c in db.query("PRAGMA table_info(transactions)")]
    assert {"merchant_id", "cashflow_override", "transfer_pair_id"} <= set(tx_columns)
    assert db.query("SELECT * FROM merchants") == [] and db.query("SELECT * FROM merchant_aliases") == []
    user = db.one("SELECT username, failed_streak, locked_at FROM users")
    assert (user["username"], user["failed_streak"], user["locked_at"]) == ("paul", 0, None)
    assert db.query("SELECT * FROM login_events") == []
    assert db.query("SELECT * FROM card_dues") == []
    bank = db.one("SELECT institution, logo, color, logo_checked FROM items")  # existing banks get their logo at the next sync
    assert (bank["institution"], bank["logo"], bank["color"], bank["logo_checked"]) == ("TD", None, None, 0)
    assert db.query("SELECT * FROM people") == [] and db.query("SELECT * FROM account_owners") == []
    kept = db.one("SELECT date, currency, person_id, net_worth FROM networth")  # old history becomes the household's
    assert (kept["date"], kept["currency"], kept["person_id"], kept["net_worth"]) == ("2026-10-01", "CAD", 0, "6")
    db.close()
    Database(path, None, insecure_plaintext=True).close()  # reopening is a no-op


def test_a_database_from_the_future_is_refused(tmp_path):
    path = tmp_path / "new.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(Exception, match="newer version"):
        Database(path, None, insecure_plaintext=True)


def test_cli_login_log_and_unlock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HF_MASTER_KEY", generate_master_key())
    services = build_services(Settings(data_dir=tmp_path, master_key=__import__("os").environ["HF_MASTER_KEY"]))
    services.auth.create_user("paul", PASSWORD)
    services.auth.max_failed_logins = 3
    for _ in range(3):
        with pytest.raises(auth_module.AuthError):
            services.auth.login("paul", "wrong-password-123", "000000", "cli test", "1.2.3.4")
    services.db.close()
    assert cli.main(["login-log"]) == 0
    out = capsys.readouterr().out
    assert "LOCKED" in out and "bad_password" in out and "wrong-password-123" not in out
    assert cli.main(["unlock"]) == 0
    assert "unlocked" in capsys.readouterr().out.lower()
    assert cli.main(["unlock"]) == 0
    assert "was not locked" in capsys.readouterr().out


def test_lock_answer_does_not_reveal_whether_the_username_exists(login, client, services):
    services.auth.max_failed_logins = 3
    for _ in range(3):
        attempt(client)
    real = attempt(client, username="paul")
    other = attempt(client, username="not-a-user")
    assert (real.status_code, real.json()) == (other.status_code, other.json()) == (LOCKED_STATUS, real.json())


def test_control_characters_in_attacker_text_never_reach_the_log(login, client, services, capsys):
    client.post(
        "/auth/login",
        json={"username": "paul", "password": "wrong-password-123", "code": "000000", "device_name": "evil\x1b[2Jdevice\x07"},
        headers={"X-Forwarded-For": "1.2.3.4\x1b]0;pwned\x07"},
    )
    event = services.auth.login_log()["events"][0]
    assert "\x1b" not in event["device_name"] and "\x07" not in event["device_name"]
    assert event["device_name"] == "evil[2Jdevice" and "\x1b" not in event["origin"]


def test_login_log_and_423_fixtures_match_the_server(login, client, services):
    from .conftest import fixture_float

    services.auth.max_failed_logins = 3
    for _ in range(3):
        attempt(client)
    locked = login.sign_in()
    assert locked.status_code == LOCKED_STATUS
    assert locked.json() == fixture_float("error_423.json")
    shape = fixture_float("login_log.json")
    live = services.auth.login_log()
    assert set(live) == set(shape)
    assert all(set(e) == set(shape["events"][0]) for e in live["events"])


def test_cli_reset_networth(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HF_DATA_DIR", str(tmp_path))
    key = generate_master_key()
    monkeypatch.setenv("HF_MASTER_KEY", key)
    services = build_services(Settings(data_dir=tmp_path, master_key=key))
    services.db.run("INSERT INTO networth(date, currency, assets, liabilities, investments, net_worth) VALUES ('2026-10-01','CAD','1','0','0','1')")
    services.db.close()
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert cli.main(["reset-networth"]) == 1  # declined: nothing deleted
    assert cli.main(["reset-networth", "--yes"]) == 0
    assert "Deleted 1" in capsys.readouterr().out
    again = build_services(Settings(data_dir=tmp_path, master_key=key))
    assert again.db.query("SELECT * FROM networth") == []
    again.db.close()
