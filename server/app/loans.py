"""The kinds of loan an account can be counted as, in the order they are listed and shown."""
from __future__ import annotations

# (code, label). `other` comes last: it is where anything unlisted goes.
LOAN_KINDS: list[tuple[str, str]] = [
    ("mortgage", "Mortgage"),
    ("home_equity", "Home equity loan"),
    ("student", "Student loan"),
    ("auto", "Auto loan"),
    ("personal", "Personal loan"),
    ("line_of_credit", "Line of credit"),
    ("business", "Business loan"),
    ("other", "Other loan"),
]
KIND_LABELS = dict(LOAN_KINDS)

# What the bank calls a loan (Plaid's account subtype, lower case), as one of the kinds above.
BANK_SUBTYPES = {
    "mortgage": "mortgage",
    "home equity": "home_equity",
    "student": "student",
    "auto": "auto",
    "consumer": "personal",
    "line of credit": "line_of_credit",
    "business": "business",
    "commercial": "business",
}


def kind_of(chosen: str | None, subtype: str | None) -> str:
    """The kind a loan counts as: the owner's choice, else what the bank's label says, else `other`."""
    if chosen in KIND_LABELS:
        return chosen
    return BANK_SUBTYPES.get((subtype or "").strip().lower(), "other")
