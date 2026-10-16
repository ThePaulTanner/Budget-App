"""Shared startup wiring: settings -> keys -> database -> services."""

from __future__ import annotations

from dataclasses import dataclass

from .auth import AuthService
from .config import Settings
from .crypto import Keys, parse_master_key
from .db import Database
from .plaid import PlaidClient
from .sync import SyncManager


@dataclass
class Services:
    settings: Settings
    keys: Keys
    db: Database
    auth: AuthService
    plaid: PlaidClient | None
    sync: SyncManager


def build_services(settings: Settings, *, plaid: PlaidClient | None = None) -> Services:
    keys = Keys(parse_master_key(settings.master_key))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(
        settings.db_path,
        None if settings.insecure_plaintext_db else keys.db_hex,
        insecure_plaintext=settings.insecure_plaintext_db,
    )
    if plaid is None and settings.plaid_client_id and settings.plaid_secret:
        plaid = PlaidClient(
            settings.plaid_client_id, settings.plaid_secret, settings.plaid_url
        )
    return Services(
        settings=settings,
        keys=keys,
        db=db,
        auth=AuthService(db, keys, max_failed_logins=settings.max_failed_logins),
        plaid=plaid,
        sync=SyncManager(db, keys, plaid, settings),
    )
