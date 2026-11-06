"""Shared fixtures. Tests never read the developer's `.env` and never touch Kite."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Strip every setting that could come from the developer's shell so tests are hermetic.
_SETTING_PREFIXES = (
    "MODE",
    "FEED",
    "LIVE_TRADING_CONFIRMED",
    "ADMIN_TOKEN",
    "KITE_",
    "TELEGRAM_",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.upper().startswith(_SETTING_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    yield
