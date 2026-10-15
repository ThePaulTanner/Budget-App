from __future__ import annotations

import pytest

from app.crypto import Keys, generate_master_key, parse_master_key, seal, unseal
from app.db import Database, DatabaseError

from .conftest import seed_fixture_data


def test_database_files_contain_no_readable_data(services, tmp_path):
    seed_fixture_data(services)
    services.db.run("PRAGMA wal_checkpoint(FULL)")
    blobs = [p.read_bytes() for p in tmp_path.iterdir() if p.is_file()]
    assert blobs, "expected database files"
    for blob in blobs:
        assert not blob.startswith(b"SQLite format 3")
        # Needles of 4+ bytes only: a 3-byte one ("IGA") turns up in random ciphertext by chance.
        for secret in (b"Desjardins", b"Restaurant Le Local", b"access-sandbox-SECRET", b"paul"):
            assert secret not in blob, secret


def test_wrong_key_cannot_open_the_database(services, tmp_path):
    services.db.close()
    other = Keys(parse_master_key(generate_master_key()))
    with pytest.raises(DatabaseError):
        Database(services.settings.db_path, other.db_hex)


def test_refuses_to_open_unencrypted_by_accident(tmp_path):
    with pytest.raises(DatabaseError, match="unencrypted"):
        Database(tmp_path / "x.db", None)


def test_plaid_tokens_are_encrypted_again_inside_the_database(services):
    seed_fixture_data(services)
    blob = bytes(services.db.one("SELECT access_token FROM items")["access_token"])
    assert b"access-sandbox-SECRET" not in blob
    assert unseal(blob, services.keys.kek, b"item:item_desj") == b"access-sandbox-SECRET"
    with pytest.raises(Exception):  # bound to its row: copying it elsewhere fails
        unseal(blob, services.keys.kek, b"item:another")
    with pytest.raises(Exception):
        unseal(blob, Keys(parse_master_key(generate_master_key())).kek, b"item:item_desj")


def test_each_seal_uses_fresh_keys_and_nonces():
    kek = Keys(parse_master_key(generate_master_key())).kek
    assert seal(b"same", kek, b"a") != seal(b"same", kek, b"a")


def test_derived_keys_are_distinct():
    keys = Keys(parse_master_key(generate_master_key()))
    assert len({keys.db, keys.kek, keys.jwt, keys.pepper}) == 4


@pytest.mark.parametrize("bad", [None, "", "short", "z" * 64, "ab" * 31, "ab" * 33])
def test_bad_master_keys_are_refused(bad):
    with pytest.raises(ValueError):
        parse_master_key(bad)


def test_totp_secret_is_encrypted_at_rest(login, services):
    stored = bytes(services.db.one("SELECT totp_secret FROM users")["totp_secret"])
    assert login.secret.encode() not in stored


def test_security_headers_present(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_server_runs_encrypted_by_default():
    from app.config import Settings
    assert Settings().insecure_plaintext_db is False
