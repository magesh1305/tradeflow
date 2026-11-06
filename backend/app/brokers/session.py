"""Daily Kite login flow and access-token storage.

Kite flow (verified against https://kite.trade/docs/connect/v3/user/):
1. Browser goes to https://kite.zerodha.com/connect/login?v=3&api_key=...
2. Kite redirects to the app's registered redirect URL with `request_token` (and `status`).
3. The request token (valid only a few minutes) is exchanged for an access token,
   using checksum = SHA-256(api_key + request_token + api_secret); the SDK does this.
4. The access token expires at 06:00 IST the next day (or on logout / master logout).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time
from typing import Any, Protocol

from app.core.clock import Clock, SystemClock, next_occurrence, to_ist

log = logging.getLogger(__name__)

SESSION_KEY = "tradeflow:kite:session"


@dataclass(frozen=True, slots=True)
class KiteSession:
    access_token: str
    user_id: str
    login_time: datetime
    expires_at: datetime

    def to_json(self) -> str:
        d = asdict(self)
        d["login_time"] = self.login_time.isoformat()
        d["expires_at"] = self.expires_at.isoformat()
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> KiteSession:
        d = json.loads(raw)
        return cls(
            access_token=d["access_token"],
            user_id=d["user_id"],
            login_time=datetime.fromisoformat(d["login_time"]),
            expires_at=datetime.fromisoformat(d["expires_at"]),
        )


class TokenStore(Protocol):
    async def save(self, session: KiteSession, ttl_seconds: int) -> None: ...
    async def load(self) -> KiteSession | None: ...
    async def clear(self) -> None: ...


class MemoryTokenStore:
    """In-process store (tests, and the sim feed without Redis)."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._session: KiteSession | None = None
        self._clock = clock or SystemClock()

    async def save(self, session: KiteSession, ttl_seconds: int) -> None:
        self._session = session

    async def load(self) -> KiteSession | None:
        if self._session and self._session.expires_at <= self._clock.now():
            self._session = None
        return self._session

    async def clear(self) -> None:
        self._session = None


class RedisTokenStore:
    """Stores the session in Redis with a TTL ending at the next 06:00 IST."""

    def __init__(self, redis: Any, key: str = SESSION_KEY) -> None:
        self._redis = redis  # redis.asyncio.Redis
        self._key = key

    async def save(self, session: KiteSession, ttl_seconds: int) -> None:
        await self._redis.set(self._key, session.to_json(), ex=max(ttl_seconds, 1))

    async def load(self) -> KiteSession | None:
        raw = await self._redis.get(self._key)
        if raw is None:
            return None
        return KiteSession.from_json(raw.decode() if isinstance(raw, bytes) else raw)

    async def clear(self) -> None:
        await self._redis.delete(self._key)


class LoginError(RuntimeError):
    pass


class KiteAuth:
    """Daily login: builds the login URL and exchanges request tokens.

    `kite` is a `kiteconnect.KiteConnect` (mocked in tests). The SDK call is sync,
    so it runs in a worker thread.
    """

    def __init__(
        self,
        kite: Any,
        api_secret: str,
        store: TokenStore,
        *,
        expected_user_id: str = "",
        expiry_time: time = time(6, 0),
        clock: Clock | None = None,
    ) -> None:
        self._kite = kite
        self._api_secret = api_secret
        self._store = store
        self._expected_user_id = expected_user_id
        self._expiry_time = expiry_time
        self._clock = clock or SystemClock()

    def login_url(self) -> str:
        return str(self._kite.login_url())

    async def complete_login(self, request_token: str) -> KiteSession:
        if not request_token or not request_token.isalnum():
            raise LoginError("missing or malformed request_token")
        try:
            data = await asyncio.to_thread(
                self._kite.generate_session, request_token, api_secret=self._api_secret
            )
        except Exception as exc:  # SDK raises several exception types
            raise LoginError(f"token exchange failed: {exc}") from exc

        user_id = str(data.get("user_id", ""))
        if self._expected_user_id and user_id != self._expected_user_id:
            # Guards against someone completing the login with a different Kite account.
            raise LoginError(f"Kite user {user_id!r} does not match KITE_USER_ID")

        now = self._clock.now()
        login_time = data.get("login_time")
        session = KiteSession(
            access_token=str(data["access_token"]),
            user_id=user_id,
            login_time=to_ist(login_time) if isinstance(login_time, datetime) else now,
            expires_at=next_occurrence(now, self._expiry_time),
        )
        ttl = int((session.expires_at - now).total_seconds())
        await self._store.save(session, ttl)
        log.info(
            "kite login complete", extra={"user_id": user_id, "expires_at": session.expires_at}
        )
        return session

    async def current_session(self) -> KiteSession | None:
        session = await self._store.load()
        if session and session.expires_at <= self._clock.now():
            await self._store.clear()
            return None
        return session

    async def logout(self) -> None:
        await self._store.clear()
