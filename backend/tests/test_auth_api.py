from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.brokers.session import KiteAuth, MemoryTokenStore
from app.config import get_settings
from app.core.clock import IST, ManualClock
from app.main import app


@pytest.fixture
def kite() -> MagicMock:
    k = MagicMock()
    k.login_url.return_value = "https://kite.zerodha.com/connect/login?api_key=k&v=3"
    k.generate_session.return_value = {"access_token": "acc", "user_id": "AB1234"}
    return k


@pytest.fixture
def client(kite: MagicMock, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LOG_JSON", "false")
    monkeypatch.setenv("FRONTEND_URL", "http://dash.local")
    get_settings.cache_clear()
    clock = ManualClock(datetime(2025, 11, 3, 8, 0, tzinfo=IST))
    app.state.auth = KiteAuth(
        kite, "secret", MemoryTokenStore(clock), clock=clock, expected_user_id="AB1234"
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.state.auth = None
    get_settings.cache_clear()


def test_status_before_login(client: TestClient) -> None:
    assert client.get("/auth/status").json() == {"configured": True, "logged_in": False}


def test_login_redirects_to_kite(client: TestClient) -> None:
    r = client.get("/auth/login")
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://kite.zerodha.com/connect/login")


def test_callback_success_then_status(client: TestClient, kite: MagicMock) -> None:
    r = client.get("/auth/callback", params={"request_token": "abc123", "status": "success"})
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    assert loc.netloc == "dash.local"
    assert parse_qs(loc.query)["kite_login"] == ["success"]
    body = client.get("/auth/status").json()
    assert body["logged_in"] is True
    assert body["user_id"] == "AB1234"
    assert body["expires_at"].startswith("2025-11-04T06:00:00")
    assert "access_token" not in body
    assert "acc" not in r.text


def test_callback_wrong_user_fails(client: TestClient, kite: MagicMock) -> None:
    kite.generate_session.return_value = {"access_token": "acc", "user_id": "EVIL01"}
    r = client.get("/auth/callback", params={"request_token": "abc123"})
    assert parse_qs(urlparse(r.headers["location"]).query)["kite_login"] == ["failed"]
    assert client.get("/auth/status").json()["logged_in"] is False


def test_callback_non_success_status(client: TestClient, kite: MagicMock) -> None:
    r = client.get("/auth/callback", params={"request_token": "abc123", "status": "cancelled"})
    assert parse_qs(urlparse(r.headers["location"]).query)["kite_login"] == ["failed"]
    kite.generate_session.assert_not_called()


def test_auth_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_JSON", "false")
    get_settings.cache_clear()
    app.state.auth = None
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/auth/login").status_code == 503
        assert c.get("/auth/status").json() == {"configured": False, "logged_in": False}
