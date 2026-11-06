from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import UnsafeConfigError, get_settings
from app.main import app


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LOG_JSON", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_health_in_paper_mode() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "paper"


def test_app_refuses_to_start_live_without_latches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "live")
    with pytest.raises(UnsafeConfigError), TestClient(app):
        pass
