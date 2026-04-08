"""
Agent 24 — Institutional Execution Algorithms (The Block Desk)

Implements four industry-standard execution algorithms for slicing
large parent orders into invisible child orders:

  VWAP  — Volume-Weighted Average Price: tracks intraday volume curve
  TWAP  — Time-Weighted Average Price: equal time slices
  POV   — Percentage of Volume: participates at fixed % of market volume
  IS    — Implementation Shortfall: minimizes market impact vs decision price

A pension fund order of 1,000,000 AAPL shares submitted at once would
crash the price by ~2%. These algos drip-feed 100-share child orders
invisibly over 6 hours, targeting <0.5 bps tracking error vs the benchmark.

Parent order: symbol, side, total_qty, algo_type, limit_price, duration_min
Child orders: symbol, side, qty=100, price, algo_type, parent_id, slice_index
"""
from __future__ import annotations

import math
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Algorithm Types ──────────────────────────────────────────────────────────

class AlgoType(str, Enum):
    VWAP = "vwap"
    TWAP = "twap"
    POV = "pov"
    IS = "is"   # Implementation Shortfall


class AlgoStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# ─── Intraday Volume Profile (VWAP benchmark) ─────────────────────────────────

# Historical intraday volume profile — 78 five-minute buckets (6.5-hour session)
# Values sum to 1.0, U-shaped: heavy open/close, thin midday.
# Construction:
#   Buckets  0– 5  (first 30 min, opening rush):     0.025 each  → 6 × 0.025 = 0.150
#   Buckets  6–59  (midday, 54 buckets):              0.0087 each → 54 × 0.0087 ≈ 0.4698
#   Buckets 60–77  (last 90 min, closing rush, 18):   0.021167 each → 18 × 0.021167 ≈ 0.381
# Total ≈ 1.0

def _build_volume_profile() -> list[float]:
    profile: list[float] = []
    # Opening rush: first 6 buckets (0:00–0:30)
    for _ in range(6):
        profile.append(0.025)
    # Midday thin: buckets 6–59 (54 buckets)
    for _ in range(54):
        profile.append(0.0087)
    # Closing rush: buckets 60–77 (18 buckets)
    for _ in range(18):
        profile.append(0.021167)
    # Normalise to exactly 1.0
    total = sum(profile)
    profile = [v / total for v in profile]
    return profile


INTRADAY_VOLUME_PROFILE: list[float] = _build_volume_profile()

# Precompute cumulative volume fractions for O(1) lookup
_CUMULATIVE_PROFILE: list[float] = []
_running = 0.0
for _v in INTRADAY_VOLUME_PROFILE:
    _running += _v
    _CUMULATIVE_PROFILE.append(_running)


def get_volume_fraction(elapsed_fraction: float) -> float:
    """
    Returns cumulative volume fraction [0, 1] at elapsed_fraction of session.
    Used by VWAP to determine how much of the parent order should be filled by now.

    elapsed_fraction: 0.0 = session open, 1.0 = session close
    The U-shaped profile means the fraction grows faster at open and close.
    """
    if elapsed_fraction <= 0.0:
        return 0.0
    if elapsed_fraction >= 1.0:
        return 1.0
    # Map elapsed_fraction → bucket index in the 78-bucket profile
    bucket_float = elapsed_fraction * len(INTRADAY_VOLUME_PROFILE)
    bucket_idx = int(bucket_float)
    if bucket_idx >= len(INTRADAY_VOLUME_PROFILE):
        return 1.0
    # Cumulative fraction up to the start of this bucket
    cum_before = _CUMULATIVE_PROFILE[bucket_idx - 1] if bucket_idx > 0 else 0.0
    # Partial fraction within the current bucket
    partial = (bucket_float - bucket_idx) * INTRADAY_VOLUME_PROFILE[bucket_idx]
    return min(1.0, cum_before + partial)


# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class ParentOrder:
    """
    A large block order submitted by an institutional client.
    Execution is delegated to one of the four algo engines.
    """
    order_id: str           # uuid4 hex
    symbol: str
    side: str               # "buy" or "sell"
    total_qty: int          # e.g. 1_000_000
    algo_type: AlgoType
    limit_price: float      # decision price / arrival price (VWAP benchmark)
    duration_min: int       # total execution window in minutes (e.g. 360 = 6h)
    child_size: int = 100   # fixed child order size in shares
    pov_rate: float = 0.05  # POV: participate at 5% of market volume
    urgency: float = 0.5    # IS: 0=patient, 1=aggressive

    # State (mutated during execution)
    status: AlgoStatus = AlgoStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    slices_sent: int = 0
    start_time_ns: int = 0  # set when activate() is called

    @property
    def remaining_qty(self) -> int:
        return self.total_qty - self.filled_qty

    @property
    def fill_rate(self) -> float:
        if self.total_qty == 0:
            return 0.0
        return self.filled_qty / self.total_qty

    @property
    def num_slices(self) -> int:
        """Total number of child orders (ceil division)."""
        return math.ceil(self.total_qty / self.child_size)


@dataclass
class ChildOrder:
    """
    A single small slice of a parent order sent to the market.
    These are kept small (100 shares) to avoid price impact.
    """
    child_id: str
    parent_id: str
    symbol: str
    side: str
    qty: int
    price: float
    algo_type: AlgoType
    slice_index: int
    timestamp_ns: int
    venue: str = "nasdaq"
    status: str = "pending"


# ─── Execution Engine ─────────────────────────────────────────────────────────

class AlgoExecutionEngine:
    """
    Slices parent orders into child orders using the configured algorithm.

    Pure computation — no I/O, no asyncio. Callers drive execution by
    repeatedly calling next_child_order() and record_fill() on each
    market tick or timer event.

    Supported algorithms
    --------------------
    TWAP  — equal-interval slicing: send one child every T/N seconds.
    VWAP  — volume-curve tracking: accelerate at open/close, slow midday.
    POV   — participation rate: send when market_volume × pov_rate >= child_size.
    IS    — implementation shortfall: front-load aggressively when urgency=1,
            back-load when urgency=0 to minimise opportunity cost.
    """

    def __init__(self) -> None:
        self._orders: dict[str, ParentOrder] = {}
        self._children: dict[str, list[ChildOrder]] = {}   # parent_id → list

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def submit(self, order: ParentOrder) -> str:
        """
        Register a parent order and mark it ACTIVE.
        Sets start_time_ns to the current monotonic time if not already set.
        Returns the order_id.
        """
        import time as _time
        if order.start_time_ns == 0:
            order.start_time_ns = _time.perf_counter_ns()
        order.status = AlgoStatus.ACTIVE
        self._orders[order.order_id] = order
        self._children[order.order_id] = []
        logger.info(
            "AlgoEngine: submitted %s %s %s qty=%d algo=%s duration=%dmin",
            order.order_id[:8], order.algo_type.value,
            order.symbol, order.total_qty, order.algo_type.value, order.duration_min,
        )
        return order.order_id

    def cancel(self, order_id: str) -> bool:
        """
        Cancel a parent order.
        Returns True if the order existed and was cancelled; False otherwise.
        """
        order = self._orders.get(order_id)
        if order is None:
            return False
        if order.status in (AlgoStatus.COMPLETED, AlgoStatus.CANCELLED):
            return False
        order.status = AlgoStatus.CANCELLED
        logger.info("AlgoEngine: cancelled order %s", order_id[:8])
        return True

    # ── Next Child Order ──────────────────────────────────────────────────────

    def next_child_order(
        self,
        order_id: str,
        current_time_ns: int,
        current_market_price: float,
        market_volume_this_period: int = 0,
    ) -> Optional[ChildOrder]:
        """
        Compute the next child order to send, or None if the algo says wait.

        Must be called repeatedly on each market tick or timer.
        When a child order is returned it is automatically registered
        in self._children and order.slices_sent is incremented.

        Parameters
        ----------
        order_id               : parent order identifier
        current_time_ns        : monotonic clock reading (perf_counter_ns)
        current_market_price   : mid-price to use as child limit price
        market_volume_this_period : shares traded since last call (POV only)
        """
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"Unknown order_id: {order_id}")

        if order.status != AlgoStatus.ACTIVE:
            return None

        # Guard: already fully sent
        if order.remaining_qty <= 0 or order.slices_sent >= order.num_slices:
            order.status = AlgoStatus.COMPLETED
            return None

        should_send = self._should_send(order, current_time_ns, market_volume_this_period)
        if not should_send:
            return None

        # Build child order
        order.slices_sent += 1
        child_qty = min(order.child_size, order.remaining_qty)

        child = ChildOrder(
            child_id=f"{order.order_id}_{order.slices_sent}",
            parent_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            qty=child_qty,
            price=current_market_price,
            algo_type=order.algo_type,
            slice_index=order.slices_sent - 1,   # 0-based index
            timestamp_ns=current_time_ns,
            venue="nasdaq",
            status="pending",
        )
        self._children[order.order_id].append(child)

        logger.debug(
            "AlgoEngine: %s slice %d/%d qty=%d @ %.4f",
            order.algo_type.value, order.slices_sent, order.num_slices,
            child_qty, current_market_price,
        )

        # Check completion after this slice
        if order.slices_sent >= order.num_slices or order.remaining_qty - child_qty <= 0:
            # Will complete once fill is recorded; mark tentatively if we've sent all
            if order.slices_sent >= order.num_slices:
                order.status = AlgoStatus.COMPLETED

        return child

    def _should_send(
        self,
        order: ParentOrder,
        current_time_ns: int,
        market_volume_this_period: int,
    ) -> bool:
        """Dispatch to the per-algo send decision."""
        algo = order.algo_type
        if algo == AlgoType.TWAP:
            return self._twap_should_send(order, current_time_ns)
        elif algo == AlgoType.VWAP:
            return self._vwap_should_send(order, current_time_ns)
        elif algo == AlgoType.POV:
            return self._pov_should_send(order, market_volume_this_period)
        elif algo == AlgoType.IS:
            return self._is_should_send(order, current_time_ns)
        return False

    def _elapsed_fraction(self, order: ParentOrder, current_time_ns: int) -> float:
        """Elapsed fraction of total duration [0, 1]."""
        duration_ns = order.duration_min * 60 * 1_000_000_000
        if duration_ns <= 0:
            return 1.0
        return (current_time_ns - order.start_time_ns) / duration_ns

    # ── TWAP ──────────────────────────────────────────────────────────────────

    def _twap_should_send(self, order: ParentOrder, current_time_ns: int) -> bool:
        """
        Send one slice every interval = (duration_ns / num_slices) nanoseconds.
        The first slice fires at t=0 (slices_sent=0 and elapsed >= 0).
        """
        duration_ns = order.duration_min * 60 * 1_000_000_000
        interval_ns = duration_ns / order.num_slices
        elapsed_ns = current_time_ns - order.start_time_ns
        # Due slice index is how many we should have sent by now
        due = int(elapsed_ns / interval_ns) if interval_ns > 0 else order.num_slices
        return order.slices_sent <= due

    # ── VWAP ──────────────────────────────────────────────────────────────────

    def _vwap_should_send(self, order: ParentOrder, current_time_ns: int) -> bool:
        """
        Send when cumulative volume target says we should have more filled.
        target_filled = total_qty * get_volume_fraction(elapsed_fraction)
        Send if filled_qty < target_filled.
        """
        elapsed = self._elapsed_fraction(order, current_time_ns)
        elapsed = min(elapsed, 1.0)
        target_qty = order.total_qty * get_volume_fraction(elapsed)
        # We use filled_qty + (slices_sent - filled slices) as proxy;
        # simpler: compare slices_sent * child_size vs target
        sent_qty = order.slices_sent * order.child_size
        return sent_qty < target_qty and order.remaining_qty > 0

    # ── POV ───────────────────────────────────────────────────────────────────

    def _pov_should_send(self, order: ParentOrder, market_volume_this_period: int) -> bool:
        """
        Send when market_volume_this_period * pov_rate >= child_size.
        I.e., market has traded enough that our target participation produces
        at least one full child order worth.
        """
        return (market_volume_this_period * order.pov_rate) >= order.child_size

    # ── IS (Implementation Shortfall) ─────────────────────────────────────────

    def _is_should_send(self, order: ParentOrder, current_time_ns: int) -> bool:
        """
        Front-load when urgency=1, back-load when urgency=0.

        Weight = exp(-urgency * elapsed * 5)
        At elapsed=0 (start), weight=1.0 regardless of urgency.
        At elapsed=1 (end),   weight decreases with urgency.

        Decision rule: send if weight > threshold
        threshold = 0.5 - elapsed * 0.5
        At elapsed=0: threshold=0.5 → weight(1.0) > 0.5 → send immediately.
        At elapsed=1: threshold=0.0 → always send remaining (catch-up).
        For high urgency, weight decays fast so most slices go early.
        For low urgency, weight decays slowly so slices spread more evenly.
        """
        elapsed = self._elapsed_fraction(order, current_time_ns)
        elapsed = max(0.0, min(elapsed, 1.0))
        weight = math.exp(-order.urgency * elapsed * 5.0)
        threshold = 0.5 - elapsed * 0.5
        return weight > threshold

    # ── Fill Recording ────────────────────────────────────────────────────────

    def record_fill(self, order_id: str, fill_price: float, fill_qty: int) -> None:
        """
        Update parent order state with a child fill.
        Recalculates volume-weighted average fill price incrementally.
        """
        order = self._orders[order_id]
        if fill_qty <= 0:
            return
        # Incremental VWAP: avg = (avg * filled + price * qty) / (filled + qty)
        total_value = order.avg_fill_price * order.filled_qty + fill_price * fill_qty
        order.filled_qty += fill_qty
        order.avg_fill_price = total_value / order.filled_qty if order.filled_qty > 0 else 0.0
        # Check completion
        if order.filled_qty >= order.total_qty:
            order.status = AlgoStatus.COMPLETED
            logger.info(
                "AlgoEngine: order %s COMPLETED — avg_fill=%.4f tracking_err=%.3f bps",
                order_id[:8], order.avg_fill_price,
                self.vwap_tracking_error_bps(order_id),
            )

    # ── Analytics ─────────────────────────────────────────────────────────────

    def vwap_tracking_error_bps(self, order_id: str) -> float:
        """
        VWAP tracking error = (avg_fill_price - market_vwap) / market_vwap * 10_000

        Positive = paid more than market VWAP (bad for buys).
        Uses limit_price as a proxy for the market VWAP benchmark.
        Returns 0.0 if no fills or limit_price is zero.
        """
        order = self._orders[order_id]
        if order.avg_fill_price == 0.0 or order.limit_price == 0.0:
            return 0.0
        return (order.avg_fill_price - order.limit_price) / order.limit_price * 10_000.0

    def get_schedule(self, order_id: str) -> list[dict]:
        """
        Return the planned execution schedule as a list of dicts:
            {slice_index, target_qty, target_time_min}

        For TWAP: equal intervals.
        For VWAP: time proportional to volume profile.
        For POV/IS: equal intervals (approximate; actual send depends on market).
        """
        order = self._orders[order_id]
        schedule: list[dict] = []
        for i in range(order.num_slices):
            qty = min(order.child_size, order.total_qty - i * order.child_size)
            if order.algo_type == AlgoType.VWAP:
                # Invert volume profile: find the time at which cumulative
                # volume fraction = (i+1)/num_slices
                target_frac = (i + 1) / order.num_slices
                # Binary search over get_volume_fraction to find elapsed_fraction
                lo, hi = 0.0, 1.0
                for _ in range(32):
                    mid = (lo + hi) / 2.0
                    if get_volume_fraction(mid) < target_frac:
                        lo = mid
                    else:
                        hi = mid
                target_time_min = round(lo * order.duration_min, 2)
            else:
                # TWAP / POV / IS: equal spacing
                target_time_min = round((i + 1) * order.duration_min / order.num_slices, 2)
            schedule.append({
                "slice_index": i,
                "target_qty": qty,
                "target_time_min": target_time_min,
            })
        return schedule

    def summary(self, order_id: str) -> dict:
        """Return execution summary dict."""
        order = self._orders[order_id]
        return {
            "order_id": order.order_id,
            "symbol": order.symbol,
            "algo": order.algo_type.value,
            "total_qty": order.total_qty,
            "filled_qty": order.filled_qty,
            "fill_rate": round(order.fill_rate, 4),
            "slices_sent": order.slices_sent,
            "avg_fill_price": round(order.avg_fill_price, 4),
            "tracking_error_bps": round(self.vwap_tracking_error_bps(order_id), 3),
            "status": order.status.value,
        }

    def get_order(self, order_id: str) -> Optional[ParentOrder]:
        """Retrieve a parent order by ID."""
        return self._orders.get(order_id)

    def get_children(self, order_id: str) -> list[ChildOrder]:
        """Retrieve all child orders for a parent order."""
        return self._children.get(order_id, [])
