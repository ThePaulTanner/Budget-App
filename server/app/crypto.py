"""Key derivation and field encryption.

One 32-byte master key is the only secret you keep outside the database. Every
other key is derived from it with HKDF-SHA256, one per purpose, so a leak of one
derived key does not reveal the others.

Plaid access tokens are encrypted with envelope encryption: each token gets its
own random data key (AES-256-GCM), and that data key is wrapped by a key-encryption
key derived from the master key. The row id is bound in as associated data, so a
ciphertext copied onto another row will not decrypt.
"""

from __future__ import annotations

import os
import re

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_VERSION = b"\x01"
_NONCE = 12
_KEY = 32
_TAG = 16
_WRAPPED_LEN = _NONCE + _KEY + _TAG

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def generate_master_key() -> str:
    """A fresh 256-bit master key as 64 hex characters."""
    return os.urandom(32).hex()


def parse_master_key(value: str | None) -> bytes:
    if not value:
        raise ValueError(
            "No master key. Set HF_MASTER_KEY_FILE (recommended) or HF_MASTER_KEY. "
            "Create one with: python -m app.cli gen-key"
        )
    value = value.strip()
    if not _HEX64.match(value):
        raise ValueError("The master key must be exactly 64 hex characters (32 bytes).")
    return bytes.fromhex(value)


def derive(master: bytes, purpose: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"homefinance/{purpose}/v1".encode(),
    ).derive(master)


class Keys:
    """All purpose-specific keys, derived once at startup."""

    def __init__(self, master: bytes) -> None:
        self.db = derive(master, "database")
        self.kek = derive(master, "key-encryption")
        self.jwt = derive(master, "access-token-signing")
        self.pepper = derive(master, "password-pepper")

    @property
    def db_hex(self) -> str:
        return self.db.hex()


def seal(plaintext: bytes, kek: bytes, aad: bytes) -> bytes:
    """Envelope-encrypt `plaintext`. Output: version | wrapped data key | ciphertext."""
    dek = os.urandom(_KEY)
    nonce1 = os.urandom(_NONCE)
    wrapped = nonce1 + AESGCM(kek).encrypt(nonce1, dek, aad)
    nonce2 = os.urandom(_NONCE)
    body = nonce2 + AESGCM(dek).encrypt(nonce2, plaintext, aad)
    return _VERSION + wrapped + body


def unseal(blob: bytes, kek: bytes, aad: bytes) -> bytes:
    if not blob or blob[:1] != _VERSION:
        raise ValueError("Unknown ciphertext version")
    wrapped = blob[1 : 1 + _WRAPPED_LEN]
    body = blob[1 + _WRAPPED_LEN :]
    dek = AESGCM(kek).decrypt(wrapped[:_NONCE], wrapped[_NONCE:], aad)
    return AESGCM(dek).decrypt(body[:_NONCE], body[_NONCE:], aad)
