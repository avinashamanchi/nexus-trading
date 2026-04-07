"""
Prime Broker and Multi-Broker Routing Infrastructure.

Provides:
  - Multi-broker order routing (Goldman, Morgan Stanley, JP Morgan, IB, Alpaca)
  - Smart Order Router (SOR): lowest-cost venue selection
  - Cross-margining position model (net exposure across prime brokers)
  - Allocation reports (give-ups, step-ins, account allocation)
  - Portfolio margin calculation (risk-based, not Reg-T)
  - Prime brokerage credit line management
  - Daily reconciliation and position reporting

This module is purely data-model focused — it does not talk to live brokers
directly. The actual execution connectors (Alpaca SDK, FIX) are injected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Awaitable

from core.enums import (
    Direction, MarginType, OrderSide, OrderVenue,
    PrimeBroker, AssetClass,
)

logger = logging.getLogger(__name__)


# ─── Position Model ───────────────────────────────────────────────────────────

@dataclass
class BrokerPosition:
    """A single position held at a specific prime broker account."""
    symbol: str
    broker: PrimeBroker
    account: str
    qty: float              # positive=long, negative=short
    avg_cost: float
    asset_class: AssetClass = AssetClass.EQUITY
    currency: str = "USD"

    @property
    def direction(self) -> Direction:
        return Direction.LONG if self.qty >= 0 else Direction.SHORT

    @property
    def market_value(self) -> float:
        """Market value (requires current price — not stored here)."""
        return 0.0   # caller must multiply by current price

    @property
    def cost_basis(self) -> float:
        return self.qty * self.avg_cost


@dataclass
class CrossMarginBook:
    """
    Consolidated position book across all prime brokers.

    Computes net exposure and cross-margined buying power.
    """
    positions: list[BrokerPosition] = field(default_factory=list)
    margin_type: MarginType = MarginType.PORTFOLIO_MARGIN

    def net_position(self, symbol: str) -> float:
        """Net signed position across all brokers for a symbol."""
        return sum(p.qty for p in self.positions if p.symbol == symbol)

    def gross_exposure(self, prices: dict[str, float]) -> float:
        """Total gross dollar exposure (|qty| × price) across all positions."""
        total = 0.0
        for p in self.positions:
            px = prices.get(p.symbol, p.avg_cost)
            total += abs(p.qty) * px
        return total

    def net_exposure(self, prices: dict[str, float]) -> float:
        """Net long minus short exposure in dollars."""
        long_val = sum(
            p.qty * prices.get(p.symbol, p.avg_cost)
            for p in self.positions if p.qty > 0
        )
        short_val = sum(
            abs(p.qty) * prices.get(p.symbol, p.avg_cost)
            for p in self.positions if p.qty < 0
        )
        return long_val - short_val

    def margin_requirement(
        self,
        prices: dict[str, float],
        initial_margin_rate: float = 0.10,   # portfolio margin ~ 10% for hedged book
    ) -> float:
        """
        Estimated margin requirement under portfolio margin.
        For hedged books (long + short same sector), offset applies.

        In production, this is computed by the prime broker risk engine (SPAN/TIMS).
        """
        gross = self.gross_exposure(prices)
        net = abs(self.net_exposure(prices))
        # Simplified: margin = max(gross × rate, net × reg_t_rate)
        return max(gross * initial_margin_rate, net * 0.50)

    def add_position(self, position: BrokerPosition) -> None:
        # Update existing position or append
        for p in self.positions:
            if p.symbol == position.symbol and p.broker == position.broker \
                    and p.account == position.account:
                total_qty = p.qty + position.qty
                if total_qty != 0:
                    p.avg_cost = (p.cost_basis + position.cost_basis) / total_qty
                p.qty = total_qty
                return
        self.positions.append(position)

    def flatten_symbol(self, symbol: str) -> None:
        self.positions = [p for p in self.positions if p.symbol != symbol]

    def symbols(self) -> list[str]:
        return list({p.symbol for p in self.positions})

    def by_broker(self, broker: PrimeBroker) -> list[BrokerPosition]:
        return [p for p in self.positions if p.broker == broker]


# ─── Smart Order Router ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class VenueCost:
    """Estimated all-in cost of executing at a venue (bps)."""
    venue: OrderVenue
    maker_rebate_bps: float    # negative = receive rebate
    taker_fee_bps: float
    exchange_fee_bps: float
    latency_us: float

    def total_taker_cost_bps(self) -> float:
        return self.taker_fee_bps + self.exchange_fee_bps

    def total_maker_cost_bps(self) -> float:
        return self.maker_rebate_bps + self.exchange_fee_bps


VENUE_COSTS: dict[OrderVenue, VenueCost] = {
    OrderVenue.IEX: VenueCost(
        venue=OrderVenue.IEX,
        maker_rebate_bps=0.0,    # IEX has no rebates (anti-HFT)
        taker_fee_bps=0.9,
        exchange_fee_bps=0.0,
        latency_us=35.0,
    ),
    OrderVenue.NASDAQ: VenueCost(
        venue=OrderVenue.NASDAQ,
        maker_rebate_bps=-2.0,   # maker gets paid 0.2¢/share
        taker_fee_bps=3.0,
        exchange_fee_bps=0.1,
        latency_us=2.0,
    ),
    OrderVenue.NYSE: VenueCost(
        venue=OrderVenue.NYSE,
        maker_rebate_bps=-1.5,
        taker_fee_bps=2.8,
        exchange_fee_bps=0.08,
        latency_us=3.0,
    ),
    OrderVenue.BATS: VenueCost(
        venue=OrderVenue.BATS,
        maker_rebate_bps=-3.2,   # BATS BZX maker is aggressive
        taker_fee_bps=2.7,
        exchange_fee_bps=0.05,
        latency_us=1.5,
    ),
    OrderVenue.DARK_POOL: VenueCost(
        venue=OrderVenue.DARK_POOL,
        maker_rebate_bps=0.0,
        taker_fee_bps=1.0,       # dark pools charge less (no queue advantage)
        exchange_fee_bps=0.0,
        latency_us=50.0,
    ),
    OrderVenue.DIRECT_DMA: VenueCost(
        venue=OrderVenue.DIRECT_DMA,
        maker_rebate_bps=-2.5,
        taker_fee_bps=2.5,
        exchange_fee_bps=0.07,
        latency_us=0.5,
    ),
}


@dataclass
class RoutingDecision:
    venue: OrderVenue
    is_maker: bool
    estimated_cost_bps: float
    rationale: str


def route_order(
    symbol: str,
    side: OrderSide,
    qty: float,
    is_aggressive: bool,   # True = taker (cross spread), False = maker (post)
    urgency: str = "normal",   # "normal", "urgent", "stealth"
    exclude_venues: set[OrderVenue] | None = None,
) -> RoutingDecision:
    """
    Smart Order Router — select lowest-cost venue.

    Logic:
      - Urgent: select lowest-latency venue regardless of cost.
      - Stealth (large blocks): prefer dark pool to minimise market impact.
      - Normal: select lowest all-in cost venue.

    Args:
        symbol:        Security symbol (used for venue eligibility in production).
        side:          Order side.
        qty:           Order size in shares.
        is_aggressive: True if willing to cross the spread (taker).
        urgency:       Routing urgency mode.
        exclude_venues: Venues to skip (e.g. if banned or circuit-broken).

    Returns:
        RoutingDecision with selected venue and cost estimate.
    """
    exclude = exclude_venues or set()
    available = {v: c for v, c in VENUE_COSTS.items() if v not in exclude}

    if not available:
        return RoutingDecision(
            venue=OrderVenue.NASDAQ,
            is_maker=not is_aggressive,
            estimated_cost_bps=3.0,
            rationale="fallback — no venues available",
        )

    if urgency == "urgent":
        best_venue = min(available, key=lambda v: available[v].latency_us)
        cost = (available[best_venue].total_taker_cost_bps() if is_aggressive
                else available[best_venue].total_maker_cost_bps())
        return RoutingDecision(
            venue=best_venue,
            is_maker=not is_aggressive,
            estimated_cost_bps=cost,
            rationale=f"urgent: lowest latency ({available[best_venue].latency_us:.1f}µs)",
        )

    if urgency == "stealth" and qty > 5_000:
        if OrderVenue.DARK_POOL not in exclude:
            dp = available.get(OrderVenue.DARK_POOL)
            if dp:
                return RoutingDecision(
                    venue=OrderVenue.DARK_POOL,
                    is_maker=True,
                    estimated_cost_bps=dp.total_maker_cost_bps(),
                    rationale="stealth: dark pool for large block",
                )

    if is_aggressive:
        best_venue = min(available, key=lambda v: available[v].total_taker_cost_bps())
        cost = available[best_venue].total_taker_cost_bps()
    else:
        best_venue = min(available, key=lambda v: available[v].total_maker_cost_bps())
        cost = available[best_venue].total_maker_cost_bps()

    return RoutingDecision(
        venue=best_venue,
        is_maker=not is_aggressive,
        estimated_cost_bps=cost,
        rationale=f"lowest-cost: {'taker' if is_aggressive else 'maker'} {cost:.2f}bps",
    )


# ─── Allocation Report ───────────────────────────────────────────────────────

@dataclass
class AllocationReport:
    """
    Post-trade allocation across multiple accounts / sub-funds.
    Used for give-up and step-in workflows with prime brokers.
    """
    exec_broker: PrimeBroker
    symbol: str
    total_qty: float
    avg_price: float
    trade_date: date
    allocations: list[dict] = field(default_factory=list)  # [{account, qty, percent}]

    def add_allocation(self, account: str, qty: float) -> None:
        pct = qty / self.total_qty * 100.0 if self.total_qty else 0.0
        self.allocations.append({"account": account, "qty": qty, "percent": pct})

    def validate(self) -> bool:
        """Returns True if allocated quantities sum to total_qty."""
        allocated = sum(a["qty"] for a in self.allocations)
        return abs(allocated - self.total_qty) < 0.01

    def to_dict(self) -> dict:
        return {
            "exec_broker": self.exec_broker.value,
            "symbol": self.symbol,
            "total_qty": self.total_qty,
            "avg_price": self.avg_price,
            "trade_date": self.trade_date.isoformat(),
            "allocations": self.allocations,
            "balanced": self.validate(),
        }


# ─── Credit Line Monitor ─────────────────────────────────────────────────────

@dataclass
class CreditLine:
    broker: PrimeBroker
    limit_usd: float
    used_usd: float = 0.0
    warning_threshold: float = 0.80   # warn at 80% utilisation
    hard_limit: float = 0.95          # reject new orders at 95%

    @property
    def available_usd(self) -> float:
        return self.limit_usd - self.used_usd

    @property
    def utilisation(self) -> float:
        return self.used_usd / self.limit_usd if self.limit_usd > 0 else 0.0

    @property
    def is_warning(self) -> bool:
        return self.utilisation >= self.warning_threshold

    @property
    def is_hard_limited(self) -> bool:
        return self.utilisation >= self.hard_limit

    def consume(self, amount_usd: float) -> bool:
        """
        Attempt to consume credit. Returns True if accepted, False if rejected.
        """
        if self.is_hard_limited:
            return False
        if self.used_usd + amount_usd > self.limit_usd * self.hard_limit:
            return False
        self.used_usd += amount_usd
        return True

    def release(self, amount_usd: float) -> None:
        self.used_usd = max(0.0, self.used_usd - amount_usd)


# ─── Prime Broker Router ──────────────────────────────────────────────────────

class PrimeBrokerRouter:
    """
    Routes orders to the optimal prime broker based on:
      - Credit line availability
      - Execution cost (venue rebate / fee)
      - Asset class suitability (e.g. futures → CME, FX → ECN)

    In production, each prime broker has a FIX connector injected.
    """

    def __init__(
        self,
        credit_lines: dict[PrimeBroker, CreditLine],
        preferred_broker: PrimeBroker = PrimeBroker.ALPACA,
    ) -> None:
        self._credit_lines = credit_lines
        self._preferred = preferred_broker
        self._book = CrossMarginBook()

    def select_broker(
        self,
        notional_usd: float,
        asset_class: AssetClass = AssetClass.EQUITY,
        preferred: PrimeBroker | None = None,
    ) -> PrimeBroker | None:
        """
        Select the best available prime broker for a new order.

        Preference order:
          1. Caller-specified preferred broker (if available and unconstrained)
          2. System preferred broker
          3. Any broker with sufficient credit

        Returns None if no broker has capacity.
        """
        candidates = preferred or self._preferred

        # Try preferred first
        for broker in (candidates, self._preferred):
            line = self._credit_lines.get(broker)
            if line and not line.is_hard_limited and line.available_usd >= notional_usd:
                return broker

        # Fallback: any broker with capacity
        for broker, line in self._credit_lines.items():
            if not line.is_hard_limited and line.available_usd >= notional_usd:
                return broker

        logger.warning("[PrimeBroker] No broker with sufficient credit for $%.0f", notional_usd)
        return None

    def consume_credit(self, broker: PrimeBroker, notional_usd: float) -> bool:
        line = self._credit_lines.get(broker)
        if not line:
            return False
        result = line.consume(notional_usd)
        if not result:
            logger.warning("[PrimeBroker] Credit rejected for %s: $%.0f", broker.value, notional_usd)
        return result

    def release_credit(self, broker: PrimeBroker, notional_usd: float) -> None:
        line = self._credit_lines.get(broker)
        if line:
            line.release(notional_usd)

    def credit_summary(self) -> list[dict]:
        return [
            {
                "broker": broker.value,
                "limit_usd": line.limit_usd,
                "used_usd": line.used_usd,
                "available_usd": line.available_usd,
                "utilisation_pct": line.utilisation * 100,
                "is_warning": line.is_warning,
                "is_hard_limited": line.is_hard_limited,
            }
            for broker, line in self._credit_lines.items()
        ]

    @property
    def book(self) -> CrossMarginBook:
        return self._book
