"""
UCB1 multi-armed bandit with exponential decay markout scoring for venue selection.

VenueStats: per-venue statistics (fill rate, markout score, UCB1).
VenueToxicityTracker: tracks toxicity, selects best venue via UCB1.
ToxicitySOR: wraps existing prime_broker.route_order() with toxicity filtering.
"""
from __future__ import annotations
import math
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Import OrderVenue from core.enums
from core.enums import OrderVenue


@dataclass
class VenueStats:
    venue: OrderVenue
    n_selections: int = 0
    cumulative_reward: float = 0.0
    markout_score: float = 0.0      # exponential-decay weighted markout
    decay_lambda: float = 0.01
    last_update_ns: int = field(default_factory=lambda: time.perf_counter_ns())
    add_cancel_ratio: float = 0.0   # not used for UCB1, kept for reporting
    fill_rate: float = 1.0

    def mean_reward(self) -> float:
        return self.cumulative_reward / self.n_selections if self.n_selections > 0 else 0.0


class VenueToxicityTracker:
    def __init__(
        self,
        venues: list[OrderVenue],
        decay_lambda: float = 0.01,
        ucb_c: float = 1.414,
    ) -> None:
        self._stats: dict[OrderVenue, VenueStats] = {
            v: VenueStats(venue=v, decay_lambda=decay_lambda) for v in venues
        }
        self._total_selections: int = 0
        self._ucb_c = ucb_c
        self._decay_lambda = decay_lambda
        # fill records for markout: {fill_id: (venue, fill_price, direction, qty, mid_at_fill)}
        self._fill_records: dict[str, tuple] = {}
        self._fill_counter: int = 0

    def record_fill(
        self,
        venue: OrderVenue,
        fill_price: float,
        mid_at_fill: float,
        direction: int,  # +1 buy, -1 sell
        qty: float,
    ) -> str:
        """Record a fill. Returns fill_id for subsequent markout recording."""
        self._fill_counter += 1
        fill_id = f"fill_{self._fill_counter}"
        self._fill_records[fill_id] = (venue, fill_price, direction, qty, mid_at_fill)
        stats = self._stats.get(venue)
        if stats:
            stats.fill_rate = min(1.0, stats.fill_rate * 0.99 + 0.01)  # EMA
        return fill_id

    def record_markout(self, venue: OrderVenue, mid_5s_later: float, fill_id: str) -> None:
        """Record 5-second markout; negative markout = adverse selection / toxic fill."""
        if fill_id not in self._fill_records:
            return
        _venue, fill_price, direction, qty, _mid = self._fill_records.pop(fill_id)
        markout = direction * (fill_price - mid_5s_later)  # negative = toxic
        stats = self._stats.get(venue)
        if stats:
            self._apply_decay(stats)
            stats.markout_score = stats.markout_score * 0.95 + markout * 0.05  # EMA
            self.update_reward(venue, markout)

    def _apply_decay(self, stats: VenueStats) -> None:
        now_ns = time.perf_counter_ns()
        elapsed_sec = (now_ns - stats.last_update_ns) / 1e9
        stats.markout_score *= math.exp(-stats.decay_lambda * elapsed_sec)
        stats.last_update_ns = now_ns

    def ucb1_score(self, venue: OrderVenue, t: int) -> float:
        stats = self._stats.get(venue)
        if stats is None:
            return float("inf")
        if stats.n_selections == 0:
            return float("inf")
        exploration = self._ucb_c * math.sqrt(math.log(t) / stats.n_selections)
        return stats.mean_reward() + exploration

    def select_venue(
        self,
        candidates: list[OrderVenue],
        urgency: str = "normal",
        exclude: list[OrderVenue] | None = None,
    ) -> OrderVenue:
        exclude = exclude or []
        available = [v for v in candidates if v not in exclude]
        if not available:
            return candidates[0] if candidates else list(self._stats.keys())[0]

        # Urgency overrides
        if urgency == "urgent":
            # Pick lowest-latency (use IEX as default low-latency; fallback to first)
            for pref in [OrderVenue.IEX, OrderVenue.NASDAQ, OrderVenue.BATS]:
                if pref in available:
                    return pref
            return available[0]

        if urgency == "stealth":
            # Use dark pool if in candidates
            for pref in [OrderVenue.DARK_POOL]:
                if pref in available:
                    return pref

        # Normal: UCB1, exclude venues with markout < -0.5
        t = max(1, self._total_selections)
        best_venue = None
        best_score = float("-inf")
        for v in available:
            stats = self._stats.get(v)
            if stats and stats.markout_score < -0.5:
                continue  # too toxic
            score = self.ucb1_score(v, t)
            if score > best_score:
                best_score = score
                best_venue = v

        return best_venue or available[0]

    def update_reward(self, venue: OrderVenue, reward: float) -> None:
        stats = self._stats.get(venue)
        if stats:
            stats.n_selections += 1
            stats.cumulative_reward += reward
            self._total_selections += 1

    def toxicity_report(self) -> dict[str, dict]:
        return {
            v.value: {
                "n_selections": s.n_selections,
                "mean_reward": s.mean_reward(),
                "markout_score": s.markout_score,
                "fill_rate": s.fill_rate,
                "ucb1_score": self.ucb1_score(v, max(1, self._total_selections)),
            }
            for v, s in self._stats.items()
        }


# RoutingDecision: simple dataclass
from dataclasses import dataclass as _dc


@_dc
class RoutingDecision:
    venue: OrderVenue
    reason: str
    toxicity_score: float


class ToxicitySOR:
    """Wraps prime_broker.route_order() with toxicity-aware venue selection."""

    def __init__(self, tracker: VenueToxicityTracker) -> None:
        self._tracker = tracker

    def route(
        self,
        symbol: str,
        side: str,
        qty: float,
        is_aggressive: bool,
        urgency: str = "normal",
    ) -> RoutingDecision:
        all_venues = list(self._tracker._stats.keys())
        # Determine stealth condition
        if qty > 5000 and urgency == "stealth":
            urgency = "stealth"
        selected = self._tracker.select_venue(all_venues, urgency=urgency)
        stats = self._tracker._stats.get(selected)
        toxicity = stats.markout_score if stats else 0.0
        reason = f"UCB1 selection (urgency={urgency})"
        return RoutingDecision(venue=selected, reason=reason, toxicity_score=toxicity)
