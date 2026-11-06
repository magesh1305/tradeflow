from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.brokers.session import (
    KiteAuth,
    KiteSession,
    LoginError,
    MemoryTokenStore,
    RedisTokenStore,
)
from app.core.clock import IST, ManualClock

T0 = datetime(2025, 11, 3, 8, 30, tzinfo=IST)


def make_auth(
    clock: ManualClock, *, user: str = "AB1234", expected: str = ""
) -> tuple[KiteAuth, MagicMock, MemoryTokenStore]:
    kite = MagicMock()
    kite.login_url.return_value = "https://kite.zerodha.com/connect/login?api_key=k&v=3"
    kite.generate_session.return_value = {
        "access_token": "acc123",
        "user_id": user,
        "login_time": datetime(2025, 11, 3, 8, 29, 50),  # SDK gives naive IST
    }
    store = MemoryTokenStore(clock)
    return KiteAuth(kite, "secret", store, expected_user_id=expected, clock=clock), kite, store


async def test_login_stores_session_until_next_6am() -> None:
    clock = ManualClock(T0)
    auth, kite, _ = make_auth(clock)
    session = await auth.complete_login("reqtok123")
    kite.generate_session.assert_called_once_with("reqtok123", api_secret="secret")
    assert session.access_token == "acc123"
    assert session.expires_at == datetime(2025, 11, 4, 6, 0, tzinfo=IST)
    assert session.login_time.tzinfo is not None
    assert await auth.current_session() == session


async def test_session_expires_at_6am() -> None:
    clock = ManualClock(T0)
    auth, _, _ = make_auth(clock)
    await auth.complete_login("reqtok123")
    clock.set(datetime(2025, 11, 4, 5, 59, tzinfo=IST))
    assert await auth.current_session() is not None
    clock.set(datetime(2025, 11, 4, 6, 0, tzinfo=IST))
    assert await auth.current_session() is None


async def test_login_after_midnight_expires_same_morning_if_before_6() -> None:
    clock = ManualClock(datetime(2025, 11, 4, 2, 0, tzinfo=IST))
    auth, _, _ = make_auth(clock)
    s = await auth.complete_login("reqtok123")
    assert s.expires_at == datetime(2025, 11, 4, 6, 0, tzinfo=IST)


async def test_wrong_kite_user_rejected() -> None:
    auth, _, store = make_auth(ManualClock(T0), user="ZZ9999", expected="AB1234")
    with pytest.raises(LoginError, match="does not match"):
        await auth.complete_login("reqtok123")
    assert await store.load() is None


@pytest.mark.parametrize("token", ["", "bad token", "x;drop"])
async def test_malformed_request_token(token: str) -> None:
    auth, kite, _ = make_auth(ManualClock(T0))
    with pytest.raises(LoginError):
        await auth.complete_login(token)
    kite.generate_session.assert_not_called()


async def test_sdk_failure_becomes_login_error() -> None:
    auth, kite, _ = make_auth(ManualClock(T0))
    kite.generate_session.side_effect = RuntimeError("Token is invalid or has expired")
    with pytest.raises(LoginError, match="exchange failed"):
        await auth.complete_login("reqtok123")


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int) -> None:
        self.data[key] = value.encode()
        self.ttl[key] = ex

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


async def test_redis_store_roundtrip_with_ttl() -> None:
    redis = FakeRedis()
    store = RedisTokenStore(redis)
    clock = ManualClock(T0)
    auth = KiteAuth(make_auth(clock)[1], "secret", store, clock=clock)
    session = await auth.complete_login("reqtok123")
    assert await store.load() == session
    (ttl,) = redis.ttl.values()
    assert ttl == int((session.expires_at - T0).total_seconds())
    await auth.logout()
    assert await store.load() is None


def test_session_json_roundtrip() -> None:
    s = KiteSession("a", "U1", T0, T0 + timedelta(hours=20))
    assert KiteSession.from_json(s.to_json()) == s
