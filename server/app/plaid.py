"""A small Plaid client over httpx.

Only read-only products are ever requested: transactions, plus investments and
liabilities (credit card due dates) when the bank has them. Auth (account numbers) and Transfer (moving money) are never used.
JSON numbers are parsed straight into `Decimal`, so amounts like 132.47 stay exact.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import httpx

CLIENT_NAME = "HomeFinance"


class PlaidError(Exception):
    def __init__(self, status: int, error_type: str, error_code: str, message: str):
        super().__init__(f"{error_code}: {message}")
        self.status = status
        self.error_type = error_type
        self.error_code = error_code
        self.message = message


class PlaidClient:
    def __init__(
        self,
        client_id: str,
        secret: str,
        base_url: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._client_id = client_id
        self._secret = secret
        self._http = client or httpx.Client(base_url=base_url, timeout=60.0)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = {"client_id": self._client_id, "secret": self._secret, **body}
        try:
            response = self._http.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise PlaidError(0, "NETWORK", "UNREACHABLE", "Could not reach Plaid") from exc
        try:
            data = json.loads(response.text, parse_float=Decimal)
        except ValueError:
            raise PlaidError(
                response.status_code, "API_ERROR", "BAD_RESPONSE", "Plaid sent an unreadable reply"
            ) from None
        if response.status_code != 200:
            raise PlaidError(
                response.status_code,
                str(data.get("error_type", "API_ERROR")),
                str(data.get("error_code", "UNKNOWN")),
                str(data.get("error_message", "Plaid request failed")),
            )
        return data

    # -- linking -----------------------------------------------------------

    def link_token_create(
        self,
        client_user_id: str,
        country_codes: tuple[str, ...],
        webhook: str | None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "client_name": CLIENT_NAME,
            "language": "en",
            "country_codes": list(country_codes),
            "user": {"client_user_id": client_user_id},
        }
        if webhook:
            body["webhook"] = webhook
        if access_token:
            # Update mode (reconnect an existing bank): Plaid wants no products here.
            body["access_token"] = access_token
        else:
            body["products"] = ["transactions"]
            body["optional_products"] = ["investments", "liabilities"]
        return self._post("/link/token/create", body)

    def exchange_public_token(self, public_token: str) -> dict[str, Any]:
        return self._post("/item/public_token/exchange", {"public_token": public_token})

    def item_get(self, access_token: str) -> dict[str, Any]:
        return self._post("/item/get", {"access_token": access_token})

    def institution_details(self, institution_id: str, country_codes: tuple[str, ...]) -> dict[str, Any] | None:
        """The bank's name, its logo (a base64 PNG) and brand colour. None if Plaid cannot say."""
        try:
            data = self._post(
                "/institutions/get_by_id",
                {
                    "institution_id": institution_id,
                    "country_codes": list(country_codes),
                    "options": {"include_optional_metadata": True},
                },
            )
        except PlaidError:
            return None
        bank = data.get("institution")
        if not isinstance(bank, dict):
            return None  # an unexpected reply is treated like a failed lookup
        return {"name": bank.get("name"), "logo": bank.get("logo"), "color": bank.get("primary_color")}

    def institution_name(self, institution_id: str, country_codes: tuple[str, ...]) -> str | None:
        details = self.institution_details(institution_id, country_codes)
        return details["name"] if details else None

    def item_remove(self, access_token: str) -> None:
        self._post("/item/remove", {"access_token": access_token})

    # -- data --------------------------------------------------------------

    def accounts_get(self, access_token: str) -> dict[str, Any]:
        return self._post("/accounts/get", {"access_token": access_token})

    def transactions_sync(
        self, access_token: str, cursor: str | None, count: int = 500
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"access_token": access_token, "count": count}
        if cursor:
            body["cursor"] = cursor
        return self._post("/transactions/sync", body)

    def investments_holdings_get(self, access_token: str) -> dict[str, Any]:
        return self._post("/investments/holdings/get", {"access_token": access_token})

    def investments_transactions_get(
        self, access_token: str, start_date: str, end_date: str, offset: int = 0, count: int = 500
    ) -> dict[str, Any]:
        return self._post(
            "/investments/transactions/get",
            {
                "access_token": access_token,
                "start_date": start_date,
                "end_date": end_date,
                "options": {"count": count, "offset": offset},
            },
        )

    def liabilities_get(self, access_token: str) -> dict[str, Any]:
        return self._post("/liabilities/get", {"access_token": access_token})

    # -- webhooks ----------------------------------------------------------

    def webhook_verification_key(self, key_id: str) -> dict[str, Any]:
        return self._post("/webhook_verification_key/get", {"key_id": key_id})["key"]


def is_retryable_not_ready(error: PlaidError) -> bool:
    return error.error_code in {"PRODUCT_NOT_READY"}


def sleep_backoff(attempt: int) -> None:  # pragma: no cover - trivial
    time.sleep(min(2**attempt, 10))
