"""
Agent 25 — Alternative Trading System (ATS) / Dark Pool

Implements an internal crossing engine that internalizes retail order flow
(Payment for Order Flow — PFoF) before routing to lit markets.

When a retail buy order arrives, the ATS checks if our Market Making Agent
has inventory to satisfy it internally. If so:
  - Cross the trade at the midpoint (improves retail fill by half the spread)
  - Market maker avoids exchange fee ($0.003/share)
  - ATS collects PFoF payment from retail broker (~$0.001/share)
  - Zero market footprint: not reported to tape until T+1 netting

Architecture:
  IncomingOrder → InventoryChecker → [Internalize | Route to Lit Market]
                                           ↓
                                    CrossedTrade (midpoint fill)

Internalization rate target: >70% (best-in-class dark pools achieve 85%)
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─── Fee Constants ─────────────────────────────────────────────────────────────

PFOF_PAYMENT_PER_SHARE = 0.001      # $ paid to retail broker for order flow
EXCHANGE_FEE_PER_SHARE = 0.003      # $ exchange fee avoided by internalizing
MIN_PRICE_IMPROVEMENT_BPS = 0.1     # minimum spread improvement to internalize


# ─── Enumerations ──────────────────────────────────────────────────────────────

class RoutingDecision(str, Enum):
    INTERNALIZED = "internalized"   # crossed internally at midpoint
    LIT_MARKET = "lit_market"       # routed to exchange
    REJECTED = "rejected"           # price improvement not possible


class OrderFlow(str, Enum):
    RETAIL = "retail"               # PFoF-eligible (from retail brokers)
    INSTITUTIONAL = "institutional" # block order
    ARBITRAGE = "arbitrage"         # not internalized (predatory)


# ─── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class IncomingOrder:
    order_id: str
    symbol: str
    side: str           # "buy" or "sell"
    qty: int
    limit_price: float  # 0.0 = market order
    flow_type: OrderFlow
    timestamp_ns: int = field(default_factory=lambda: time.perf_counter_ns())


@dataclass
class InventoryPosition:
    symbol: str
    long_qty: int = 0
    short_qty: int = 0
    avg_cost: float = 0.0

    @property
    def net_qty(self) -> int:
        return self.long_qty - self.short_qty

    @property
    def can_sell(self) -> bool:
        return self.long_qty > 0

    @property
    def can_buy_to_cover(self) -> bool:
        return self.short_qty > 0


@dataclass
class CrossedTrade:
    trade_id: str
    order_id: str
    symbol: str
    side: str
    qty: int
    cross_price: float          # midpoint
    bid: float
    ask: float
    spread_bps: float           # spread in basis points
    pfof_payment: float         # $ paid to retail broker (0.001 * qty)
    exchange_fee_saved: float   # $ saved vs lit market (0.003 * qty)
    routing_decision: RoutingDecision
    timestamp_ns: int


@dataclass
class ATSStats:
    total_orders: int = 0
    internalized: int = 0
    lit_routed: int = 0
    rejected: int = 0
    total_pfof_paid: float = 0.0
    total_fees_saved: float = 0.0
    total_volume_internalized: int = 0   # shares
    total_volume_lit: int = 0

    @property
    def internalization_rate(self) -> float:
        if self.total_orders == 0:
            return 0.0
        return self.internalized / self.total_orders

    @property
    def net_benefit_usd(self) -> float:
        return self.total_fees_saved - self.total_pfof_paid


# ─── ATS Engine ────────────────────────────────────────────────────────────────

class DarkPoolATS:
    """
    Internal crossing engine for payment-for-order-flow internalization.

    Retail orders arrive from broker-dealers who route order flow to the ATS
    in exchange for PFoF payments. The engine crosses eligible orders against
    market maker inventory at the midpoint, providing price improvement to
    retail clients while the market maker avoids exchange transaction fees.

    Arbitrage flow is never internalized — it is toxic order flow that would
    systematically pick off stale quotes in inventory.
    """

    def __init__(
        self,
        max_internalization_qty: int = 10_000,   # max shares per cross
        min_spread_bps: float = 1.0,             # only internalize if spread >= 1 bps
        internalize_arbitrage: bool = False,      # never internalize arb flow
    ):
        self._max_qty = max_internalization_qty
        self._min_spread_bps = min_spread_bps
        self._internalize_arb = internalize_arbitrage
        self._inventory: dict[str, InventoryPosition] = {}
        self._trades: list[CrossedTrade] = []
        self._stats = ATSStats()

    def update_inventory(
        self, symbol: str, long_qty: int, short_qty: int, avg_cost: float
    ) -> None:
        """Update market maker inventory (called by Agent 20 periodically)."""
        self._inventory[symbol] = InventoryPosition(
            symbol=symbol,
            long_qty=long_qty,
            short_qty=short_qty,
            avg_cost=avg_cost,
        )

    def _spread_bps(self, bid: float, ask: float) -> float:
        """Compute bid-ask spread in basis points."""
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return 0.0
        return (ask - bid) / mid * 10_000.0

    def _can_internalize(
        self, order: IncomingOrder, bid: float, ask: float
    ) -> bool:
        """
        Return True if we can cross this order internally.

        Rules:
          1. Must be retail or institutional flow (not arbitrage unless enabled)
          2. Spread must be >= min_spread_bps (enough room for price improvement)
          3. Must have inventory on the opposite side
          4. Qty must be <= max_internalization_qty
          5. For buy orders: need long inventory to sell to them
          6. For sell orders: need long inventory >= order qty to buy from them
        """
        # Rule 1: flow type gate
        if order.flow_type == OrderFlow.ARBITRAGE and not self._internalize_arb:
            return False

        # Rule 4: size limit
        if order.qty > self._max_qty:
            return False

        # Rule 2: spread must be wide enough for price improvement to be meaningful
        spd = self._spread_bps(bid, ask)
        if spd < self._min_spread_bps:
            return False

        # Rules 3, 5, 6: inventory check
        inv = self._inventory.get(order.symbol)
        if inv is None:
            return False

        if order.side == "buy":
            # We sell inventory to the retail buyer
            if not inv.can_sell:
                return False
            if inv.long_qty < order.qty:
                return False
        else:
            # order.side == "sell": we buy from the retail seller
            # We need long inventory capacity (or short capacity to cover)
            # Simple rule: long inventory must cover the purchase
            if inv.long_qty < order.qty:
                return False

        return True

    def route(
        self,
        order: IncomingOrder,
        bid: float,
        ask: float,
    ) -> CrossedTrade:
        """
        Route an incoming order: internalize or send to lit market.

        Returns a CrossedTrade with routing_decision set.
        Internalized crosses happen at the midpoint (price improvement).
        Lit-routed orders use ask price for buys, bid price for sells.
        """
        self._stats.total_orders += 1
        mid = (bid + ask) / 2.0
        spd_bps = self._spread_bps(bid, ask)

        if self._can_internalize(order, bid, ask):
            # Cross at midpoint — price improvement for retail client
            cross_price = mid
            pfof = PFOF_PAYMENT_PER_SHARE * order.qty
            fee_saved = EXCHANGE_FEE_PER_SHARE * order.qty

            # Reduce inventory
            inv = self._inventory[order.symbol]
            inv.long_qty -= order.qty   # deduct inventory used for this cross

            trade = CrossedTrade(
                trade_id=uuid.uuid4().hex,
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                cross_price=cross_price,
                bid=bid,
                ask=ask,
                spread_bps=spd_bps,
                pfof_payment=pfof,
                exchange_fee_saved=fee_saved,
                routing_decision=RoutingDecision.INTERNALIZED,
                timestamp_ns=time.perf_counter_ns(),
            )
            self._stats.internalized += 1
            self._stats.total_pfof_paid += pfof
            self._stats.total_fees_saved += fee_saved
            self._stats.total_volume_internalized += order.qty

        else:
            # Route to lit market — retail fills at best available lit price
            lit_price = ask if order.side == "buy" else bid

            trade = CrossedTrade(
                trade_id=uuid.uuid4().hex,
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                cross_price=lit_price,
                bid=bid,
                ask=ask,
                spread_bps=spd_bps,
                pfof_payment=0.0,
                exchange_fee_saved=0.0,
                routing_decision=RoutingDecision.LIT_MARKET,
                timestamp_ns=time.perf_counter_ns(),
            )
            self._stats.lit_routed += 1
            self._stats.total_volume_lit += order.qty

        self._trades.append(trade)
        return trade

    # ─── Accessors ─────────────────────────────────────────────────────────────

    def internalization_rate(self) -> float:
        """Return fraction of orders internalized (0.0 – 1.0)."""
        return self._stats.internalization_rate

    def stats(self) -> ATSStats:
        """Return a reference to the live ATSStats object."""
        return self._stats

    def trades(self) -> list[CrossedTrade]:
        """Return all crossed/routed trades recorded so far."""
        return list(self._trades)

    def inventory(self, symbol: str) -> Optional[InventoryPosition]:
        """Return the current inventory position for a symbol."""
        return self._inventory.get(symbol)

    def summary(self) -> dict:
        """Return a human-readable summary dict suitable for logging/display."""
        s = self._stats
        return {
            "total_orders": s.total_orders,
            "internalized": s.internalized,
            "lit_routed": s.lit_routed,
            "internalization_rate": round(s.internalization_rate, 4),
            "total_pfof_paid_usd": round(s.total_pfof_paid, 2),
            "total_fees_saved_usd": round(s.total_fees_saved, 2),
            "net_benefit_usd": round(s.net_benefit_usd, 2),
            "volume_internalized": s.total_volume_internalized,
            "volume_lit": s.total_volume_lit,
        }
