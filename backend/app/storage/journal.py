"""Order/fill journal in Postgres.

The engine calls `record_order` / `record_fill` synchronously. Those calls only
take a snapshot and put it on an in-memory queue, so they can never block or
fail order handling. A background writer persists the queue in batches. If the
database fails, the writer logs, alerts (throttled) and retries with backoff,
keeping records in order (an order row is always written before its fills).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import now_ist
from app.core.models import Fill, Order, Product
from app.notify.base import Notifier
from app.storage.models import DailyPnlRow, FillRow, LegGroupRow, OrderEventRow, OrderRow

log = logging.getLogger(__name__)

RecordKind = Literal["order", "fill", "leg_group"]


@dataclass(frozen=True, slots=True)
class Record:
    kind: RecordKind
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReportFilter:
    """Read-side filter for the control API (dates are trading dates, inclusive)."""

    start: date
    end: date
    mode: str
    strategy_id: str | None = None
    limit: int = 1000


def _match(row: dict[str, Any], f: ReportFilter) -> bool:
    return (
        f.start <= row["trading_date"] <= f.end
        and row["mode"] == f.mode
        and (f.strategy_id is None or row["strategy_id"] == f.strategy_id)
    )


def order_row(order: Order, mode: str) -> dict[str, Any]:
    req = order.request
    inst = req.instrument
    return {
        "client_order_id": order.client_order_id,
        "broker_order_id": order.broker_order_id,
        "trading_date": order.created_at.date(),
        "mode": mode,
        "strategy_id": req.strategy_id,
        "leg_group_id": req.leg_group_id,
        "exchange": str(inst.exchange),
        "tradingsymbol": inst.tradingsymbol,
        "instrument_token": inst.token,
        "side": str(req.side),
        "quantity": req.quantity,
        "order_type": str(req.order_type),
        "product": str(req.product),
        "price": req.price,
        "trigger_price": req.trigger_price,
        "status": str(order.status),
        "filled_quantity": order.filled_quantity,
        "average_price": order.average_price,
        "status_message": order.status_message,
        "reason": req.reason,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }


def fill_row(fill: Fill, mode: str, charges: float = 0.0) -> dict[str, Any]:
    inst = fill.instrument
    return {
        "fill_id": fill.fill_id,
        "client_order_id": fill.client_order_id,
        "broker_order_id": fill.broker_order_id,
        "trading_date": fill.ts.date(),
        "mode": mode,
        "strategy_id": fill.strategy_id,
        "exchange": str(inst.exchange),
        "tradingsymbol": inst.tradingsymbol,
        "instrument_token": inst.token,
        "side": str(fill.side),
        "quantity": fill.quantity,
        "price": fill.price,
        "product": str(fill.product),
        "charges": charges,
        "ts": fill.ts,
    }


class JournalStore(Protocol):
    async def write(self, batch: list[Record]) -> None: ...
    async def load_fills(self, mode: str, trading_date: date) -> list[dict[str, Any]]: ...
    async def load_carry_fills(self, mode: str, before: date) -> list[dict[str, Any]]: ...
    async def load_open_orders(self, mode: str, trading_date: date) -> list[dict[str, Any]]: ...
    async def load_order_ids(self, mode: str, trading_date: date) -> set[str]: ...
    async def upsert_daily_pnl(self, rows: list[dict[str, Any]]) -> None: ...
    async def query_orders(self, f: ReportFilter) -> list[dict[str, Any]]: ...
    async def query_fills(self, f: ReportFilter) -> list[dict[str, Any]]: ...
    async def query_daily_pnl(self, f: ReportFilter) -> list[dict[str, Any]]: ...


_TERMINAL = ("COMPLETE", "CANCELLED", "REJECTED")


class SqlJournalStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def write(self, batch: list[Record]) -> None:
        async with self._sessions.begin() as s:
            for rec in batch:
                if rec.kind == "order":
                    d = rec.data
                    stmt = insert(OrderRow).values(**d)
                    updatable = {k: stmt.excluded[k] for k in d if k != "client_order_id"}
                    await s.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["client_order_id"], set_=updatable
                        )
                    )
                    await s.execute(
                        insert(OrderEventRow).values(
                            client_order_id=d["client_order_id"],
                            ts=d["updated_at"],
                            status=d["status"],
                            filled_quantity=d["filled_quantity"],
                            average_price=d["average_price"],
                            message=d["status_message"],
                            payload=None,
                        )
                    )
                elif rec.kind == "leg_group":
                    stmt = insert(LegGroupRow).values(**rec.data)
                    await s.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["id"],
                            set_={k: stmt.excluded[k] for k in rec.data if k != "id"},
                        )
                    )
                else:
                    await s.execute(
                        insert(FillRow)
                        .values(**rec.data)
                        .on_conflict_do_nothing(index_elements=["fill_id"])
                    )

    async def _rows(self, stmt: Any) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            result = await s.execute(stmt)
            return [dict(r._mapping) for r in result]

    async def load_fills(self, mode: str, trading_date: date) -> list[dict[str, Any]]:
        t = FillRow.__table__
        return await self._rows(
            select(t).where(t.c.mode == mode, t.c.trading_date == trading_date).order_by(t.c.ts)
        )

    async def load_carry_fills(self, mode: str, before: date) -> list[dict[str, Any]]:
        t = FillRow.__table__
        return await self._rows(
            select(t)
            .where(t.c.mode == mode, t.c.trading_date < before, t.c.product == Product.NRML.value)
            .order_by(t.c.ts)
        )

    async def load_open_orders(self, mode: str, trading_date: date) -> list[dict[str, Any]]:
        t = OrderRow.__table__
        return await self._rows(
            select(t).where(
                t.c.mode == mode,
                t.c.trading_date == trading_date,
                t.c.status.not_in(_TERMINAL),
            )
        )

    async def load_order_ids(self, mode: str, trading_date: date) -> set[str]:
        t = OrderRow.__table__
        rows = await self._rows(
            select(t.c.client_order_id).where(t.c.mode == mode, t.c.trading_date == trading_date)
        )
        return {r["client_order_id"] for r in rows}

    async def upsert_daily_pnl(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        async with self._sessions.begin() as s:
            for row in rows:
                stmt = insert(DailyPnlRow).values(**row)
                keys = {"trading_date", "mode", "strategy_id"}
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=list(keys),
                        set_={k: stmt.excluded[k] for k in row if k not in keys},
                    )
                )

    async def _query(self, table: Any, f: ReportFilter, order_col: str) -> list[dict[str, Any]]:
        t = table.__table__
        stmt = select(t).where(
            t.c.trading_date >= f.start, t.c.trading_date <= f.end, t.c.mode == f.mode
        )
        if f.strategy_id is not None:
            stmt = stmt.where(t.c.strategy_id == f.strategy_id)
        return await self._rows(stmt.order_by(t.c[order_col].desc()).limit(f.limit))

    async def query_orders(self, f: ReportFilter) -> list[dict[str, Any]]:
        return await self._query(OrderRow, f, "created_at")

    async def query_fills(self, f: ReportFilter) -> list[dict[str, Any]]:
        return await self._query(FillRow, f, "ts")

    async def query_daily_pnl(self, f: ReportFilter) -> list[dict[str, Any]]:
        return await self._query(DailyPnlRow, f, "trading_date")


class Journal:
    def __init__(
        self,
        store: JournalStore,
        mode: str,
        notifier: Notifier,
        *,
        retry_initial: float = 1.0,
        retry_max: float = 30.0,
        alert_every: float = 60.0,
        batch_size: int = 200,
    ) -> None:
        self.store = store
        self.mode = mode
        self._notifier = notifier
        self._queue: asyncio.Queue[Record] = asyncio.Queue()
        self._retry_initial = retry_initial
        self._retry_max = retry_max
        self._alert_every = alert_every
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._last_alert = -1e18
        self.failures = 0
        self.last_error = ""
        self.written = 0

    # --- called synchronously by the engine (never blocks, never raises) -------------

    def record_order(self, order: Order) -> None:
        try:
            self._queue.put_nowait(Record("order", order_row(order, self.mode)))
        except Exception:
            log.exception("journal enqueue failed")

    def record_fill(self, fill: Fill, charges: float = 0.0) -> None:
        try:
            self._queue.put_nowait(Record("fill", fill_row(fill, self.mode, charges)))
        except Exception:
            log.exception("journal enqueue failed")

    def record_leg_group(self, group: Any) -> None:
        try:
            self._queue.put_nowait(Record("leg_group", group.as_row(self.mode, now_ist())))
        except Exception:
            log.exception("journal enqueue failed")

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    # --- writer --------------------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="journal-writer")

    async def flush(self, timeout: float = 10.0) -> bool:  # noqa: ASYNC109
        try:
            async with asyncio.timeout(timeout):
                await self._queue.join()
            return True
        except TimeoutError:
            log.error("journal flush timed out", extra={"backlog": self.backlog})
            return False

    async def stop(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109
        await self.flush(timeout)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self._batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            await self._write_with_retry(batch)
            for _ in batch:
                self._queue.task_done()

    async def _write_with_retry(self, batch: list[Record]) -> None:
        delay = self._retry_initial
        while True:
            try:
                await self.store.write(batch)
                self.written += len(batch)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("journal write failed; retrying", extra={"batch": len(batch)})
                now = time.monotonic()
                if now - self._last_alert >= self._alert_every:
                    self._last_alert = now
                    with contextlib.suppress(Exception):
                        await self._notifier.send(
                            f"Journal DB write failing ({self.last_error}). Trading continues; "
                            f"{self.backlog + len(batch)} records buffered in memory.",
                            "critical",
                        )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max)


class MemoryJournalStore:
    """In-memory JournalStore for tests and for running without Postgres."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.fills: dict[str, dict[str, Any]] = {}
        self.daily_pnl: dict[tuple[date, str, str], dict[str, Any]] = {}
        self.leg_groups: dict[str, dict[str, Any]] = {}
        self.fail_writes = 0  # make the next N writes raise

    async def write(self, batch: list[Record]) -> None:
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise ConnectionError("database unavailable")
        for rec in batch:
            if rec.kind == "order":
                self.orders[rec.data["client_order_id"]] = dict(rec.data)
                self.events.append(dict(rec.data))
            elif rec.kind == "leg_group":
                self.leg_groups[rec.data["id"]] = dict(rec.data)
            else:
                if rec.data["client_order_id"] not in self.orders:
                    raise RuntimeError("FK violation: fill before order")
                self.fills.setdefault(rec.data["fill_id"], dict(rec.data))

    async def load_fills(self, mode: str, trading_date: date) -> list[dict[str, Any]]:
        rows = [
            f
            for f in self.fills.values()
            if f["mode"] == mode and f["trading_date"] == trading_date
        ]
        return sorted(rows, key=lambda f: f["ts"])

    async def load_carry_fills(self, mode: str, before: date) -> list[dict[str, Any]]:
        rows = [
            f
            for f in self.fills.values()
            if f["mode"] == mode and f["trading_date"] < before and f["product"] == "NRML"
        ]
        return sorted(rows, key=lambda f: f["ts"])

    async def load_open_orders(self, mode: str, trading_date: date) -> list[dict[str, Any]]:
        return [
            o
            for o in self.orders.values()
            if o["mode"] == mode
            and o["trading_date"] == trading_date
            and o["status"] not in _TERMINAL
        ]

    async def load_order_ids(self, mode: str, trading_date: date) -> set[str]:
        return {
            o["client_order_id"]
            for o in self.orders.values()
            if o["mode"] == mode and o["trading_date"] == trading_date
        }

    async def upsert_daily_pnl(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.daily_pnl[(r["trading_date"], r["mode"], r["strategy_id"])] = dict(r)

    async def query_orders(self, f: ReportFilter) -> list[dict[str, Any]]:
        rows = [o for o in self.orders.values() if _match(o, f)]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[: f.limit]

    async def query_fills(self, f: ReportFilter) -> list[dict[str, Any]]:
        rows = [r for r in self.fills.values() if _match(r, f)]
        return sorted(rows, key=lambda r: r["ts"], reverse=True)[: f.limit]

    async def query_daily_pnl(self, f: ReportFilter) -> list[dict[str, Any]]:
        rows = [r for r in self.daily_pnl.values() if _match(r, f)]
        return sorted(rows, key=lambda r: r["trading_date"], reverse=True)[: f.limit]


def daily_pnl_rows(
    trading_date: date, mode: str, summary: dict[str, dict[str, float]], now: datetime
) -> list[dict[str, Any]]:
    return [
        {
            "trading_date": trading_date,
            "mode": mode,
            "strategy_id": sid,
            "gross_pnl": v["gross"],
            "charges": v["charges"],
            "net_pnl": v["net"],
            "trades": int(v["trades"]),
            "created_at": now,
        }
        for sid, v in summary.items()
    ]
