"""Backups: the button, the daily timer, the copy made before an upgrade, and downloading one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app import backup
from app.backup import BackupScheduler, create_backup, list_backups
from app.db import Database

from .conftest import fixture_float


def open_copy(services, name):
    """Open a backup the way a restore would: with the same key."""
    path = backup.backup_dir(services.db) / name
    key = None if services.settings.insecure_plaintext_db else services.keys.db_hex
    return Database(path, key, insecure_plaintext=services.settings.insecure_plaintext_db)


def test_button_makes_a_working_encrypted_copy(seeded, services):
    client, auth = seeded
    made = client.post("/api/backup", headers=auth)
    assert made.status_code == 201
    body = made.json()
    assert body["kind"] == "manual" and body["size_bytes"] > 0 and body["name"].endswith(".db")
    copy = open_copy(services, body["name"])
    assert copy.one("SELECT count(*) AS n FROM transactions")["n"] == services.db.one("SELECT count(*) AS n FROM transactions")["n"]
    assert copy.one("PRAGMA user_version")[0] == services.db.one("PRAGMA user_version")[0]
    copy.close()
    if not services.settings.insecure_plaintext_db:  # the file is not readable without the key
        assert b"SQLite format 3" not in (backup.backup_dir(services.db) / body["name"]).read_bytes()[:16]


def test_list_and_download(seeded, services):
    client, auth = seeded
    assert client.get("/api/backups", headers=auth).json()["backups"] == []
    name = client.post("/api/backup", headers=auth).json()["name"]
    listing = client.get("/api/backups", headers=auth).json()
    assert [b["name"] for b in listing["backups"]] == [name] and listing["last_daily"] is None
    assert listing["keep"] == {"daily": 14, "manual": 10, "upgrade": 5}
    got = client.get(f"/api/backups/{name}", headers=auth)
    assert got.status_code == 200 and got.content == (backup.backup_dir(services.db) / name).read_bytes()
    assert "attachment" in got.headers["content-disposition"]


def test_download_only_serves_backups(seeded):
    client, auth = seeded
    for bad in ["homefinance.db", "..%2Fhomefinance.db", "homefinance-manual-20260101T000000Z.db", "notes.txt"]:
        assert client.get(f"/api/backups/{bad}", headers=auth).status_code == 404


def test_requires_sign_in(client):
    assert client.post("/api/backup").status_code == 401
    assert client.get("/api/backups").status_code == 401
    assert client.get("/api/backups/homefinance-manual-20260101T000000Z.db").status_code == 401


def test_daily_timer_takes_one_a_day(services):
    scheduler = BackupScheduler(services.db)
    start = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    assert scheduler.run_once(start)["kind"] == "daily"  # none yet: takes one straight away
    assert scheduler.run_once(start + timedelta(hours=23)) is None
    assert scheduler.run_once(start + timedelta(hours=25))["kind"] == "daily"
    assert [b["kind"] for b in list_backups(services.db)] == ["daily", "daily"]
    assert backup.last_daily(services.db) == start + timedelta(hours=25)


def test_only_the_newest_are_kept(services):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for day in range(20):
        create_backup(services.db, "daily", now=start + timedelta(days=day))
    daily = [b for b in list_backups(services.db) if b["kind"] == "daily"]
    assert len(daily) == backup.KEEP["daily"] == 14
    assert daily[0]["created_at"] == "2026-01-20T00:00:00Z"  # the newest survived, the oldest went
    manual = create_backup(services.db, "manual", now=start)  # another kind is untouched by the daily pruning
    assert manual["kind"] == "manual" and len([b for b in list_backups(services.db) if b["kind"] == "manual"]) == 1


def test_a_failed_backup_is_reported_and_leaves_no_partial_file(seeded, services, monkeypatch):
    client, auth = seeded

    def boom(target):
        open(target, "wb").write(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(services.db, "backup_to", boom)
    r = client.post("/api/backup", headers=auth)
    assert r.status_code == 500 and "free space" in r.json()["detail"]
    assert list_backups(services.db) == []
    assert not list(backup.backup_dir(services.db).glob("*.partial"))
    assert BackupScheduler(services.db).run_once() is None  # the timer swallows the failure


def test_upgrading_the_database_keeps_a_copy_first(tmp_path):
    key = "ab" * 32
    path = tmp_path / "old.db"
    old = Database(path, key)
    old.run("INSERT INTO budgets(category, monthly_limit) VALUES ('FOOD', '100')")
    for column in ("kind", "active_at", "expires_at"):
        old.run(f"ALTER TABLE sessions DROP COLUMN {column}")  # what the last layout change added
    old.run("PRAGMA user_version = 12")  # looks like the previous release
    old.close()
    upgraded = Database(path, key)  # the migrations run on open
    copies = list_backups(upgraded)
    assert [c["kind"] for c in copies] == ["upgrade"] and "v12-to-" in copies[0]["name"]
    from sqlcipher3 import dbapi2 as sqlcipher

    before = sqlcipher.connect(str(tmp_path / "backups" / copies[0]["name"]))  # raw: opening it must not upgrade it
    before.execute(f"PRAGMA key = \"x'{key}'\"")
    assert before.execute("PRAGMA user_version").fetchone()[0] == 12  # the copy is from before the upgrade
    assert before.execute("SELECT monthly_limit FROM budgets").fetchone()[0] == "100"
    before.close()
    assert upgraded.one("PRAGMA user_version")[0] > 12
    upgraded.close()


def test_a_fresh_database_makes_no_upgrade_copy(tmp_path):
    db = Database(tmp_path / "new.db", "cd" * 32)
    assert list_backups(db) == []
    db.close()


def test_fixture_shape(seeded):
    client, auth = seeded
    client.post("/api/backup", headers=auth)
    body = client.get("/api/backups", headers=auth).json()
    expected = fixture_float("backups.json")
    assert set(body) == set(expected) and body["keep"] == expected["keep"]
    assert set(body["backups"][0]) == set(expected["backups"][0])
    assert {type(v) for v in body["backups"][0].values()} == {type(v) for v in expected["backups"][0].values()}
