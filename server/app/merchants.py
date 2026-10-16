"""Merchants: a master list with readable names, to track spending over time.

Every transaction belongs to at most one merchant. How it gets one:
  1. An alias you made ("user") maps raw transaction text to a merchant. It can match
     Plaid's cleaned `merchant_name` or the raw `name`, by equals or contains.
  2. Otherwise, if Plaid supplied a `merchant_name`, a merchant with that name is created
     automatically (with an "auto" alias), so the master list fills itself.
  3. Otherwise the transaction is unassigned: its raw name is non-descriptive and is
     waiting for you to give it a name.
Priority between aliases: yours beat automatic ones, then `equals` beats `contains`,
then the longest pattern, then the newest. Renaming a merchant never breaks the link,
because transactions follow the alias, not the name.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .db import Database, placeholders, utcnow
from .rules import _norm

MAX_NAME = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_name(raw: str) -> str:
    value = " ".join(_CONTROL.sub(" ", raw).split())
    if not value:
        raise ValueError("A merchant name cannot be empty.")
    if len(value) > MAX_NAME:
        raise ValueError(f"A merchant name is at most {MAX_NAME} characters.")
    return value


def alias_matches(alias: Any, tx: Any) -> bool:
    value = tx[alias["match_field"]]
    if value is None:
        return False  # an alias on merchant_name never matches when it is null
    value, pattern = _norm(value), _norm(alias["pattern"])
    return value == pattern if alias["match_type"] == "equals" else pattern in value


def _priority(alias: Any) -> tuple[int, int, int, int]:
    return (
        0 if alias["source"] == "user" else 1,
        0 if alias["match_type"] == "equals" else 1,
        -len(alias["pattern"]),
        -alias["id"],
    )


class AliasIndex:
    """Aliases ready for fast lookup: exact matches by text, "contains" ones in a short list."""

    def __init__(self, aliases: Iterable[Any]) -> None:
        self.equals: dict[tuple[str, str], list[Any]] = {}
        self.contains: list[Any] = []
        for a in aliases:
            if a["match_type"] == "equals":
                self.equals.setdefault((a["match_field"], _norm(a["pattern"])), []).append(a)
            else:
                self.contains.append(a)

    def best(self, tx: Any) -> Any | None:
        found = []
        for field in ("merchant_name", "name"):
            value = tx[field]
            if value is not None:
                found += self.equals.get((field, _norm(value)), [])
        found += [a for a in self.contains if alias_matches(a, tx)]
        return min(found, key=_priority) if found else None


def best_alias(aliases: Iterable[Any], tx: Any) -> Any | None:
    return AliasIndex(aliases).best(tx)


def find_merchant(db: Database, name: str) -> Any | None:
    return db.one("SELECT * FROM merchants WHERE lower(name)=lower(?)", (name,))


def get_or_create_merchant(db: Database, name: str, *, auto: bool) -> Any:
    name = clean_name(name)
    existing = find_merchant(db, name)
    if existing is not None:
        return existing
    merchant_id = db.run(
        "INSERT INTO merchants(name, auto, created_at) VALUES (?,?,?)", (name, int(auto), utcnow())
    )
    return db.one("SELECT * FROM merchants WHERE id=?", (merchant_id,))


def assign_merchants(db: Database, tx_ids: list[str] | None = None) -> int:
    """(Re)compute each transaction's merchant. All transactions, or just `tx_ids`.

    Also creates automatic merchants for Plaid's merchant names, and removes automatic
    merchants nothing uses any more. Returns how many transactions changed.
    """
    index = AliasIndex(dict(a) for a in db.query("SELECT * FROM merchant_aliases"))
    if tx_ids is None:
        rows = db.query("SELECT id, name, merchant_name, merchant_id FROM transactions")
    else:
        rows = []
        for start in range(0, len(tx_ids), 500):
            chunk = tx_ids[start : start + 500]
            rows += db.query(
                "SELECT id, name, merchant_name, merchant_id FROM transactions "
                f"WHERE id IN ({placeholders(len(chunk))})",
                chunk,
            )
    changed = 0
    with db.transaction():
        for tx in rows:
            alias = index.best(tx)
            if alias is None and tx["merchant_name"] and tx["merchant_name"].strip():
                try:
                    merchant = get_or_create_merchant(db, tx["merchant_name"], auto=True)
                except ValueError:
                    merchant = None  # a name too long to use: leave it unassigned
                if merchant is not None:
                    db.run(
                        "INSERT OR IGNORE INTO merchant_aliases(merchant_id, match_field, match_type, "
                        "pattern, source, created_at) VALUES (?,?,?,?,?,?)",
                        (merchant["id"], "merchant_name", "equals", clean_pattern(tx["merchant_name"]), "auto", utcnow()),
                    )
                    alias = db.one(
                        "SELECT * FROM merchant_aliases WHERE match_field='merchant_name' "
                        "AND match_type='equals' AND lower(pattern)=lower(?)",
                        (clean_pattern(tx["merchant_name"]),),
                    )
                    if alias is not None:
                        alias = dict(alias)
                        index.equals.setdefault(("merchant_name", _norm(alias["pattern"])), []).append(alias)
            new_id = alias["merchant_id"] if alias else None
            if new_id != tx["merchant_id"]:
                db.run("UPDATE transactions SET merchant_id=? WHERE id=?", (new_id, tx["id"]))
                changed += 1
        db.run(
            "DELETE FROM merchants WHERE auto=1 "
            "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.merchant_id = merchants.id) "
            "AND NOT EXISTS (SELECT 1 FROM merchant_aliases a WHERE a.merchant_id = merchants.id AND a.source='user')"
        )
    return changed


def clean_pattern(raw: str) -> str:
    """Bank text as stored: whitespace collapsed, so it lines up with how matching reads it."""
    return " ".join(raw.split())


def preview(db: Database, candidate: dict[str, Any]) -> dict[str, Any]:
    """What saving this name would do, using the real priority: transactions a stronger
    existing name already claims are matched but would not move."""
    probe = {**candidate, "id": 10**12, "source": "user", "merchant_id": -1}  # a new name is the newest
    index = AliasIndex([*(dict(a) for a in db.query("SELECT * FROM merchant_aliases")), probe])
    rows = matching_transactions(db, candidate)
    applied = [r for r in rows if index.best(r) is probe]
    shown = applied or rows
    return {
        "matching_transactions": len(rows),
        "would_apply": len(applied),
        "kept_by_other_names": len(rows) - len(applied),
        "currently_unassigned": sum(1 for r in applied if r["merchant_id"] is None),
        "samples": [
            {
                "date": r["date"],
                "name": r["name"],
                "merchant_name": r["merchant_name"],
                "amount": r["amount"],
                "currency": r["currency"],
                "account_name": r["account_name"],
            }
            for r in shown[:5]
        ],
    }


def matching_transactions(db: Database, candidate: dict[str, Any]) -> list[Any]:
    return [
        tx
        for tx in db.query(
            "SELECT t.id, t.date, t.name, t.merchant_name, t.amount, t.currency, t.merchant_id, "
            "a.name AS account_name FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "ORDER BY t.date DESC, t.id"
        )
        if alias_matches(candidate, tx)
    ]
