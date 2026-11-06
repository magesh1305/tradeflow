"""Executor interface shared by PaperExecutor and LiveExecutor.

Executors never call the engine re-entrantly in the middle of a strategy callback:
they report through an `ExecutionListener`, and the engine queues those events
and processes them after the current dispatch finishes.

`submit` returns immediately. The live executor places the order in a background
task; the paper executor fills immediately when the order is marketable.
"""

from __future__ import annotations

from typing import Protocol

from app.core.models import Fill, Order, Tick


class ExecutionListener(Protocol):
    def on_order_update(self, order: Order) -> None: ...
    def on_fill(self, fill: Fill) -> None: ...
    def last_tick(self, token: int) -> Tick | None: ...


class Executor(Protocol):
    def bind(self, listener: ExecutionListener) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def adopt(self, order: Order) -> None:
        """Take over an order restored from the journal (crash recovery)."""

    def submit(self, order: Order) -> None: ...
    def cancel(self, client_order_id: str) -> None: ...
    def modify(
        self,
        client_order_id: str,
        *,
        price: float | None = None,
        trigger_price: float | None = None,
        quantity: int | None = None,
    ) -> None: ...
    def on_tick(self, tick: Tick) -> None: ...
