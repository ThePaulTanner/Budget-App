"""SQLCipher-backed database (AES-256, per-page HMAC).

A single connection guarded by a re-entrant lock is plenty for one user and keeps
the cost of opening an encrypted database off the request path. Money is stored
as TEXT decimals, never floats, and summed in Python with `Decimal`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("homefinance.db")

SCHEMA_VERSION = 10

SCHEMA = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  totp_secret BLOB NOT NULL,
  totp_last_step INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  failed_streak INTEGER NOT NULL DEFAULT 0,
  locked_at TEXT
);
CREATE TABLE recovery_codes (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL UNIQUE,
  used_at TEXT
);
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_name TEXT NOT NULL,
  refresh_hash TEXT NOT NULL,
  prev_refresh_hash TEXT,
  prev_valid_until REAL,
  created_at TEXT NOT NULL,
  last_used_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE TABLE items (
  id TEXT PRIMARY KEY,
  institution_id TEXT,
  institution TEXT,
  access_token BLOB NOT NULL,
  cursor TEXT,
  error_code TEXT,
  needs_reauth INTEGER NOT NULL DEFAULT 0,
  has_investments INTEGER NOT NULL DEFAULT 0,
  last_synced_at TEXT,
  created_at TEXT NOT NULL,
  logo TEXT,
  color TEXT,
  logo_checked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  official_name TEXT,
  mask TEXT,
  type TEXT NOT NULL,
  subtype TEXT,
  current_balance TEXT,
  available_balance TEXT,
  credit_limit TEXT,
  currency TEXT NOT NULL,
  hidden INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE transactions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  authorized_date TEXT,
  name TEXT NOT NULL,
  merchant_name TEXT,
  amount TEXT NOT NULL,
  currency TEXT NOT NULL,
  pending INTEGER NOT NULL DEFAULT 0,
  plaid_category TEXT NOT NULL,
  category_detailed TEXT,
  override_category TEXT,
  rule_id INTEGER,
  rule_category TEXT,
  logo_url TEXT,
  updated_at TEXT NOT NULL,
  merchant_id INTEGER REFERENCES merchants(id) ON DELETE SET NULL,
  cashflow_override TEXT,
  transfer_pair_id TEXT
);
CREATE INDEX transactions_date ON transactions(date DESC);
CREATE INDEX transactions_account ON transactions(account_id);
CREATE INDEX transactions_merchant ON transactions(merchant_id);
CREATE TABLE holdings (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  security_id TEXT NOT NULL,
  name TEXT NOT NULL,
  ticker TEXT,
  mic TEXT,
  type TEXT,
  quantity TEXT,
  price TEXT,
  value TEXT,
  cost_basis TEXT,
  currency TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account_id, security_id)
);
CREATE TABLE budgets (
  category TEXT PRIMARY KEY,
  monthly_limit TEXT NOT NULL
);
CREATE TABLE rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_field TEXT NOT NULL CHECK (match_field IN ('merchant_name', 'name')),
  match_type TEXT NOT NULL CHECK (match_type IN ('equals', 'contains')),
  pattern TEXT NOT NULL,
  account_id TEXT,
  category TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE login_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  result TEXT NOT NULL,
  ip TEXT,
  origin TEXT,
  method TEXT,
  device_name TEXT,
  user_known INTEGER NOT NULL DEFAULT 0,
  count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE merchants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  auto INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX merchants_name ON merchants(lower(name));
CREATE TABLE merchant_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id INTEGER NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
  match_field TEXT NOT NULL CHECK (match_field IN ('merchant_name', 'name')),
  match_type TEXT NOT NULL CHECK (match_type IN ('equals', 'contains')),
  pattern TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('user', 'auto')),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX merchant_aliases_unique ON merchant_aliases(match_field, match_type, lower(pattern));
CREATE INDEX merchant_aliases_merchant ON merchant_aliases(merchant_id);
CREATE TABLE networth (
  date TEXT NOT NULL,
  currency TEXT NOT NULL,
  person_id INTEGER NOT NULL DEFAULT 0,
  assets TEXT NOT NULL,
  liabilities TEXT NOT NULL,
  investments TEXT NOT NULL,
  net_worth TEXT NOT NULL,
  PRIMARY KEY (date, currency, person_id)
);
CREATE TABLE people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX people_name ON people(lower(name));
CREATE TABLE account_owners (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  PRIMARY KEY (account_id, person_id)
);
CREATE INDEX account_owners_person ON account_owners(person_id);
CREATE TABLE custom_categories (
  code TEXT PRIMARY KEY,
  parent TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE investment_transactions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  security_id TEXT,
  date TEXT NOT NULL,
  name TEXT NOT NULL,
  ticker TEXT,
  type TEXT NOT NULL,
  subtype TEXT,
  quantity TEXT,
  price TEXT,
  amount TEXT NOT NULL,
  fees TEXT,
  currency TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX investment_transactions_account ON investment_transactions(account_id, date DESC);
CREATE INDEX investment_transactions_security ON investment_transactions(security_id);
CREATE TABLE card_dues (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  due_date TEXT,
  minimum_payment TEXT,
  statement_balance TEXT,
  statement_date TEXT,
  last_payment_amount TEXT,
  last_payment_date TEXT,
  is_overdue INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""

# Upgrades for databases created by an older version: {from_version: SQL}.
MIGRATIONS = {
    1: """
ALTER TABLE users ADD COLUMN failed_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_at TEXT;
CREATE TABLE login_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  result TEXT NOT NULL,
  ip TEXT,
  origin TEXT,
  method TEXT,
  device_name TEXT,
  user_known INTEGER NOT NULL DEFAULT 0,
  count INTEGER NOT NULL DEFAULT 1
);
""",
    2: """
ALTER TABLE holdings ADD COLUMN mic TEXT;
""",
    3: """
CREATE TABLE merchants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  auto INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX merchants_name ON merchants(lower(name));
CREATE TABLE merchant_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id INTEGER NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
  match_field TEXT NOT NULL CHECK (match_field IN ('merchant_name', 'name')),
  match_type TEXT NOT NULL CHECK (match_type IN ('equals', 'contains')),
  pattern TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('user', 'auto')),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX merchant_aliases_unique ON merchant_aliases(match_field, match_type, lower(pattern));
CREATE INDEX merchant_aliases_merchant ON merchant_aliases(merchant_id);
ALTER TABLE transactions ADD COLUMN merchant_id INTEGER REFERENCES merchants(id) ON DELETE SET NULL;
CREATE INDEX transactions_merchant ON transactions(merchant_id);
""",
    4: """
CREATE TABLE card_dues (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  due_date TEXT,
  minimum_payment TEXT,
  statement_balance TEXT,
  statement_date TEXT,
  last_payment_amount TEXT,
  last_payment_date TEXT,
  is_overdue INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
""",

    5: """
CREATE TABLE people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX people_name ON people(lower(name));
CREATE TABLE account_owners (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  PRIMARY KEY (account_id, person_id)
);
CREATE INDEX account_owners_person ON account_owners(person_id);
CREATE TABLE networth_new (
  date TEXT NOT NULL,
  currency TEXT NOT NULL,
  person_id INTEGER NOT NULL DEFAULT 0,
  assets TEXT NOT NULL,
  liabilities TEXT NOT NULL,
  investments TEXT NOT NULL,
  net_worth TEXT NOT NULL,
  PRIMARY KEY (date, currency, person_id)
);
INSERT INTO networth_new(date, currency, person_id, assets, liabilities, investments, net_worth)
  SELECT date, currency, 0, assets, liabilities, investments, net_worth FROM networth;
DROP TABLE networth;
ALTER TABLE networth_new RENAME TO networth;
""",

    6: """
ALTER TABLE items ADD COLUMN logo TEXT;
ALTER TABLE items ADD COLUMN color TEXT;
ALTER TABLE items ADD COLUMN logo_checked INTEGER NOT NULL DEFAULT 0;
""",

    7: """
CREATE TABLE custom_categories (
  code TEXT PRIMARY KEY,
  parent TEXT,
  created_at TEXT NOT NULL
);
""",

    8: """
CREATE TABLE investment_transactions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  security_id TEXT,
  date TEXT NOT NULL,
  name TEXT NOT NULL,
  ticker TEXT,
  type TEXT NOT NULL,
  subtype TEXT,
  quantity TEXT,
  price TEXT,
  amount TEXT NOT NULL,
  fees TEXT,
  currency TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX investment_transactions_account ON investment_transactions(account_id, date DESC);
CREATE INDEX investment_transactions_security ON investment_transactions(security_id);
""",

    # Cash flow: your choice to count or leave out a transaction ("include" / "exclude"), and the
    # other half of a matched transfer between two of your own accounts (set by transfers.py).
    9: """
ALTER TABLE transactions ADD COLUMN cashflow_override TEXT;
ALTER TABLE transactions ADD COLUMN transfer_pair_id TEXT;
"""}


def utcnow() -> str:
    """UTC ISO-8601 with microseconds and a trailing Z, like the fixtures."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DatabaseError(RuntimeError):
    pass


class Database:
    def __init__(
        self,
        path: Path | str,
        key_hex: str | None,
        *,
        insecure_plaintext: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self.path = Path(path)
        self._key_hex = key_hex
        if key_hex is None and not insecure_plaintext:
            raise DatabaseError("Refusing to open an unencrypted database.")
        module: Any = sqlite3
        if insecure_plaintext:
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
        else:
            try:
                from sqlcipher3 import dbapi2 as sqlcipher
            except ImportError as exc:  # pragma: no cover
                raise DatabaseError("sqlcipher3 is not installed") from exc
            module = sqlcipher
            self._conn = sqlcipher.connect(str(path), check_same_thread=False)
            # Raw key form: skips the passphrase KDF. Fine, the key is 256 random bits.
            self._conn.execute(f"PRAGMA key = \"x'{key_hex}'\"")
        self._module = module
        self._conn.isolation_level = None  # explicit transactions below
        self._conn.row_factory = module.Row
        try:
            self._conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception as exc:
            raise DatabaseError(
                "Could not read the database. The master key is probably wrong."
            ) from exc
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise DatabaseError(
                    f"This database is from a newer version (schema {version}); "
                    "update the server image instead of downgrading."
                )
            if 0 < version < SCHEMA_VERSION:
                # An upgrade cannot be undone by an older image, so keep a copy first. Best effort:
                # a failure here (a full disk, say) is logged and does not stop the server starting.
                try:
                    from .backup import create_backup

                    create_backup(self, "upgrade", label=f"v{version}-to-v{SCHEMA_VERSION}")
                except Exception:
                    log.exception("Could not back up the database before upgrading it")
            while version < SCHEMA_VERSION:
                script = SCHEMA if version == 0 else MIGRATIONS[version]
                target = SCHEMA_VERSION if version == 0 else version + 1
                self._conn.execute("BEGIN")
                try:
                    for statement in script.split(";\n"):
                        if statement.strip():
                            self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version = {target}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                version = target

    def backup_to(self, target: Path | str) -> None:
        """Write a consistent copy of the database to `target`, encrypted with the same key.
        Other work waits for the few moments this takes."""
        with self._lock:
            dest = self._module.connect(str(target))
            try:
                if self._key_hex is not None:
                    dest.execute(f"PRAGMA key = \"x'{self._key_hex}'\"")
                self._conn.backup(dest)
            finally:
                dest.close()

    def query(self, sql: str, params: tuple | list = ()) -> list[Any]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> Any | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple | list = ()) -> int:
        """Execute a write. Returns the row count (or last row id for INSERT)."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            if sql.lstrip().upper().startswith("INSERT"):
                return cur.lastrowid
            return cur.rowcount

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def placeholders(n: int) -> str:
    return ",".join("?" * n)


def as_dict(row: Any | None) -> dict[str, Any] | None:
    return None if row is None else {k: row[k] for k in row.keys()}
