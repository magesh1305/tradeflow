"""Alert interface. Telegram is added in Phase 4; LogNotifier is the default."""

from __future__ import annotations

import logging
from typing import Literal, Protocol

Level = Literal["info", "warning", "critical"]

log = logging.getLogger("tradeflow.alerts")

_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "critical": logging.CRITICAL}


class Notifier(Protocol):
    async def send(self, text: str, level: Level = "info") -> None: ...


class LogNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[Level, str]] = []

    async def send(self, text: str, level: Level = "info") -> None:
        self.sent.append((level, text))
        log.log(_LEVELS[level], text, extra={"alert": True})
