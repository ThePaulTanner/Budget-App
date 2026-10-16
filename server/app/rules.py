"""Categorization rules.

Priority for a transaction's category: a manual change wins over a rule, and a rule
wins over Plaid's own category. When several rules match, an `equals` rule beats a
`contains` rule; among `contains` rules the longest pattern wins (more specific beats
generic), then the newest rule.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .db import Database, placeholders

# Categories that are not "spending": moving money around, paying debts, or income.
EXCLUDED_FROM_SPENDING = frozenset({"INCOME", "TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS"})

# Plaid's top-level categories. A detailed code such as FOOD_AND_DRINK_FAST_FOOD starts with its
# top-level one, which is how a sub-category finds its parent.
PRIMARY_CATEGORIES = (
    "INCOME", "TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "BANK_FEES", "ENTERTAINMENT",
    "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT", "MEDICAL", "PERSONAL_CARE",
    "GENERAL_SERVICES", "GOVERNMENT_AND_NON_PROFIT", "TRANSPORTATION", "TRAVEL",
    "RENT_AND_UTILITIES", "OTHER",
)

EFFECTIVE_CATEGORY_SQL = "COALESCE(override_category, rule_category, plaid_category)"

_CATEGORY = re.compile(r"^[A-Z0-9][A-Z0-9_&'\-]{0,59}$")


def normalize_category(raw: str) -> str:
    value = re.sub(r"\s+", "_", raw.strip().upper())
    if not _CATEGORY.match(value):
        raise ValueError("A category is 1-60 letters, numbers, spaces, & ' or -.")
    return value


def parent_category(category: str | None) -> str | None:
    """FOOD_AND_DRINK_FAST_FOOD -> FOOD_AND_DRINK. None when it is not a sub-category."""
    if not category:
        return None
    best = None
    for primary in PRIMARY_CATEGORIES:
        if category.startswith(primary + "_") and (best is None or len(primary) > len(best)):
            best = primary
    return best


def top_level(category: str | None) -> str | None:
    return parent_category(category) or category


def is_excluded(category: str | None) -> bool:
    """Not spending (transfers, debt payments, income), including their sub-categories."""
    return top_level(category) in EXCLUDED_FROM_SPENDING


def split_category(row: Any) -> tuple[str | None, str | None]:
    """(top-level category, sub-category or None) for a transaction row.

    Plaid's own category has a sub-category in `category_detailed`. A category the user (or a
    rule) chose is a sub-category only when it is written as one, e.g. FOOD_AND_DRINK_FAST_FOOD.
    Spending counts each transaction once, in its top-level category; the sub-category is a
    finer view of the same money."""
    chosen = row["override_category"] if row["override_category"] is not None else row["rule_category"]
    if chosen is not None:
        parent = parent_category(chosen)
        return (parent, chosen) if parent else (chosen, None)
    primary = row["plaid_category"]
    detailed = row["category_detailed"]
    if primary is not None and detailed and parent_category(detailed) == primary:
        return primary, detailed
    return primary, None


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def rule_matches(rule: Any, tx: Any) -> bool:
    if rule["account_id"] and rule["account_id"] != tx["account_id"]:
        return False
    value = tx[rule["match_field"]]
    if value is None:
        return False  # a rule on merchant_name never matches when it is null
    value, pattern = _norm(value), _norm(rule["pattern"])
    if rule["match_type"] == "equals":
        return value == pattern
    return pattern in value


def _priority(rule: Any) -> tuple[int, int, int]:
    return (0 if rule["match_type"] == "equals" else 1, -len(rule["pattern"]), -rule["id"])


def best_rule(rules: Iterable[Any], tx: Any) -> Any | None:
    matches = [r for r in rules if rule_matches(r, tx)]
    return min(matches, key=_priority) if matches else None


def assign_rules(db: Database, tx_ids: list[str] | None = None) -> int:
    """(Re)compute rule categories. All transactions, or just `tx_ids`.

    Returns how many transactions changed.
    """
    rules = db.query("SELECT * FROM rules")
    if tx_ids is None:
        rows = db.query("SELECT id, account_id, name, merchant_name, rule_id FROM transactions")
    else:
        rows = []
        for start in range(0, len(tx_ids), 500):
            chunk = tx_ids[start : start + 500]
            rows += db.query(
                "SELECT id, account_id, name, merchant_name, rule_id FROM transactions "
                f"WHERE id IN ({placeholders(len(chunk))})",
                chunk,
            )
    changed = 0
    with db.transaction():
        for tx in rows:
            rule = best_rule(rules, tx)
            new_id = rule["id"] if rule else None
            if new_id != tx["rule_id"]:
                db.run(
                    "UPDATE transactions SET rule_id=?, rule_category=? WHERE id=?",
                    (new_id, rule["category"] if rule else None, tx["id"]),
                )
                changed += 1
            elif rule is not None:
                db.run(
                    "UPDATE transactions SET rule_category=? WHERE id=? AND rule_category IS NOT ?",
                    (rule["category"], tx["id"], rule["category"]),
                )
    return changed


def reassign_for_rule(db: Database, rule_id: int) -> None:
    """After a rule is edited or deleted, re-evaluate the transactions that used it."""
    ids = [r["id"] for r in db.query("SELECT id FROM transactions WHERE rule_id=?", (rule_id,))]
    if ids:
        assign_rules(db, ids)


def preview(db: Database, candidate: dict[str, Any]) -> dict[str, int]:
    """What would happen if this rule were saved and applied to existing transactions."""
    probe = {**candidate, "id": 10**12}  # a new rule is the newest
    rules = [*db.query("SELECT * FROM rules"), probe]
    matching = would_change = skipped_manual = into_excluded = out_of_excluded = 0
    for tx in db.query(
        "SELECT id, account_id, name, merchant_name, override_category, "
        f"{EFFECTIVE_CATEGORY_SQL} AS effective FROM transactions"
    ):
        if not rule_matches(probe, tx):
            continue
        matching += 1
        if tx["override_category"] is not None:
            skipped_manual += 1
            continue
        winner = best_rule(rules, tx)
        if winner is not probe or tx["effective"] == probe["category"]:
            continue
        would_change += 1
        was_excluded = is_excluded(tx["effective"])
        will_be_excluded = is_excluded(probe["category"])
        if will_be_excluded and not was_excluded:
            into_excluded += 1
        elif was_excluded and not will_be_excluded:
            out_of_excluded += 1
    return {
        "matching_transactions": matching,
        "would_change": would_change,
        "skipped_manual": skipped_manual,
        "moves_into_excluded": into_excluded,
        "moves_out_of_excluded": out_of_excluded,
    }
