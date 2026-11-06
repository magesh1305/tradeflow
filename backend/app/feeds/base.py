"""Feed interface. A feed pushes ticks (and, for Kite, order updates) into a sink.

Sink methods are always called on the asyncio event-loop thread, never on a
feed's internal thread.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from app.core.models import Tick


class FeedSink(Protocol):
    def on_ticks(self, ticks: list[Tick]) -> None: ...
    def on_order_update(self, payload: dict[str, Any]) -> None: ...
    def on_feed_status(self, connected: bool, detail: str) -> None: ...


class Feed(Protocol):
    async def start(self, sink: FeedSink) -> None: ...
    async def stop(self) -> None: ...
    def subscribe(self, tokens: Iterable[int]) -> None: ...
    def unsubscribe(self, tokens: Iterable[int]) -> None: ...
    @property
    def connected(self) -> bool: ...
