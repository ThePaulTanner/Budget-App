"""Bank logos: taken from Plaid when a bank is linked, or at its next sync, and checked before use."""

from __future__ import annotations

import base64

from app.sync import clean_color, clean_logo

from .conftest import TEST_LOGO
from .test_sync import ACCOUNTS, add_item, page, run


def test_clean_logo_accepts_only_a_small_png():
    assert clean_logo(TEST_LOGO) == TEST_LOGO
    assert clean_logo(None) is None and clean_logo("") is None and clean_logo(123) is None
    assert clean_logo("not base64 !!") is None
    assert clean_logo(base64.b64encode(b"<svg onload=alert(1)>").decode()) is None  # not a PNG
    assert clean_logo(base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 200_000).decode()) is None  # too large


def test_clean_color():
    assert clean_color("#00a651") == "#00a651"
    for bad in [None, "red", "#fff", "#00a651; background:url(x)", "00a651"]:
        assert clean_color(bad) is None


def test_items_carry_the_logo(seeded):
    client, auth = seeded
    item = client.get("/api/items", headers=auth).json()[0]
    assert item["logo"] == TEST_LOGO and item["color"] == "#00a651"


def test_a_new_bank_gets_its_logo_when_linked(services, plaid, client, auth):
    r = client.post("/api/link/exchange", json={"public_token": "public-sandbox-1"}, headers=auth)
    assert r.status_code == 200, r.text
    row = services.db.one("SELECT logo, color, logo_checked FROM items WHERE id='item_new'")
    assert (row["logo"], row["color"], row["logo_checked"]) == (TEST_LOGO, "#00a651", 1)


def test_an_existing_bank_is_filled_in_at_its_next_sync_once(services, plaid):
    item = add_item(services)
    services.db.run("UPDATE items SET institution_id='ins_1' WHERE id='item_desj'")
    item = services.db.one("SELECT * FROM items WHERE id='item_desj'")
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(), page(cursor="c2")]
    assert run(services, item)["ok"] is True
    row = services.db.one("SELECT logo, logo_checked FROM items")
    assert (row["logo"], row["logo_checked"]) == (TEST_LOGO, 1)
    assert run(services, services.db.one("SELECT * FROM items"))["ok"] is True
    assert plaid.calls.count("institution_details") == 1  # asked once, not at every sync


def test_a_failed_lookup_is_retried_and_never_fails_the_sync(services, plaid):
    add_item(services)
    services.db.run("UPDATE items SET institution_id='ins_1' WHERE id='item_desj'")
    plaid.accounts = ACCOUNTS
    plaid.pages = [page(), page(cursor="c2")]
    plaid.errors["institution_details"] = object()  # the double returns None, as the real client does on a Plaid error
    assert run(services, services.db.one("SELECT * FROM items"))["ok"] is True
    assert services.db.one("SELECT logo, logo_checked FROM items")["logo_checked"] == 0
    del plaid.errors["institution_details"]
    assert run(services, services.db.one("SELECT * FROM items"))["ok"] is True
    assert services.db.one("SELECT logo FROM items")["logo"] == TEST_LOGO


def test_an_unexpected_institution_reply_is_a_failed_lookup_not_a_crash():
    from app.plaid import PlaidClient

    class Odd(PlaidClient):
        def __init__(self, reply):
            self.reply = reply

        def _post(self, path, body):
            return self.reply

    for reply in ({"institution": None}, {"institution": "oops"}, {}):
        assert Odd(reply).institution_details("ins_1", ("CA",)) is None
    assert Odd({"institution": {"name": "TD", "logo": None}}).institution_details("ins_1", ("CA",)) == {
        "name": "TD", "logo": None, "color": None}
