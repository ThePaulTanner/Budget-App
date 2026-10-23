"""Shared startup wiring: settings -> keys -> database -> services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .auth import AuthService
from .backup import BackupScheduler
from .config import Settings
from .crypto import Keys, parse_master_key
from .db import Database
from .merchants import assign_merchants
from .plaid import PlaidClient
from .quotes import QuoteService, YahooProvider
from .sync import SyncManager


@dataclass
class Services:
    settings: Settings
    keys: Keys
    db: Database
    auth: AuthService
    plaid: PlaidClient | None
    sync: SyncManager
    backups: BackupScheduler
    quotes: QuoteService | None = None
    performance: Any = None  # PerformanceService, set up by the API app


def build_services(
    settings: Settings, *, plaid: PlaidClient | None = None, quotes: QuoteService | None = None
) -> Services:
    keys = Keys(parse_master_key(settings.master_key))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(
        settings.db_path,
        None if settings.insecure_plaintext_db else keys.db_hex,
        insecure_plaintext=settings.insecure_plaintext_db,
    )
    assign_merchants(db)  # names for transactions saved before merchants existed; a no-op otherwise
    if plaid is None and settings.plaid_client_id and settings.plaid_secret:
        plaid = PlaidClient(
            settings.plaid_client_id, settings.plaid_secret, settings.plaid_url
        )
    market = quotes if quotes is not None else (QuoteService(YahooProvider()) if settings.live_quotes else None)
    return Services(
        settings=settings,
        keys=keys,
        db=db,
        auth=AuthService(
            db,
            keys,
            max_failed_logins=settings.max_failed_logins,
            web_idle_seconds=settings.web_idle_minutes * 60,
            web_max_seconds=settings.web_max_hours * 3600,
        ),
        plaid=plaid,
        sync=SyncManager(db, keys, plaid, settings, quotes=market),
        backups=BackupScheduler(db),
        quotes=market,
    )
