"""Copies of the encrypted database, kept next to it in <data>/backups.

A copy is encrypted with the same key as the database, so it is safe to leave on the NAS and to
download, and useless without the master key. Three kinds:

* daily: taken by a timer, once a day
* manual: the "Back up now" button
* upgrade: taken automatically just before the database is upgraded to a newer layout
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .db import Database

log = logging.getLogger("homefinance.backup")

KEEP = {"daily": 14, "manual": 10, "upgrade": 5}  # newest copies of each kind that are kept
DAILY_EVERY = timedelta(hours=24)
CHECK_EVERY_SECONDS = 30 * 60
NAME = re.compile(r"^homefinance-(daily|manual|upgrade)-(\d{8}T\d{6}Z)(?:-[A-Za-z0-9.-]{1,40})?\.db$")


def backup_dir(db: Database) -> Path:
    return db.path.parent / "backups"


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def _describe(path: Path) -> dict[str, Any] | None:
    match = NAME.match(path.name)
    if not match:
        return None
    taken = datetime.strptime(match.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return {
        "name": path.name,
        "kind": match.group(1),
        "created_at": taken.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "size_bytes": path.stat().st_size,
    }


def list_backups(db: Database) -> list[dict[str, Any]]:
    """Every copy, newest first."""
    folder = backup_dir(db)
    if not folder.is_dir():
        return []
    found = [d for p in folder.iterdir() if p.is_file() and (d := _describe(p))]
    return sorted(found, key=lambda d: d["created_at"], reverse=True)


def prune(db: Database, kind: str) -> None:
    for old in [b for b in list_backups(db) if b["kind"] == kind][KEEP[kind]:]:
        try:
            (backup_dir(db) / old["name"]).unlink()
        except OSError:
            log.warning("Could not remove the old backup %s", old["name"])


def create_backup(db: Database, kind: str, *, now: datetime | None = None, label: str | None = None) -> dict[str, Any]:
    """Take a copy now. Raises OSError (or a database error) if it cannot be written."""
    if kind not in KEEP:
        raise ValueError(f"unknown backup kind {kind!r}")
    now = now or datetime.now(UTC)
    folder = backup_dir(db)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"homefinance-{kind}-{_stamp(now)}{'-' + label if label else ''}.db"
    final = folder / name
    partial = folder / (name + ".partial")  # never mistaken for a finished copy
    try:
        db.backup_to(partial)
        partial.replace(final)
    finally:
        partial.unlink(missing_ok=True)
    prune(db, kind)
    return _describe(final) or {}


def last_daily(db: Database) -> datetime | None:
    for b in list_backups(db):
        if b["kind"] == "daily":
            return datetime.strptime(b["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return None


def find_backup(db: Database, name: str) -> Path | None:
    """The file for a listed backup name, or None. Only names this module wrote are accepted."""
    if not NAME.match(name):
        return None
    path = backup_dir(db) / name
    return path if path.is_file() else None


class BackupScheduler:
    """Takes the daily copy: right away at start-up if the last one is over a day old, then again
    whenever a day has passed."""

    def __init__(self, db: Database):
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or datetime.now(UTC)
        last = last_daily(self.db)
        if last is not None and now - last < DAILY_EVERY:
            return None
        try:
            made = create_backup(self.db, "daily", now=now)
            log.info("Daily backup written: %s", made.get("name"))
            return made
        except Exception:
            log.exception("The daily backup failed")
            return None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="backups", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(CHECK_EVERY_SECONDS)
