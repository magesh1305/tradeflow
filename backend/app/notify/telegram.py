"""Telegram alerts via the Bot API (sendMessage). Failures are logged, never raised:
alerting must never break trading."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import httpx

from app.notify.base import Level, Notifier

log = logging.getLogger(__name__)

MAX_LEN = 4096  # Telegram message limit
_PREFIX = {"info": "", "warning": "⚠️ ", "critical": "🚨 "}


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        min_interval: float = 1.0,  # Telegram: about 1 message/sec per chat
        min_level: Level = "info",
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=10)
        self._min_interval = min_interval
        self._order = {"info": 0, "warning": 1, "critical": 2}
        self._min_level = self._order[min_level]
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.failures = 0

    async def send(self, text: str, level: Level = "info") -> None:
        if self._order[level] < self._min_level:
            return
        body = (_PREFIX[level] + text)[:MAX_LEN]
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._client.post(
                    self._url, json={"chat_id": self._chat_id, "text": body}
                )
                if resp.status_code != 200:
                    self.failures += 1
                    # Never log the URL: it contains the bot token.
                    log.warning("telegram send failed", extra={"status": resp.status_code})
            except httpx.HTTPError as exc:
                self.failures += 1
                log.warning("telegram send failed", extra={"error": type(exc).__name__})
            finally:
                self._last = time.monotonic()

    async def aclose(self) -> None:
        await self._client.aclose()


class CompositeNotifier:
    """Fan-out with an optional prefix (e.g. "[LIVE]"). One failing sink never blocks others."""

    def __init__(self, *sinks: Notifier, prefix: str = "") -> None:
        self.sinks = sinks
        self._prefix = prefix

    async def send(self, text: str, level: Level = "info") -> None:
        msg = f"{self._prefix}{text}"
        for sink in self.sinks:
            with contextlib.suppress(Exception):
                await sink.send(msg, level)
