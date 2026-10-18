"""Passkeys: sign in with Face ID. A software authenticator stands in for the phone."""

from __future__ import annotations

import hashlib
import json
import struct

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app import passkeys

from .conftest import PASSWORD

HOST = "testserver"
WEB = {"X-Requested-With": "HomeFinance"}


def cbor(value) -> bytes:
    """Just enough CBOR to build what a phone sends."""
    def head(kind: int, n: int) -> bytes:
        if n < 24:
            return bytes([kind << 5 | n])
        if n < 256:
            return bytes([kind << 5 | 24, n])
        return bytes([kind << 5 | 25]) + n.to_bytes(2, "big")

    if isinstance(value, bool):
        raise TypeError
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(type(value))


class Phone:
    """A device with a Face ID passkey: makes registration and sign-in answers."""

    def __init__(self, host: str = HOST):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b"cred-" + hashlib.sha256(str(id(self)).encode()).digest()[:12]
        self.host, self.counter = host, 0

    def _auth_data(self, flags: int, attested: bool = False) -> bytes:
        data = hashlib.sha256(self.host.encode()).digest() + bytes([flags]) + struct.pack(">I", self.counter)
        if attested:
            numbers = self.key.public_key().public_numbers()
            cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
            data += bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + cbor(cose)
        return data

    def _client(self, kind: str, challenge: str, origin: str | None = None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or f"https://{self.host}"}).encode()

    def register(self, options: dict, *, flags: int = 0x45, origin: str | None = None, name: str = "Paul's iPhone") -> dict:
        client = self._client("webauthn.create", options["publicKey"]["challenge"], origin)
        attestation = cbor({"fmt": "none", "attStmt": {}, "authData": self._auth_data(flags, attested=True)})
        return {"handle": options["handle"], "name": name, "client_data": passkeys.b64u(client), "attestation": passkeys.b64u(attestation)}

    def sign_in(self, options: dict, *, flags: int = 0x05, origin: str | None = None, counter: int | None = None, tamper: bool = False) -> dict:
        self.counter = self.counter + 1 if counter is None else counter
        client = self._client("webauthn.get", options["publicKey"]["challenge"], origin)
        auth_data = self._auth_data(flags)
        signature = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        if tamper:
            signature = signature[:-1] + bytes([signature[-1] ^ 1])
        return {
            "handle": options["handle"], "credential_id": passkeys.b64u(self.credential_id),
            "client_data": passkeys.b64u(client), "authenticator_data": passkeys.b64u(auth_data), "signature": passkeys.b64u(signature),
        }


def add_passkey(client, auth, phone=None, password=PASSWORD, **kwargs):
    phone = phone or Phone()
    options = client.post("/api/passkeys/options", headers=auth).json()
    added = client.post("/api/passkeys", headers=auth, json={**phone.register(options, **kwargs), "password": password})
    return phone, added


def sign_in(client, phone, **kwargs):
    options = client.post("/web/passkey/options", headers=WEB).json()
    return client.post("/web/passkey/login", headers=WEB, json=phone.sign_in(options, **kwargs))


def test_register_then_sign_in_with_the_passkey(client, auth):
    phone, added = add_passkey(client, auth)
    assert added.status_code == 201 and added.json()["name"] == "Paul's iPhone"
    listed = client.get("/api/passkeys", headers=auth).json()
    assert [p["name"] for p in listed] == ["Paul's iPhone"] and listed[0]["last_used_at"] is None

    signed = sign_in(client, phone)
    assert signed.status_code == 200
    token = signed.json()["access_token"]
    assert client.get("/api/version", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/api/passkeys", headers=auth).json()[0]["last_used_at"] is not None
    events = client.get("/auth/login-log", headers=auth).json()["events"]
    assert any(e["result"] == "success" and e["method"] == "passkey" and e["username"] == "paul" for e in events)


def test_challenge_works_once(client, auth):
    phone, _ = add_passkey(client, auth)
    options = client.post("/web/passkey/options", headers=WEB).json()
    answer = phone.sign_in(options)
    assert client.post("/web/passkey/login", headers=WEB, json=answer).status_code == 200
    assert client.post("/web/passkey/login", headers=WEB, json=answer).status_code == 401


@pytest.mark.parametrize(
    "kwargs",
    [{"flags": 0x01}, {"flags": 0x04}, {"origin": "https://evil.example"}, {"tamper": True}],
    ids=["no-face-id", "no-presence", "other-site", "bad-signature"],
)
def test_bad_answers_are_refused_and_logged(client, auth, kwargs):
    phone, _ = add_passkey(client, auth)
    assert sign_in(client, phone, **kwargs).status_code == 401
    events = client.get("/auth/login-log", headers=auth).json()["events"]
    assert any(e["result"] == "bad_passkey" for e in events)
    assert sign_in(client, phone).status_code == 200  # one bad try does not lock anything


def test_unknown_passkey_is_refused(client, auth):
    add_passkey(client, auth)
    assert sign_in(client, Phone()).status_code == 401


def test_counter_going_backwards_is_refused(client, auth):
    phone, _ = add_passkey(client, auth)
    assert sign_in(client, phone, counter=5).status_code == 200
    assert sign_in(client, phone, counter=5).status_code == 401
    assert sign_in(client, phone, counter=6).status_code == 200


def test_synced_passkeys_that_always_report_zero_are_fine(client, auth):
    phone, _ = add_passkey(client, auth)
    assert sign_in(client, phone, counter=0).status_code == 200
    assert sign_in(client, phone, counter=0).status_code == 200


def test_registration_refuses_wrong_site_and_no_face_id(client, auth):
    for kwargs in ({"origin": "https://evil.example"}, {"flags": 0x41}):
        _, added = add_passkey(client, auth, **kwargs)
        assert added.status_code == 400
    assert client.get("/api/passkeys", headers=auth).json() == []


def test_the_same_passkey_cannot_be_added_twice(client, auth):
    phone, _ = add_passkey(client, auth)
    _, again = add_passkey(client, auth, phone)
    assert again.status_code == 400


def test_locked_sign_in_refuses_passkeys_too(client, auth, services, login):
    phone, _ = add_passkey(client, auth)
    services.db.run("UPDATE users SET locked_at='2026-10-02T00:00:00.000000Z'")
    assert sign_in(client, phone).status_code == 423


def test_passkeys_belong_to_their_login_and_can_be_removed(client, auth, services, clock):
    import pyotp

    phone, added = add_passkey(client, auth)
    secret, _, _ = services.auth.create_user("sam", "another long password")
    clock.now += 31  # a fresh authenticator step
    code = pyotp.TOTP(secret).at(int(clock() // 30) * 30)
    token = client.post(
        "/auth/login", json={"username": "sam", "password": "another long password", "code": code, "device_name": "Sam"}
    ).json()["access_token"]
    sams = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/passkeys", headers=sams).json() == []
    assert client.delete(f"/api/passkeys/{added.json()['id']}", headers=sams).status_code == 404
    assert client.delete(f"/api/passkeys/{added.json()['id']}", headers=auth).status_code == 204
    assert sign_in(client, phone).status_code == 401


def test_passkeys_fixture_matches_the_shape(client, auth):
    import json
    from pathlib import Path

    add_passkey(client, auth)
    fixture = json.loads((Path(__file__).resolve().parents[2] / "api-fixtures/passkeys.json").read_text())
    got = client.get("/api/passkeys", headers=auth).json()
    assert set(fixture[0]) == set(got[0])


def test_passkey_pages_need_a_login(client):
    assert client.get("/api/passkeys").status_code == 401
    assert client.post("/api/passkeys/options").status_code == 401


def test_web_endpoints_refuse_other_sites(client):
    assert client.post("/web/passkey/options").status_code == 403


def test_cbor_reader_roundtrip_and_garbage():
    sample = {"a": [1, -7, b"xy", "hi"], 3: {-1: 2}}
    assert passkeys.cbor_decode(cbor(sample)) == sample
    with pytest.raises(passkeys.PasskeyError):
        passkeys.cbor_decode(b"\xff")
    with pytest.raises(passkeys.PasskeyError):
        passkeys.cbor_decode(b"\x45ab")


def test_ip_addresses_are_not_valid_relying_parties():
    with pytest.raises(passkeys.PasskeyError):
        passkeys.rp_id_for("192.168.1.5:8080")
    assert passkeys.rp_id_for("tanner-nas.example.ts.net") == "tanner-nas.example.ts.net"


def test_adding_a_passkey_needs_the_password_again(client, auth):
    _, added = add_passkey(client, auth, password="not the password")
    assert added.status_code == 400 and added.json()["detail"] == "Wrong password"
    assert client.get("/api/passkeys", headers=auth).json() == []


def test_guessing_the_password_there_is_rate_limited(client, auth):
    codes = [add_passkey(client, auth, password=f"wrong guess {i}")[1].status_code for i in range(7)]
    assert codes[:5] == [400] * 5 and codes[5] == 429
    assert add_passkey(client, auth)[1].status_code == 429  # even the right one waits


def test_cbor_map_with_a_container_key_is_a_clean_refusal():
    with pytest.raises(passkeys.PasskeyError):
        passkeys.cbor_decode(bytes([0xA1, 0x80, 0x01]))


def test_a_counter_that_falls_back_to_zero_is_refused(client, auth):
    phone, _ = add_passkey(client, auth)
    assert sign_in(client, phone, counter=3).status_code == 200
    assert sign_in(client, phone, counter=0).status_code == 401
