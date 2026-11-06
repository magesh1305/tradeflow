"""Per-fill F&O cost estimate (Zerodha, NSE). Rates come from Settings and CHANGE:
verify them at zerodha.com/charges before trusting net P&L.

  brokerage   futures: lower of 0.03% of order turnover or Rs 20 per executed order;
              options: flat Rs 20 per executed order
  STT         sell side: futures 0.05% of turnover; options 0.15% of premium
  exchange    futures 0.00183%; options 0.03553% of premium
  SEBI        Rs 10 per crore
  IPFT        Rs 0.01 per crore
  stamp       buy side: futures 0.002%; options 0.003%
  GST         18% of (brokerage + exchange + SEBI + IPFT)

Brokerage is per executed ORDER: an order filled in several parts pays it once.
The model tracks what each order has paid so far and charges only the difference.
Exercise/assignment STT on expiry is not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.core.models import Fill, Side

CRORE = 10_000_000


@dataclass(frozen=True, slots=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    ipft: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange
            + self.sebi
            + self.ipft
            + self.stamp
            + self.gst
        )


@dataclass
class FnoCharges:
    brokerage_per_order: float = 20.0
    brokerage_futures_pct: float = 0.03
    stt_futures_sell_pct: float = 0.05
    stt_options_sell_pct: float = 0.15
    exchange_txn_futures_pct: float = 0.00183
    exchange_txn_options_pct: float = 0.03553
    sebi_fee_per_crore: float = 10.0
    ipft_per_crore: float = 0.01
    stamp_futures_buy_pct: float = 0.002
    stamp_options_buy_pct: float = 0.003
    gst_pct: float = 18.0
    _order_turnover: dict[str, float] = field(default_factory=dict)
    _order_brokerage: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, s: Settings) -> FnoCharges:
        return cls(
            brokerage_per_order=s.brokerage_per_order,
            brokerage_futures_pct=s.brokerage_futures_pct,
            stt_futures_sell_pct=s.stt_futures_sell_pct,
            stt_options_sell_pct=s.stt_options_sell_pct,
            exchange_txn_futures_pct=s.exchange_txn_futures_pct,
            exchange_txn_options_pct=s.exchange_txn_options_pct,
            sebi_fee_per_crore=s.sebi_fee_per_crore,
            ipft_per_crore=s.ipft_per_crore,
            stamp_futures_buy_pct=s.stamp_futures_buy_pct,
            stamp_options_buy_pct=s.stamp_options_buy_pct,
            gst_pct=s.gst_pct,
        )

    def breakdown(self, fill: Fill) -> ChargeBreakdown:
        turnover = fill.quantity * fill.price  # premium turnover for options
        option = fill.instrument.is_option
        cid = fill.client_order_id

        order_turnover = self._order_turnover.get(cid, 0.0) + turnover
        self._order_turnover[cid] = order_turnover
        cap = self.brokerage_per_order
        due = cap if option else min(cap, order_turnover * self.brokerage_futures_pct / 100)
        brokerage = max(due - self._order_brokerage.get(cid, 0.0), 0.0)
        self._order_brokerage[cid] = self._order_brokerage.get(cid, 0.0) + brokerage

        sell = fill.side is Side.SELL
        stt_pct = self.stt_options_sell_pct if option else self.stt_futures_sell_pct
        stt = turnover * stt_pct / 100 if sell else 0.0
        exch_pct = self.exchange_txn_options_pct if option else self.exchange_txn_futures_pct
        exchange = turnover * exch_pct / 100
        sebi = turnover * self.sebi_fee_per_crore / CRORE
        ipft = turnover * self.ipft_per_crore / CRORE
        stamp_pct = self.stamp_options_buy_pct if option else self.stamp_futures_buy_pct
        stamp = 0.0 if sell else turnover * stamp_pct / 100
        gst = (brokerage + exchange + sebi + ipft) * self.gst_pct / 100
        return ChargeBreakdown(brokerage, stt, exchange, sebi, ipft, stamp, gst)

    def for_fill(self, fill: Fill) -> float:
        return round(self.breakdown(fill).total, 4)


class NoCharges:
    def for_fill(self, fill: Fill) -> float:
        return 0.0
