"""Finds money moving between two of your own linked accounts, so cash flow doesn't count it twice.

A pair is money leaving one account and exactly the same amount, in the same currency, arriving in
a different linked account within MAX_GAP_DAYS. Examples: chequing to savings, paying the Visa from
chequing.

To avoid false matches, the money-out side must look like a transfer: categorised TRANSFER_OUT or
LOAN_PAYMENTS (or uncategorised, when the money-in side says TRANSFER_IN), or it's a payment arriving
in a credit card or loan account. So a $50 grocery purchase never pairs with an unrelated $50
e-Transfer from a friend, and a card refund never pairs with a purchase. e-Transfers to or from
other people have no partner in your own accounts, so they stay counted as real income and spending.

Pairs are recomputed from scratch after every sync. The result doesn't depend on row order, and a
cash-flow choice you make by hand (`cashflow_override`) always wins over it (see cashflow.py).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from .db import Database
from .rules import EFFECTIVE_CATEGORY_SQL, top_level

log = logging.getLogger("homefinance.transfers")

MAX_GAP_DAYS = 3
OUT_TRANSFER_CATEGORIES = frozenset({"TRANSFER_OUT", "LOAN_PAYMENTS"})
DEBT_TYPES = frozenset({"credit", "loan"})


def _looks_like_transfer(out_cat: str | None, out_type: str, in_cat: str | None, in_type: str) -> bool:
    if out_cat in OUT_TRANSFER_CATEGORIES:
        return True
    if in_cat == "TRANSFER_IN" and out_cat in (None, "OTHER"):
        return True
    # Paying a card or loan: money into a debt account that isn't a merchant refund.
    return in_type in DEBT_TYPES and out_type not in DEBT_TYPES and in_cat in (None, "OTHER", "TRANSFER_IN", "LOAN_PAYMENTS")


def find_pairs(rows: list[dict[str, Any]]) -> dict[str, str]:
    """rows: dicts with id, account_id, account_type, date (YYYY-MM-DD), amount, currency and
    category (the effective one). Returns {transaction id: partner id}, both directions."""
    outflows, inflows = [], defaultdict(list)
    for r in rows:
        amount = Decimal(str(r["amount"]))
        if not amount:
            continue
        item = {**r, "_day": date.fromisoformat(r["date"]), "_top": top_level(r["category"])}
        key = (r["currency"], abs(amount))
        (outflows.append((item, key)) if amount > 0 else inflows[key].append(item))

    outflows.sort(key=lambda o: (o[0]["_day"], o[0]["id"]))  # oldest first, ties by id: order-independent
    for key in inflows:
        inflows[key].sort(key=lambda i: (i["_day"], i["id"]))

    pairs: dict[str, str] = {}
    for out, key in outflows:
        best, best_score = None, None
        for inc in inflows.get(key, ()):
            if inc["id"] in pairs or inc["account_id"] == out["account_id"]:
                continue
            gap = abs((inc["_day"] - out["_day"]).days)
            if gap > MAX_GAP_DAYS or not _looks_like_transfer(out["_top"], out["account_type"], inc["_top"], inc["account_type"]):
                continue
            # Closest date first; on a tie prefer a pair where both sides are labelled as transfers.
            labelled = out["_top"] in OUT_TRANSFER_CATEGORIES and inc["_top"] in ("TRANSFER_IN", "LOAN_PAYMENTS")
            score = (gap, 0 if labelled else 1)
            if best_score is None or score < best_score:
                best, best_score = inc, score
        if best is not None:
            pairs[out["id"]] = best["id"]
            pairs[best["id"]] = out["id"]
    return pairs


def match_transfers(db: Database) -> int:
    """Recompute every pair and save the rows that changed. Returns the number of pairs."""
    rows = [
        {k: r[k] for k in r.keys()}
        for r in db.query(
            f"SELECT t.id, t.account_id, a.type AS account_type, t.date, t.amount, t.currency, "
            f"t.transfer_pair_id, {EFFECTIVE_CATEGORY_SQL} AS category "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id"
        )
    ]
    pairs = find_pairs(rows)
    changed = [(pairs.get(r["id"]), r["id"]) for r in rows if r["transfer_pair_id"] != pairs.get(r["id"])]
    if changed:
        with db.transaction():
            for partner, tx_id in changed:
                db.run("UPDATE transactions SET transfer_pair_id = ? WHERE id = ?", (partner, tx_id))
    log.info("Transfer matching: %d pairs (%d rows updated)", len(pairs) // 2, len(changed))
    return len(pairs) // 2
