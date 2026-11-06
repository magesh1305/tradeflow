from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from kiteconnect import KiteTicker

from app import factory
from app.brokers.session import KiteSession, MemoryTokenStore
from app.config import Settings
from app.core.clock import IST
from app.feeds.kite import KiteFeed, _default_ticker_factory
from app.feeds.random_walk import RandomWalkFeed

NOW = datetime(2025, 11, 3, 8, 0, tzinfo=IST)
SESSION = KiteSession("acc", "AB1234", NOW, NOW + timedelta(hours=22))


def settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {"kite_api_key": "key", "kite_api_secret": "secret"}
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_paper_broker_has_orders_disabled() -> None:
    broker = factory.build_broker(settings(mode="paper"), SESSION)
    assert broker.orders_enabled is False


def test_live_broker_has_orders_enabled() -> None:
    s = settings(mode="live", live_trading_confirmed=True, admin_token="x" * 16, feed="kite")
    assert factory.build_broker(s, SESSION).orders_enabled is True


def test_feed_selection() -> None:
    assert isinstance(
        factory.build_feed(settings(feed="sim"), None, date(2025, 11, 3)), RandomWalkFeed
    )
    assert isinstance(
        factory.build_feed(settings(feed="kite"), SESSION, date(2025, 11, 3)), KiteFeed
    )
    with pytest.raises(RuntimeError, match="log in"):
        factory.build_feed(settings(feed="kite"), None, date(2025, 11, 3))


def test_auth_login_url_uses_api_key() -> None:
    auth = factory.build_auth(settings(), MemoryTokenStore())
    url = auth.login_url()
    assert url.startswith("https://kite.zerodha.com/connect/login")
    assert "api_key=key" in url


def test_real_kiteticker_has_every_callback_we_set() -> None:
    ticker = _default_ticker_factory("key", "token")  # constructing does not connect
    assert isinstance(ticker, KiteTicker)
    for attr in (
        "on_ticks",
        "on_connect",
        "on_close",
        "on_error",
        "on_reconnect",
        "on_noreconnect",
        "on_order_update",
    ):
        assert hasattr(ticker, attr), attr
    for method in ("subscribe", "set_mode", "unsubscribe", "stop_retry", "close"):
        assert callable(getattr(ticker, method)), method
