"""
Core microstructure mathematics for institutional market making and execution.

Implements:
  - Avellaneda-Stoikov optimal market making model (2008)
  - Order book imbalance and pressure metrics
  - Inventory risk quantification
  - Arrival rate (κ) and fill probability estimation
  - VWAP / TWAP execution benchmarking
  - Market impact models (square-root law)

All functions are pure (no I/O) and work with plain floats/lists.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import NamedTuple


# ─── Avellaneda-Stoikov Model ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ASTParameters:
    """
    Calibrated parameters for the Avellaneda-Stoikov model.

    Attributes:
        gamma:  Risk-aversion coefficient (0.01–0.1 typical).
        sigma:  Asset volatility (per-second, annualised / sqrt(252*6.5*3600)).
        kappa:  Order arrival intensity (orders per second per unit spread).
        T:      Session horizon in seconds (e.g. 23400 for 6.5 h).
        q_max:  Maximum signed inventory (shares), hard limit.
    """
    gamma: float = 0.05
    sigma: float = 0.0002   # ≈ 20% annualised on a liquid mid-cap
    kappa: float = 1.5      # calibrated from LOB data
    T: float = 23400.0      # 6.5-hour session in seconds
    q_max: float = 1000.0


def reservation_price(
    mid: float,
    inventory: float,
    t_elapsed: float,
    params: ASTParameters,
) -> float:
    """
    Avellaneda-Stoikov reservation (indifference) price.

        r = s − q · γ · σ² · (T − t)

    Args:
        mid:        Current mid-price.
        inventory:  Signed inventory in shares (positive = long).
        t_elapsed:  Seconds elapsed since session open.
        params:     Model parameters.

    Returns:
        Reservation price (float).
    """
    time_remaining = max(params.T - t_elapsed, 1.0)  # avoid zero at session end
    return mid - inventory * params.gamma * (params.sigma ** 2) * time_remaining


def optimal_spread(
    t_elapsed: float,
    params: ASTParameters,
) -> float:
    """
    Avellaneda-Stoikov optimal full spread δ* (bid-ask total width).

        δ* = γ · σ² · (T − t) + (2/γ) · ln(1 + γ/κ)

    Args:
        t_elapsed:  Seconds elapsed since session open.
        params:     Model parameters.

    Returns:
        Optimal total spread in price units.
    """
    time_remaining = max(params.T - t_elapsed, 1.0)
    inventory_term = params.gamma * (params.sigma ** 2) * time_remaining
    liquidity_term = (2.0 / params.gamma) * math.log(1.0 + params.gamma / params.kappa)
    return inventory_term + liquidity_term


def optimal_quotes(
    mid: float,
    inventory: float,
    t_elapsed: float,
    params: ASTParameters,
    tick_size: float = 0.01,
) -> tuple[float, float]:
    """
    Compute optimal bid and ask prices, snapped to tick grid.

    Returns:
        (bid_price, ask_price) — both rounded to tick_size.
    """
    r = reservation_price(mid, inventory, t_elapsed, params)
    half_spread = optimal_spread(t_elapsed, params) / 2.0

    raw_bid = r - half_spread
    raw_ask = r + half_spread

    # Snap to tick grid (round bid down, ask up for conservative quoting)
    bid = math.floor(raw_bid / tick_size) * tick_size
    ask = math.ceil(raw_ask / tick_size) * tick_size

    # Enforce minimum one-tick spread
    if ask - bid < tick_size:
        ask = bid + tick_size

    return round(bid, 6), round(ask, 6)


def fill_probability(
    spread_half: float,
    params: ASTParameters,
) -> float:
    """
    Approximate probability that a resting quote at half-spread distance fills
    in the next second, using the exponential intensity model.

        P(fill) ≈ 1 − exp(−κ · e^(−spread_half))

    Returns value in [0, 1].
    """
    intensity = params.kappa * math.exp(-spread_half)
    return 1.0 - math.exp(-intensity)


def inventory_risk(
    inventory: float,
    sigma: float,
    horizon_sec: float,
) -> float:
    """
    99th-percentile inventory P&L risk (Gaussian approximation).

        risk = 2.326 · |q| · σ · √horizon_sec

    Returns risk as a positive dollar amount.
    """
    z99 = 2.3263  # 99th percentile z-score
    return z99 * abs(inventory) * sigma * math.sqrt(horizon_sec)


# ─── Order Book Analysis ──────────────────────────────────────────────────────

@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    """Lightweight snapshot of a limit order book."""
    bids: list[Level] = field(default_factory=list)   # sorted descending by price
    asks: list[Level] = field(default_factory=list)   # sorted ascending by price

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba - bb


def order_book_imbalance(book: OrderBook, depth: int = 5) -> float:
    """
    Order book imbalance over top-N levels.

        OBI = (bid_volume − ask_volume) / (bid_volume + ask_volume)

    Returns value in [−1, +1]. Positive = more bid pressure (bullish lean).
    """
    bid_vol = sum(lvl.size for lvl in book.bids[:depth])
    ask_vol = sum(lvl.size for lvl in book.asks[:depth])
    total = bid_vol + ask_vol
    if total == 0.0:
        return 0.0
    return (bid_vol - ask_vol) / total


def weighted_mid_price(book: OrderBook) -> float | None:
    """
    Microprice: bid-size-weighted mid.

        microprice = ask × (bid_sz / (bid_sz + ask_sz))
                   + bid × (ask_sz / (bid_sz + ask_sz))

    Returns None if book is empty.
    """
    if not book.bids or not book.asks:
        return None
    bb, ba = book.bids[0], book.asks[0]
    total = bb.size + ba.size
    if total == 0.0:
        return book.mid
    return ba.price * (bb.size / total) + bb.price * (ba.size / total)


def market_depth(book: OrderBook, price_pct: float = 0.001) -> tuple[float, float]:
    """
    Cumulative bid/ask depth within price_pct of mid.

    Args:
        book:       Order book snapshot.
        price_pct:  Depth window as fraction of mid (default 0.1%).

    Returns:
        (bid_depth_dollars, ask_depth_dollars)
    """
    mid = book.mid
    if mid is None:
        return 0.0, 0.0

    window = mid * price_pct
    bid_depth = sum(
        lvl.price * lvl.size for lvl in book.bids if lvl.price >= mid - window
    )
    ask_depth = sum(
        lvl.price * lvl.size for lvl in book.asks if lvl.price <= mid + window
    )
    return bid_depth, ask_depth


# ─── Market Impact ────────────────────────────────────────────────────────────

def square_root_impact(
    order_size: float,
    adv: float,
    sigma: float,
    eta: float = 0.142,
) -> float:
    """
    Almgren-Chriss square-root market impact model.

        impact = η · σ · √(order_size / ADV)

    Args:
        order_size:  Shares to execute.
        adv:         Average daily volume (shares).
        sigma:       Daily volatility (fraction, e.g. 0.02 for 2%).
        eta:         Calibration constant (default Almgren 2005: 0.142).

    Returns:
        Expected permanent impact as fraction of price (e.g. 0.0003 = 3bps).
    """
    if adv <= 0:
        return 0.0
    participation = order_size / adv
    return eta * sigma * math.sqrt(participation)


def implementation_shortfall(
    decision_price: float,
    avg_fill: float,
    shares: float,
    direction: int,
) -> float:
    """
    Implementation shortfall (Perold 1988) in dollars.

        IS = direction · (avg_fill − decision_price) · shares

    direction: +1 for buy, -1 for sell.
    Positive IS = cost (slippage). Negative = price improvement.
    """
    return direction * (avg_fill - decision_price) * shares


# ─── VWAP / TWAP Benchmarking ─────────────────────────────────────────────────

class FillRecord(NamedTuple):
    price: float
    size: float
    timestamp_ns: int   # Unix nanoseconds


def vwap(fills: list[FillRecord]) -> float | None:
    """Volume-weighted average price over a list of fills."""
    total_value = sum(f.price * f.size for f in fills)
    total_size = sum(f.size for f in fills)
    if total_size == 0.0:
        return None
    return total_value / total_size


def twap(fills: list[FillRecord]) -> float | None:
    """Time-weighted average price (simple average of fill prices)."""
    if not fills:
        return None
    return sum(f.price for f in fills) / len(fills)


def slippage_bps(executed_price: float, benchmark_price: float, direction: int) -> float:
    """
    Slippage in basis points vs a benchmark (VWAP, TWAP, arrival).

        slippage = direction · (executed − benchmark) / benchmark · 10000

    direction: +1 for buy, -1 for sell.
    Positive = paid more than benchmark (adverse). Negative = price improvement.
    """
    if benchmark_price == 0.0:
        return 0.0
    return direction * (executed_price - benchmark_price) / benchmark_price * 10_000.0


# ─── Spread Decomposition (Roll model) ───────────────────────────────────────

def roll_spread(price_changes: list[float]) -> float:
    """
    Roll (1984) effective spread estimator from transaction price changes.

        c = √(−Cov(ΔP_t, ΔP_{t-1}))   if covariance < 0, else 0

    Returns estimated half-spread (effective cost per trade).
    """
    n = len(price_changes)
    if n < 4:
        return 0.0

    # First-order autocovariance
    mean = sum(price_changes) / n
    cov = sum(
        (price_changes[i] - mean) * (price_changes[i - 1] - mean)
        for i in range(1, n)
    ) / (n - 1)

    if cov >= 0:
        return 0.0
    return math.sqrt(-cov)


# ─── Inventory Skew Helper ────────────────────────────────────────────────────

def inventory_skew_factor(
    inventory: float,
    q_max: float,
    aggression: float = 1.0,
) -> float:
    """
    Returns a signed skew [-1, +1] indicating how aggressively to skew quotes.

    Positive → skew ask tighter / bid wider (offload long inventory).
    Negative → skew bid tighter / ask wider (offload short inventory).

    aggression: multiplier (1.0 = standard, 2.0 = aggressive offload).
    """
    if q_max <= 0:
        return 0.0
    skew = -inventory / q_max * aggression
    return max(-1.0, min(1.0, skew))
