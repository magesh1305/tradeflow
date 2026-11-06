"""Crash recovery: rebuild today's state from the journal and, in live mode,
check it against Kite before any strategy may trade.

1. Carried NRML positions: NRML fills from earlier days for contracts that are
   still live are replayed. Their realized P&L and charges are then zeroed, so
   today's P&L only counts today.
2. Today's fills are replayed into the PositionBook.
3. Today's non-terminal orders are restored and adopted by the executor. Live
   reconciles them with Kite; paper cancels them.
4. Live only: positions (NFO, MIS/NRML, summed across strategies) and orders
   carrying our 16-char tags are compared with `kite.positions()` / `kite.orders()`.
   Any disagreement blocks the engine (no strategy can start) and alerts loudly,
   until an operator resolves it through the API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.brokers.kite import KiteBroker
from app.core.clock import to_ist
from app.core.models import (
    CLIENT_ORDER_ID_LEN,
    Exchange,
    Fill,
    Instrument,
    InstrumentType,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Product,
    Side,
    new_client_order_id,
)
from app.data.instruments import InstrumentStore
from app.engine.engine import TradingEngine
from app.storage.journal import JournalStore

log = logging.getLogger(__name__)

RECOVERED = "__recovered__"
TRADED_PRODUCTS = frozenset({Product.MIS.value, Product.NRML.value})


@dataclass
class PositionDiff:
    token: int
    exchange: str
    tradingsymbol: str
    product: str
    ours: int
    broker: int
    broker_avg: float = 0.0

    def describe(self) -> str:
        return (
            f"{self.exchange}:{self.tradingsymbol} {self.product}: "
            f"ours={self.ours} kite={self.broker}"
        )


@dataclass
class RecoveryReport:
    fills_replayed: int = 0
    carried_positions: int = 0
    orders_restored: int = 0
    checked_with_broker: bool = False
    position_diffs: list[PositionDiff] = field(default_factory=list)
    order_issues: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def mismatches(self) -> list[str]:
        out = [d.describe() for d in self.position_diffs] + self.order_issues
        if self.error:
            out.append(self.error)
        return out

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "fills_replayed": self.fills_replayed,
            "carried_positions": self.carried_positions,
            "orders_restored": self.orders_restored,
            "checked_with_broker": self.checked_with_broker,
            "mismatches": self.mismatches,
        }


def _instrument(row: dict[str, Any], store: InstrumentStore) -> Instrument:
    found = store.find(f"{row['exchange']}:{row['tradingsymbol']}")
    if found is not None:
        return found
    try:
        return store.by_token(int(row["instrument_token"]))
    except KeyError:
        log.warning(
            "journal instrument not in today's master", extra={"symbol": row["tradingsymbol"]}
        )
        return Instrument(
            token=int(row["instrument_token"]),
            tradingsymbol=row["tradingsymbol"],
            exchange=Exchange(row["exchange"]),
            name=row["tradingsymbol"],
            instrument_type=InstrumentType.FUT,
            lot_size=1,
            tick_size=0.05,
        )


def fill_from_row(row: dict[str, Any], store: InstrumentStore) -> Fill:
    return Fill(
        fill_id=row["fill_id"],
        client_order_id=row["client_order_id"],
        strategy_id=row["strategy_id"],
        instrument=_instrument(row, store),
        side=Side(row["side"]),
        quantity=int(row["quantity"]),
        price=float(row["price"]),
        ts=to_ist(row["ts"]),
        product=Product(row["product"]),
        broker_order_id=row.get("broker_order_id"),
    )


def order_from_row(row: dict[str, Any], store: InstrumentStore) -> Order:
    req = OrderRequest(
        strategy_id=row["strategy_id"],
        instrument=_instrument(row, store),
        side=Side(row["side"]),
        quantity=int(row["quantity"]),
        order_type=OrderType(row["order_type"]),
        product=Product(row["product"]),
        price=row.get("price"),
        trigger_price=row.get("trigger_price"),
        leg_group_id=row.get("leg_group_id"),
        reason=row.get("reason") or "",
        client_order_id=row["client_order_id"],
    )
    return Order(
        request=req,
        status=OrderStatus(row["status"]),
        broker_order_id=row.get("broker_order_id"),
        filled_quantity=int(row.get("filled_quantity") or 0),
        average_price=float(row.get("average_price") or 0.0),
        status_message=row.get("status_message") or "",
        created_at=to_ist(row["created_at"]),
        updated_at=to_ist(row["updated_at"]),
    )


async def rebuild(
    engine: TradingEngine, store: JournalStore, mode: str, today: date
) -> RecoveryReport:
    """Steps 1-3. Call before engine.start()."""
    report = RecoveryReport()
    instruments = engine.instruments
    book = engine.positions

    for row in await store.load_carry_fills(mode, today):
        fill = fill_from_row(row, instruments)
        if fill.instrument.expiry is not None and fill.instrument.expiry < today:
            continue  # settled at expiry
        if instruments.find(fill.instrument.key) is None:
            continue  # contract no longer listed: expired
        book.apply_fill(fill)
    for pos in book.positions():
        pos.realized_pnl = 0.0
        pos.charges = 0.0
    report.carried_positions = len(book.open_positions())

    for row in await store.load_fills(mode, today):
        fill = fill_from_row(row, instruments)
        engine.fills.append(fill)
        book.apply_fill(fill, float(row.get("charges") or 0.0))
        report.fills_replayed += 1

    for row in await store.load_open_orders(mode, today):
        engine.adopt_order(order_from_row(row, instruments))
        report.orders_restored += 1
    log.info("journal replayed", extra=report.as_dict())
    return report


def _looks_like_our_tag(tag: Any) -> bool:
    return isinstance(tag, str) and len(tag) == CLIENT_ORDER_ID_LEN and tag.isalnum()


async def compare_with_broker(
    engine: TradingEngine,
    broker: KiteBroker,
    store: JournalStore,
    mode: str,
    today: date,
    *,
    report: RecoveryReport,
) -> RecoveryReport:
    """Step 4 (live only). Fills `report.position_diffs` / `order_issues`."""
    report.checked_with_broker = True
    net = (await broker.positions()).get("net", [])
    theirs: dict[tuple[int, str], dict[str, Any]] = {}
    for row in net:
        if row.get("exchange") != Exchange.NFO.value or row.get("product") not in TRADED_PRODUCTS:
            continue
        theirs[(int(row["instrument_token"]), row["product"])] = row
    ours: dict[tuple[int, str], int] = {}
    meta: dict[tuple[int, str], Instrument] = {}
    for pos in engine.positions.positions():
        key = (pos.instrument.token, pos.product.value)
        ours[key] = ours.get(key, 0) + pos.quantity
        meta[key] = pos.instrument
    for key in sorted(set(ours) | set(theirs)):
        our_q = ours.get(key, 0)
        row = theirs.get(key, {})
        their_q = int(row.get("quantity") or 0)
        if our_q != their_q:
            inst = meta.get(key)
            report.position_diffs.append(
                PositionDiff(
                    token=key[0],
                    exchange=str(row.get("exchange") or (inst.exchange if inst else "NFO")),
                    tradingsymbol=str(
                        row.get("tradingsymbol") or (inst.tradingsymbol if inst else key[0])
                    ),
                    product=key[1],
                    ours=our_q,
                    broker=their_q,
                    broker_avg=float(row.get("average_price") or 0.0),
                )
            )

    known = await store.load_order_ids(mode, today)
    for row in await broker.orders():
        tag = row.get("tag")
        if _looks_like_our_tag(tag) and tag not in known:
            report.order_issues.append(
                f"Kite order {row.get('order_id')} tagged {tag} ({row.get('status')}) "
                "is missing from the journal"
            )
    return report


def adopt_broker_positions(engine: TradingEngine, diffs: list[PositionDiff], now: datetime) -> int:
    """Operator action: book the difference to Kite under strategy '__recovered__',
    so the system's view matches the broker and the position can be squared off."""
    booked = 0
    for d in diffs:
        delta = d.broker - d.ours
        if delta == 0:
            continue
        inst = engine.instruments.find(f"{d.exchange}:{d.tradingsymbol}")
        if inst is None:
            try:
                inst = engine.instruments.by_token(d.token)
            except KeyError:
                log.error("cannot adopt unknown instrument", extra={"symbol": d.tradingsymbol})
                continue
        side = Side.BUY if delta > 0 else Side.SELL
        last = engine.last_tick(inst.token)
        price = d.broker_avg or (last.ltp if last else 0.0)
        cid = new_client_order_id()
        req = OrderRequest(
            strategy_id=RECOVERED,
            instrument=inst,
            side=side,
            quantity=abs(delta),
            product=Product(d.product),
            reason="operator adopted broker position",
            client_order_id=cid,
        )
        order = Order(
            req,
            status=OrderStatus.COMPLETE,
            filled_quantity=abs(delta),
            average_price=price,
            created_at=now,
            updated_at=now,
        )
        fill = Fill(
            fill_id=f"ADOPT-{cid}",
            client_order_id=cid,
            strategy_id=RECOVERED,
            instrument=inst,
            side=side,
            quantity=abs(delta),
            price=price,
            ts=now,
            product=Product(d.product),
        )
        engine.apply_external_fill(order, fill)
        booked += 1
    return booked
