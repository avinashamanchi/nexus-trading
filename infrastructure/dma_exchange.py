"""
Direct Market Access (DMA) and Self-Clearing simulation.

Covers two institutional capabilities that compound each other:

1. DMA / Exchange Co-Location
   Exchange membership + cross-connect cable directly into the matching engine.
   FPGA → cross-connect → exchange (no prime-broker risk hop).
   Latency: ~220 ns vs ~2 ms through a broker.
   Revenue: maker rebates flow 100 % to the firm (no clearing spread).

2. Self-Clearing (DTCC / OCC)
   DTCC = Depository Trust & Clearing Corporation (US equities).
   OCC  = Options Clearing Corporation (US equity options).
   Clearing member nets all intraday trades; only net positions settle T+1.
   Fee savings: $0.001/share (broker clearing) → $0.00002/share (NSCC fee).
   At 10 M shares/day → $9,800/day → $2.45 M/year savings.

Classes:
  ExchangeMember          – models direct exchange membership
  MakerRebateTracker      – accumulates maker rebates per venue
  DMAOrderPath            – FPGA → cross-connect → matching engine
  TradeRecord             – one executed trade (for clearing input)
  NetSettlement           – DTCC end-of-day net position
  DTCCClearingEngine      – end-of-day netting + fee savings model
"""
from __future__ import annotations

import logging
import time
import asyncio
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from core.enums import OrderSide, OrderVenue

logger = logging.getLogger(__name__)


# ─── Exchange Membership ──────────────────────────────────────────────────────

@dataclass
class ExchangeMember:
    """
    Direct membership in a US equity exchange.

    Physical setup: cross-connect cable from firm's server rack to the
    exchange matching-engine switch in the same NY4 data-centre cabinet.
    Typical cable run: ~5 m → ~16 ns signal propagation (light in copper).
    """
    venue:                    OrderVenue
    member_id:                str
    cross_connect_latency_ns: int   = 80      # cable + NIC + switch
    membership_monthly_usd:   float = 15_000.0
    is_active:                bool  = True

    @property
    def total_path_latency_ns(self) -> int:
        """FPGA encode (80 ns) + cross-connect (80 ns) + NIC TX (60 ns)."""
        return 80 + self.cross_connect_latency_ns + 60

    @property
    def broker_path_latency_us(self) -> float:
        """Typical broker risk-check + route latency: ~2 ms."""
        return 2_000.0


# ─── Maker Rebate Tracker ────────────────────────────────────────────────────

_REBATES_PER_SHARE: dict[OrderVenue, float] = {
    OrderVenue.NASDAQ:    0.00200,   # BZX maker
    OrderVenue.NYSE:      0.00150,
    OrderVenue.BATS:      0.00320,   # BATS BZX — most aggressive
    OrderVenue.DIRECT_DMA: 0.00250,
}


@dataclass
class MakerRebateTracker:
    """Accumulates exchange maker rebates for limit orders that rest and fill."""
    _daily_shares:    dict = field(default_factory=dict)
    _daily_rebate:    float = 0.0
    _total_rebate:    float = 0.0

    def record_maker_fill(self, venue: OrderVenue, shares: int) -> float:
        """Record a maker fill. Returns the rebate earned in USD."""
        rate = _REBATES_PER_SHARE.get(venue, 0.0)
        rebate = shares * rate
        self._daily_shares[venue] = self._daily_shares.get(venue, 0) + shares
        self._daily_rebate += rebate
        self._total_rebate += rebate
        return rebate

    def daily_reset(self) -> float:
        """Reset daily counters. Returns the day's total rebate."""
        day = self._daily_rebate
        self._daily_shares.clear()
        self._daily_rebate = 0.0
        return day

    def summary(self) -> dict:
        return {
            "daily_rebate_usd":      round(self._daily_rebate, 4),
            "total_rebate_usd":      round(self._total_rebate, 4),
            "daily_shares_by_venue": dict(self._daily_shares),
        }


# ─── DMA Order Path ───────────────────────────────────────────────────────────

class DMAOrderPath:
    """
    Models the FPGA → cross-connect → exchange matching-engine path.

    Bypasses prime-broker risk checks entirely (the firm's FPGA BRAM handles
    pre-trade risk in hardware, <5 ns BRAM lookup).

    Protocol: NASDAQ OUCH or NYSE Pillar binary (not FIX — FIX adds ~50 µs).
    """

    def __init__(
        self,
        members:        dict[OrderVenue, ExchangeMember],
        rebate_tracker: MakerRebateTracker | None = None,
    ) -> None:
        self._members  = members
        self._rebates  = rebate_tracker or MakerRebateTracker()
        self._submitted = 0
        self._acked     = 0

    async def submit_order(
        self,
        venue:    OrderVenue,
        symbol:   str,
        side:     OrderSide,
        qty:      int,
        price:    float,
        is_maker: bool = True,
    ) -> dict:
        """Submit via DMA path. Returns ACK dict with latency and rebate."""
        member = self._members.get(venue)
        if member is None or not member.is_active:
            return {"status": "rejected", "reason": "no DMA membership"}

        self._submitted += 1
        latency_ns = member.total_path_latency_ns
        await asyncio.sleep(latency_ns / 1_000_000_000)

        self._acked += 1
        rebate = self._rebates.record_maker_fill(venue, qty) if is_maker else 0.0

        return {
            "status":     "ack",
            "order_id":   f"DMA-{venue.value}-{self._submitted}",
            "venue":      venue.value,
            "symbol":     symbol,
            "side":       side.value,
            "qty":        qty,
            "price":      price,
            "is_maker":   is_maker,
            "latency_ns": latency_ns,
            "rebate_usd": round(rebate, 6),
        }

    def latency_advantage_ns(self, venue: OrderVenue) -> int:
        """Nanoseconds saved vs broker path for a given venue."""
        member = self._members.get(venue)
        if member is None:
            return 0
        broker_ns = int(member.broker_path_latency_us * 1_000)
        return broker_ns - member.total_path_latency_ns

    @property
    def summary(self) -> dict:
        return {
            "orders_submitted": self._submitted,
            "orders_acked":     self._acked,
            "rebates":          self._rebates.summary(),
        }


# ─── Trade Record ─────────────────────────────────────────────────────────────

class SettlementStatus(str, Enum):
    PENDING    = "pending"
    NETTED     = "netted"
    SETTLED    = "settled"


@dataclass
class TradeRecord:
    trade_id:    str
    symbol:      str
    side:        OrderSide
    qty:         int
    price:       float
    venue:       OrderVenue
    timestamp_ns: int = field(default_factory=time.perf_counter_ns)
    status:      SettlementStatus = SettlementStatus.PENDING

    @property
    def signed_qty(self) -> int:
        return self.qty if self.side == OrderSide.BUY else -self.qty

    @property
    def notional_usd(self) -> float:
        return self.qty * self.price


# ─── Net Settlement ───────────────────────────────────────────────────────────

@dataclass
class NetSettlement:
    """NSCC Continuous Net Settlement result for one symbol."""
    symbol:       str
    net_qty:      int      # positive = net buyer, negative = net seller
    avg_price:    float
    gross_trades: int
    gross_shares: int

    @property
    def settlement_side(self) -> str:
        return "RECEIVE" if self.net_qty > 0 else "DELIVER"


# ─── DTCC Clearing Engine ─────────────────────────────────────────────────────

class DTCCClearingEngine:
    """
    Models DTCC (DTC) clearing membership for US equities.

    Core benefit: end-of-day netting via NSCC CNS (Continuous Net Settlement).

    Example:
      Buy  1 000 AAPL @ 10:00
      Sell   800 AAPL @ 14:00
      ──────────────────────
      Net:  +200 AAPL to receive at T+1  (1 800 gross → 200 net)

    Fee comparison:
      Broker-cleared:  $0.001 000/share
      NSCC member fee: $0.000 020/share
      Savings:         $0.000 980/share → $9 800 / million shares

    At 10 M shares/day: $9 800/day → $2.45 M/year.
    """

    BROKER_FEE_PER_SHARE: float = 0.001_00
    NSCC_FEE_PER_SHARE:   float = 0.000_020
    GUARANTEE_FUND_USD:   float = 25_000_000.0

    def __init__(self, member_id: str = "NEXUS_CLEARING_001") -> None:
        self._member_id = member_id
        self._trades: list[TradeRecord] = []
        self._settled_count = 0

    def submit_trade(self, trade: TradeRecord) -> str:
        """Submit a trade record for clearing. Returns clearing ID."""
        clearing_id = f"CLR-{len(self._trades):08d}"
        self._trades.append(trade)
        return clearing_id

    def net_positions(self) -> dict[str, NetSettlement]:
        """
        Compute end-of-day net positions via NSCC CNS.
        Returns one NetSettlement per symbol.
        """
        acc: dict[str, dict] = {}
        for t in self._trades:
            if t.symbol not in acc:
                acc[t.symbol] = {"net_qty": 0, "cost": 0.0,
                                  "trades": 0, "shares": 0}
            a = acc[t.symbol]
            a["net_qty"] += t.signed_qty
            a["cost"]    += t.qty * t.price
            a["trades"]  += 1
            a["shares"]  += t.qty

        return {
            sym: NetSettlement(
                symbol=sym,
                net_qty=v["net_qty"],
                avg_price=v["cost"] / v["shares"] if v["shares"] else 0.0,
                gross_trades=v["trades"],
                gross_shares=v["shares"],
            )
            for sym, v in acc.items()
        }

    def fee_savings_vs_broker(self) -> float:
        """Total clearing fee savings vs broker-cleared for current trade set."""
        total_shares = sum(t.qty for t in self._trades)
        broker = total_shares * self.BROKER_FEE_PER_SHARE
        nscc   = total_shares * self.NSCC_FEE_PER_SHARE
        return broker - nscc

    def netting_efficiency(self) -> float:
        """
        Net shares / gross shares.  0 = perfect netting, 1 = no netting.
        """
        nets = self.net_positions()
        gross = sum(t.qty for t in self._trades)
        net   = sum(abs(s.net_qty) for s in nets.values())
        return net / gross if gross > 0 else 0.0

    def daily_reset(self) -> None:
        self._settled_count += len(self._trades)
        self._trades.clear()

    def summary(self) -> dict:
        nets = self.net_positions()
        return {
            "member_id":          self._member_id,
            "pending_trades":     len(self._trades),
            "symbols_to_settle":  len(nets),
            "netting_efficiency": round(self.netting_efficiency(), 4),
            "fee_savings_usd":    round(self.fee_savings_vs_broker(), 4),
            "guarantee_fund_usd": self.GUARANTEE_FUND_USD,
        }
