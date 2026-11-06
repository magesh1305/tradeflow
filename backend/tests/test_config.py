from __future__ import annotations

from datetime import time
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings, UnsafeConfigError

GOOD_TOKEN = "x" * 16


def make(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)


def live_ok(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "mode": "live",
        "live_trading_confirmed": True,
        "admin_token": GOOD_TOKEN,
        "feed": "kite",
        "kite_api_key": "k",
        "kite_api_secret": "s",
    }
    base.update(overrides)
    return make(**base)


def test_defaults_are_safe() -> None:
    s = make()
    assert s.mode == "paper"
    assert s.feed == "sim"
    assert s.live_trading_confirmed is False
    assert s.market_protection == -1
    assert not s.is_live
    s.assert_safe()  # paper always passes


def test_paper_ignores_missing_latches() -> None:
    make(mode="paper", feed="kite").assert_safe()


def test_live_with_all_latches_passes() -> None:
    s = live_ok()
    s.assert_safe()
    assert s.is_live
    assert s.live_latch_failures() == []


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"live_trading_confirmed": False}, "LIVE_TRADING_CONFIRMED"),
        ({"admin_token": ""}, "ADMIN_TOKEN"),
        ({"admin_token": "x" * 15}, "ADMIN_TOKEN"),
        ({"feed": "sim"}, "FEED"),
        ({"kite_api_key": ""}, "KITE_API_KEY"),
        ({"kite_api_secret": ""}, "KITE_API_KEY"),
    ],
)
def test_live_refuses_when_any_latch_missing(override: dict[str, Any], expected: str) -> None:
    with pytest.raises(UnsafeConfigError, match=expected):
        live_ok(**override).assert_safe()


def test_live_reports_all_failures_at_once() -> None:
    s = make(mode="live")
    problems = s.live_latch_failures()
    assert len(problems) == 4
    with pytest.raises(UnsafeConfigError):
        s.assert_safe()


def test_live_latches_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "false")
    with pytest.raises(UnsafeConfigError):
        make().assert_safe()


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        make(mode="LIVE!")


@pytest.mark.parametrize("value", [-1, 0.5, 5, 100])
def test_market_protection_valid(value: float) -> None:
    assert make(market_protection=value).market_protection == value


@pytest.mark.parametrize("value", [0, -2, 100.1, 250])
def test_market_protection_invalid(value: float) -> None:
    with pytest.raises(ValidationError):
        make(market_protection=value)


def test_order_rate_must_stay_below_kite_limit() -> None:
    with pytest.raises(ValidationError):
        make(order_rate_per_sec=10)


def test_trading_window_validation() -> None:
    with pytest.raises(ValidationError):
        make(trading_end=time(15, 20), square_off_time=time(15, 15))
    with pytest.raises(ValidationError):
        make(square_off_time=time(15, 45))


def test_times_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQUARE_OFF_TIME", "15:20")
    assert make().square_off_time == time(15, 20)


def test_secrets_not_in_repr() -> None:
    s = live_ok(admin_token="super-secret-token-123")
    assert "super-secret-token-123" not in repr(s)
