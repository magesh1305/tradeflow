"""Shared fixtures. Tests never read the developer's `.env` and never touch Kite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

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


@pytest.fixture(autouse=True)
def _no_real_kite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guard: any real Kite HTTP request or WebSocket connect fails the test."""
    import kiteconnect  # noqa: PLC0415

    def _blocked(*_: Any, **__: Any) -> Any:
        raise AssertionError("tests must never call the real Kite API")

    monkeypatch.setattr(kiteconnect.KiteConnect, "_request", _blocked)
    monkeypatch.setattr(kiteconnect.KiteTicker, "connect", _blocked)
