"""Kite WebSocket feed (KiteTicker) bridged into asyncio.

Verified against https://kite.trade/docs/connect/v3/websocket/ and the
kiteconnect 5.2.2 source:
- up to 3000 instruments per connection, up to 3 connections per API key;
- `full` mode has LTP, volume, OI, exchange timestamp and 5-level depth;
- order updates arrive as text frames with type "order" (SDK: `on_order_update`);
- KiteTicker runs on the Twisted reactor in its own thread (`connect(threaded=True)`),
  and auto-reconnects with exponential backoff, re-subscribing on reconnect.

Threading rules:
- every KiteTicker callback runs on the reactor thread and is handed to the
  asyncio loop with `loop.call_soon_threadsafe`;
- subscribe/unsubscribe calls go *into* the reactor thread via
  `reactor.callFromThread` (Twisted is not thread-safe).

The Twisted reactor cannot be restarted inside one process, so a KiteFeed can
be started only once per process. After the daily token expiry the engine stops
and the process is restarted (Docker `restart: unless-stopped`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from app.core.clock import now_ist, to_ist
from app.core.models import Tick
from app.feeds.base import FeedSink

log = logging.getLogger(__name__)

MAX_TOKENS_PER_CONNECTION = 3000
MODE_FULL = "full"

_started_once = False  # the Twisted reactor is not restartable


def tick_from_kite(raw: dict[str, Any]) -> Tick:
    """Convert a KiteTicker tick dict to a Tick. Timestamps are naive IST in the SDK."""
    ts_raw = raw.get("exchange_timestamp") or raw.get("last_trade_time")
    ts = to_ist(ts_raw) if isinstance(ts_raw, datetime) else now_ist()
    bid = ask = None
    bid_qty = ask_qty = 0
    depth = raw.get("depth") or {}
    buys, sells = depth.get("buy") or [], depth.get("sell") or []
    if buys and buys[0].get("price", 0) > 0:
        bid, bid_qty = float(buys[0]["price"]), int(buys[0].get("quantity", 0))
    if sells and sells[0].get("price", 0) > 0:
        ask, ask_qty = float(sells[0]["price"]), int(sells[0].get("quantity", 0))
    return Tick(
        token=int(raw["instrument_token"]),
        ts=ts,
        ltp=float(raw.get("last_price", 0.0)),
        volume=int(raw.get("volume_traded", 0) or 0),
        oi=int(raw.get("oi", 0) or 0),
        bid=bid,
        ask=ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
    )


def _default_ticker_factory(api_key: str, access_token: str) -> Any:
    from kiteconnect import KiteTicker  # noqa: PLC0415 - imports Twisted; keep lazy

    return KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=300)


def _default_call_in_reactor(fn: Callable[..., Any], *args: Any) -> None:
    from twisted.internet import reactor  # noqa: PLC0415

    reactor.callFromThread(fn, *args)


class KiteFeed:
    def __init__(
        self,
        api_key: str,
        access_token: str,
        *,
        ticker_factory: Callable[[str, str], Any] = _default_ticker_factory,
        call_in_reactor: Callable[..., None] = _default_call_in_reactor,
        enforce_single_start: bool = True,
    ) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._ticker_factory = ticker_factory
        self._call_in_reactor = call_in_reactor
        self._enforce_single_start = enforce_single_start
        self._ticker: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sink: FeedSink | None = None
        self._tokens: set[int] = set()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def subscribed(self) -> frozenset[int]:
        return frozenset(self._tokens)

    async def start(self, sink: FeedSink) -> None:
        global _started_once  # noqa: PLW0603
        if self._enforce_single_start:
            if _started_once:
                raise RuntimeError("KiteFeed can only start once per process (Twisted reactor)")
            _started_once = True
        self._loop = asyncio.get_running_loop()
        self._sink = sink
        t = self._ticker_factory(self._api_key, self._access_token)
        t.on_ticks = self._on_ticks
        t.on_connect = self._on_connect
        t.on_close = self._on_close
        t.on_error = self._on_error
        t.on_reconnect = self._on_reconnect
        t.on_noreconnect = self._on_noreconnect
        t.on_order_update = self._on_order_update
        self._ticker = t
        t.connect(threaded=True)
        log.info("kite feed connecting")

    async def stop(self) -> None:
        if self._ticker is not None:
            t = self._ticker
            self._call_in_reactor(t.stop_retry)
            self._call_in_reactor(t.close)
        self._connected = False

    # --- subscriptions (called on the asyncio thread) ---------------------------

    def subscribe(self, tokens: Iterable[int]) -> None:
        new = [t for t in tokens if t not in self._tokens]
        if not new:
            return
        if len(self._tokens) + len(new) > MAX_TOKENS_PER_CONNECTION:
            raise ValueError(
                f"Kite allows at most {MAX_TOKENS_PER_CONNECTION} instruments per connection"
            )
        self._tokens.update(new)
        if self._connected and self._ticker is not None:
            self._call_in_reactor(self._send_subscribe, new)

    def unsubscribe(self, tokens: Iterable[int]) -> None:
        gone = [t for t in tokens if t in self._tokens]
        if not gone:
            return
        self._tokens.difference_update(gone)
        if self._connected and self._ticker is not None:
            self._call_in_reactor(self._ticker.unsubscribe, gone)

    def _send_subscribe(self, tokens: list[int]) -> None:
        # Runs on the reactor thread.
        self._ticker.subscribe(tokens)
        self._ticker.set_mode(MODE_FULL, tokens)

    # --- KiteTicker callbacks (reactor thread) -----------------------------------

    def _dispatch(self, fn: Callable[..., None], *args: Any) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn, *args)

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        converted = []
        for raw in ticks:
            try:
                converted.append(tick_from_kite(raw))
            except (KeyError, TypeError, ValueError):
                log.warning("bad tick dropped", extra={"tick": raw}, exc_info=True)
        if converted and self._sink is not None:
            self._dispatch(self._sink.on_ticks, converted)

    def _on_connect(self, ws: Any, response: Any) -> None:
        self._connected = True
        tokens = list(self._tokens)
        if tokens:
            # Already on the reactor thread here.
            self._send_subscribe(tokens)
        log.info("kite feed connected", extra={"tokens": len(tokens)})
        self._status(True, "connected")

    def _on_close(self, ws: Any, code: Any, reason: Any) -> None:
        self._connected = False
        log.warning("kite feed closed", extra={"code": code, "reason": str(reason)})
        self._status(False, f"closed: {code} {reason}")

    def _on_error(self, ws: Any, code: Any, reason: Any) -> None:
        log.error("kite feed error", extra={"code": code, "reason": str(reason)})

    def _on_reconnect(self, ws: Any, attempts: int) -> None:
        log.warning("kite feed reconnecting", extra={"attempt": attempts})
        self._status(False, f"reconnecting (attempt {attempts})")

    def _on_noreconnect(self, ws: Any) -> None:
        self._connected = False
        log.error("kite feed gave up reconnecting")
        self._status(False, "reconnect attempts exhausted")

    def _on_order_update(self, ws: Any, data: dict[str, Any]) -> None:
        if self._sink is not None:
            self._dispatch(self._sink.on_order_update, data)

    def _status(self, connected: bool, detail: str) -> None:
        if self._sink is not None:
            self._dispatch(self._sink.on_feed_status, connected, detail)
