"""More than one login: each has its own password and authenticator, and everyone sees everything."""

from __future__ import annotations

import pyotp
import pytest

from app.auth import LOCKED_STATUS

from .conftest import fixture_float

PASSWORD2 = "a second long password"


class Second:
    """A second login, 'sam', created next to the first ('paul', from the `login` fixture)."""

    def __init__(self, services, client, clock):
        self.client, self.clock = client, clock
        self.secret, self.uri, self.recovery = services.auth.create_user("sam", PASSWORD2)

    def code(self) -> str:
        return pyotp.TOTP(self.secret).at(int(self.clock() // 30) * 30)

    def sign_in(self, password=PASSWORD2, name="sam", code=None):
        r = self.client.post("/auth/login", json={"username": name, "password": password,
                                                  "code": code or self.code(), "device_name": "Sam's phone"})
        self.clock.advance(31)  # a code works once; the next sign-in needs the next step
        return r

    def headers(self):
        r = self.sign_in()
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def sam(services, client, login, clock):
    return Second(services, client, clock)


def test_a_second_login_has_its_own_secrets(services, login, sam):
    assert len(sam.recovery) == 10 and "sam" in sam.uri
    users = services.db.query("SELECT username, totp_secret FROM users ORDER BY id")
    assert [u["username"] for u in users] == ["paul", "sam"] and users[0]["totp_secret"] != users[1]["totp_secret"]
    assert services.db.one("SELECT count(*) AS n FROM recovery_codes WHERE user_id=2")["n"] == 10


def test_names_are_unique_and_passwords_still_need_to_be_long(services, sam):
    with pytest.raises(ValueError, match="already taken"):
        services.auth.create_user("SAM ", PASSWORD2)  # names compare the way sign-in does
    with pytest.raises(ValueError, match="at least"):
        services.auth.create_user("other", "short")


def test_the_second_login_sees_the_same_data(client, seeded, sam):
    _, paul = seeded
    mine = sam.headers()
    assert client.get("/api/accounts", headers=mine).json() == client.get("/api/accounts", headers=paul).json()
    assert client.get("/api/summary", headers=mine).status_code == 200


def test_logins_are_independent(client, login, sam):
    assert sam.sign_in(password="not her password").status_code == 401
    assert sam.sign_in().status_code == 200
    # Her password with his authenticator code, and his password with her code, both fail.
    assert sam.sign_in(code=login.code()).status_code == 401
    assert sam.sign_in(name="paul", password=PASSWORD2).status_code == 401
    assert login.sign_in().status_code == 200


def test_each_login_sees_only_its_own_devices(client, seeded, sam):
    _, paul = seeded
    mine = sam.headers()
    sam_devices = client.get("/auth/sessions", headers=mine).json()
    paul_devices = client.get("/auth/sessions", headers=paul).json()
    assert {d["id"] for d in sam_devices}.isdisjoint({d["id"] for d in paul_devices})
    assert client.delete(f"/auth/sessions/{sam_devices[0]['id']}", headers=paul).status_code == 404  # not his to revoke


def test_sign_in_log_says_who(client, seeded, sam):
    sam.sign_in()
    sam.sign_in(password="wrong wrong wrong")
    sam.sign_in(name="nobody")
    events = client.get("/auth/login-log", headers=seeded[1]).json()["events"]
    by_result = {}
    for e in events:
        by_result.setdefault(e["result"], []).append(e)
    assert {e["username"] for e in by_result["success"]} == {"paul", "sam"}
    assert by_result["bad_password"][0]["username"] == "sam"
    assert by_result["unknown_user"][0]["username"] is None  # a name that is not real is never recorded


def test_a_lockout_locks_everyone_until_unlock(client, services, login, sam):
    services.auth.max_failed_logins = 3
    for _ in range(3):
        sam.sign_in(password="wrong wrong wrong")
    state = services.auth.login_log()
    assert state["locked"] is True and state["failed_streak"] == 3
    assert login.sign_in().status_code == LOCKED_STATUS  # the same refusal for everyone, so nothing is revealed
    assert services.auth.unlock() is True and services.auth.login_log()["locked"] is False
    assert sam.sign_in().status_code == 200
    assert login.sign_in().status_code == 200


def test_list_and_remove_logins(client, services, seeded, sam):
    paul = seeded[1]
    sam.sign_in()
    listing = client.get("/auth/users", headers=paul).json()
    assert [u["username"] for u in listing] == ["paul", "sam"]
    assert [u["you"] for u in listing] == [True, False]
    other = listing[1]
    assert other["devices"] == 1 and other["last_sign_in"]
    shape = fixture_float("logins.json")
    assert all(set(u) == set(shape[0]) for u in listing)
    assert client.delete(f"/auth/users/{listing[0]['id']}", headers=paul).status_code == 409  # not yourself
    assert client.delete("/auth/users/999", headers=paul).status_code == 404
    assert client.delete(f"/auth/users/{other['id']}", headers=paul).status_code == 204
    assert [u["username"] for u in client.get("/auth/users", headers=paul).json()] == ["paul"]
    assert sam.sign_in().status_code == 401  # her login no longer works
    assert services.db.one("SELECT count(*) AS n FROM sessions WHERE user_id=?", (other["id"],))["n"] == 0
    assert services.db.one("SELECT count(*) AS n FROM recovery_codes WHERE user_id=?", (other["id"],))["n"] == 0


def test_a_removed_login_stops_working_at_once(client, seeded, sam):
    paul = seeded[1]
    hers = sam.headers()
    assert client.get("/api/summary", headers=hers).status_code == 200
    sam_id = [u for u in client.get("/auth/users", headers=paul).json() if u["username"] == "sam"][0]["id"]
    assert client.delete(f"/auth/users/{sam_id}", headers=paul).status_code == 204
    assert client.get("/api/summary", headers=hers).status_code == 401  # her token, still unexpired, no longer works
    removed = [e for e in client.get("/auth/login-log", headers=paul).json()["events"] if e["result"] == "login_removed"]
    assert removed[0]["username"] == "paul" and removed[0]["origin"] == "removed sam"  # who did it, and to whom


def test_login_endpoints_need_sign_in(client):
    assert client.get("/auth/users").status_code == 401
    assert client.delete("/auth/users/2").status_code == 401


def test_cli_adds_a_second_login(tmp_path, monkeypatch, capsys):
    from app import cli
    from app.bootstrap import build_services
    from app.config import Settings
    from app.crypto import generate_master_key

    settings = Settings(data_dir=tmp_path, master_key=generate_master_key(), webhook_port=0)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD2)
    assert cli.main(["create-user", "--username", "first"]) == 0
    assert cli.main(["create-user", "--username", "second"]) == 0
    assert "Adding another login" in capsys.readouterr().out
    services = build_services(settings)
    assert [r["username"] for r in services.db.query("SELECT username FROM users ORDER BY id")] == ["first", "second"]
    services.db.close()
    assert cli.main(["create-user", "--username", "first"]) == 1  # a name can only be used once
