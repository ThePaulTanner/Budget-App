"""Settings, read from environment variables.

Secrets can be given as a value (`NAME`) or as a path to a file holding the value
(`NAME_FILE`, for Docker secrets). The file form wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _secret(name: str, default: str | None = None) -> str | None:
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text().strip()
    return os.environ.get(name, default)


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    master_key: str | None = None  # 64 hex characters (32 bytes)
    plaid_client_id: str | None = None
    plaid_secret: str | None = None
    plaid_env: str = "sandbox"  # sandbox | production
    plaid_country_codes: tuple[str, ...] = ("CA", "US")
    public_webhook_url: str | None = None  # where Plaid should send webhooks
    base_currency: str = "CAD"
    api_port: int = 8080
    webhook_port: int = 8081
    host: str = "0.0.0.0"
    sync_interval_minutes: int = 60
    live_quotes: bool = True  # live prices for investment positions from Yahoo Finance
    max_failed_logins: int = 10  # in a row; then new sign-ins stay locked until `cli unlock`
    # Test-only escape hatches. Never set these in production.
    insecure_plaintext_db: bool = False
    plaid_base_url: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "homefinance.db"

    @property
    def plaid_url(self) -> str:
        if self.plaid_base_url:
            return self.plaid_base_url
        return {
            "sandbox": "https://sandbox.plaid.com",
            "production": "https://production.plaid.com",
        }[self.plaid_env]


def load_settings() -> Settings:
    env = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
    if env not in {"sandbox", "production"}:
        raise SystemExit("PLAID_ENV must be 'sandbox' or 'production'")
    countries = tuple(
        c.strip().upper()
        for c in os.environ.get("PLAID_COUNTRY_CODES", "CA,US").split(",")
        if c.strip()
    )
    return Settings(
        data_dir=Path(os.environ.get("HF_DATA_DIR", "/data")),
        master_key=_secret("HF_MASTER_KEY"),
        plaid_client_id=_secret("PLAID_CLIENT_ID"),
        plaid_secret=_secret("PLAID_SECRET"),
        plaid_env=env,
        plaid_country_codes=countries,
        public_webhook_url=os.environ.get("PUBLIC_WEBHOOK_URL") or None,
        base_currency=os.environ.get("BASE_CURRENCY", "CAD").strip().upper(),
        api_port=int(os.environ.get("API_PORT", "8080")),
        webhook_port=int(os.environ.get("WEBHOOK_PORT", "8081")),
        host=os.environ.get("HF_HOST", "0.0.0.0"),
        sync_interval_minutes=int(os.environ.get("SYNC_INTERVAL_MINUTES", "60")),
        live_quotes=_bool("LIVE_QUOTES", True),
        max_failed_logins=max(3, min(100, int(os.environ.get("MAX_FAILED_LOGINS", "10")))),
        insecure_plaintext_db=_bool("HF_INSECURE_PLAINTEXT_DB"),
    )
