"""Facts about a held position that more than one part of the server needs."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .sync import MARKET_CURRENCY


def bank_price_and_value(row: Any) -> tuple[Decimal | None, Decimal | None]:
    """The bank's (price, value) for a holding, with anything unusable treated as missing.
    Some banks send 0 as the price or value of a position they cannot price. That is not a real
    $0, and would show the position as worthless. Each is judged on its own: a missing price
    stays missing, and a missing value is worked out from the quantity and a good price."""
    price = None if row["price"] is None else Decimal(row["price"])
    value = None if row["value"] is None else Decimal(row["value"])
    quantity = None if row["quantity"] is None else Decimal(row["quantity"])
    if quantity is None or quantity <= 0:
        return price, value
    if price is not None and price <= 0:
        price = None
    if value is not None and value <= 0:
        value = None
    if value is None and price is not None:
        value = (quantity * price).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return price, value


def effective_currency(row) -> str:
    """The currency to treat a position as being in. A bank that sends no price and no value for a
    position gives nothing to check its currency label against (some banks label everything with the
    account's currency), so a position on a market with one currency, such as Nasdaq, is taken to be
    in that currency. It is applied when a position is read, not stored: once a bank does send a price,
    the bank's label is checkable again and is used as it is."""
    price, value = bank_price_and_value(row)
    if price is None and value is None:
        implied = MARKET_CURRENCY.get((row["mic"] or "").upper())
        if implied:
            return implied
    return row["currency"]
