"""The release number: a file in the image, shown in Settings, served to signed-in users only."""

from __future__ import annotations

import re
from pathlib import Path

from app import version


def test_version_file_is_a_plain_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.VERSION)


def test_endpoint_reports_version_and_build(seeded, monkeypatch):
    client, auth = seeded
    monkeypatch.setenv("HF_BUILD", "0123456789abcdef")
    body = client.get("/api/version", headers=auth).json()
    assert body == {"version": version.VERSION, "build": "0123456"}
    monkeypatch.delenv("HF_BUILD")
    assert client.get("/api/version", headers=auth).json()["build"] is None


def test_endpoint_needs_a_login(client):
    assert client.get("/api/version").status_code == 401


def test_settings_page_shows_it():
    script = (Path(version.__file__).parent / "static/web/app.js").read_text()
    assert "/api/version" in script and "HomeFinance version" in script
