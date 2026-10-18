"""Which release this is: the number in app/VERSION (bumped in every pull request) and, in the
Docker image, the short commit it was built from."""

from __future__ import annotations

import os
from pathlib import Path


def _read() -> str:
    try:
        return Path(__file__).with_name("VERSION").read_text().strip() or "unknown"
    except OSError:  # a cosmetic field must never stop the server starting
        return "unknown"


VERSION = _read()


def build() -> str | None:
    """Short commit id baked into the image by the build (HF_BUILD); None when run from source."""
    value = os.environ.get("HF_BUILD", "").strip()
    return value[:7] or None


def info() -> dict[str, str | None]:
    return {"version": VERSION, "build": build()}
