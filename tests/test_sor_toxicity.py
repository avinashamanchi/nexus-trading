"""
Tests for:
  - VenueToxicityTracker (UCB1 multi-armed bandit with markout scoring)
  - ToxicitySOR          (toxicity-aware smart order router)
  - ClickHouseClient     (connect failure fallback)
  - TickWarehouseClickHouse (dual-write, batch flush)
"""
from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core.enums import OrderVenue
from infrastructure.venue_toxicity import (
    RoutingDecision,
    ToxicitySOR,
    VenueStats,
    VenueToxicityTracker,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

_ALL_VENUES = list(OrderVenue)
_SOME_VENUES = [OrderVenue.IEX, OrderVenue.NASDAQ, OrderVenue.BATS, OrderVenue.DARK_POOL]


def _tracker(venues=None) -> VenueToxicityTracker:
    return VenueToxicityTracker(venues or _SOME_VENUES)


# ═══════════════════════════════════════════════════════════════════════════════
#  TestVenueToxicityTracker
# ═══════════════════════════════════════════════════════════════════════════════

class TestVenueToxicityTracker:

    def test_initial_ucb1_is_inf_for_all(self):
        """Brand-new venues score +inf (exploration mode)."""
        t = _tracker()
        score = t.ucb1_score(OrderVenue.IEX, t=1)
        assert score == float("inf")

    def test_ucb1_finite_after_one_selection(self):
        t = _tracker()
        t.update_reward(OrderVenue.IEX, 0.1)
        score = t.ucb1_score(OrderVenue.IEX, t=1)
        assert math.isfinite(score)

    def test_ucb1_exploration_unvisited_beats_visited(self):
        """Unvisited venue (inf UCB1) beats a venue visited many times."""
        t = _tracker([OrderVenue.IEX, OrderVenue.NASDAQ])
        for _ in range(20):
            t.update_reward(OrderVenue.IEX, 0.5)
        # NASDAQ never selected → +inf → should be chosen
        selected = t.select_venue([OrderVenue.IEX, OrderVenue.NASDAQ])
        assert selected == OrderVenue.NASDAQ

    def test_toxic_venue_excluded_in_normal_mode(self):
        """Venue with markout < -0.5 is excluded from normal selection."""
        t = _tracker([OrderVenue.IEX, OrderVenue.NASDAQ])
        # Poison IEX
        t._stats[OrderVenue.IEX].markout_score = -1.0
        t.update_reward(OrderVenue.IEX, -1.0)
        t.update_reward(OrderVenue.NASDAQ, 0.1)
        selected = t.select_venue([OrderVenue.IEX, OrderVenue.NASDAQ])
        assert selected == OrderVenue.NASDAQ

    def test_urgent_mode_returns_low_latency_venue(self):
        """urgency='urgent' should pick IEX or NASDAQ over DARK_POOL."""
        t = _tracker([OrderVenue.IEX, OrderVenue.DARK_POOL, OrderVenue.NASDAQ])
        selected = t.select_venue(
            [OrderVenue.IEX, OrderVenue.DARK_POOL, OrderVenue.NASDAQ],
            urgency="urgent",
        )
        assert selected in (OrderVenue.IEX, OrderVenue.NASDAQ)

    def test_stealth_mode_returns_dark_pool(self):
        """urgency='stealth' should return DARK_POOL when available."""
        t = _tracker([OrderVenue.IEX, OrderVenue.DARK_POOL])
        selected = t.select_venue(
            [OrderVenue.IEX, OrderVenue.DARK_POOL],
            urgency="stealth",
        )
        assert selected == OrderVenue.DARK_POOL

    def test_record_fill_and_markout_updates_score(self):
        t = _tracker()
        fill_id = t.record_fill(
            venue=OrderVenue.IEX,
            fill_price=100.0,
            mid_at_fill=100.0,
            direction=1,
            qty=100,
        )
        # Mid moved against us (toxic: paid 100, mid moved to 100.50)
        t.record_markout(OrderVenue.IEX, mid_5s_later=100.50, fill_id=fill_id)
        score = t._stats[OrderVenue.IEX].markout_score
        assert score < 0   # negative markout → toxic

    def test_decay_reduces_markout_score(self):
        """Markout score should decay toward zero over time."""
        t = _tracker()
        t._stats[OrderVenue.IEX].markout_score = -1.0
        # Simulate 10 seconds elapsed by backdating last_update
        import time
        t._stats[OrderVenue.IEX].last_update_ns -= int(10e9)
        t._apply_decay(t._stats[OrderVenue.IEX])
        # After decay, absolute value should be smaller
        assert abs(t._stats[OrderVenue.IEX].markout_score) < 1.0

    def test_toxicity_report_has_all_venues(self):
        t = _tracker()
        report = t.toxicity_report()
        for venue in _SOME_VENUES:
            assert venue.value in report

    def test_update_reward_increments_n_selections(self):
        t = _tracker()
        t.update_reward(OrderVenue.IEX, 0.1)
        t.update_reward(OrderVenue.IEX, 0.2)
        assert t._stats[OrderVenue.IEX].n_selections == 2

    def test_select_venue_returns_fallback_when_all_toxic(self):
        """If all venues are toxic, still returns a venue (no crash)."""
        t = _tracker([OrderVenue.IEX, OrderVenue.NASDAQ])
        t._stats[OrderVenue.IEX].markout_score = -1.0
        t._stats[OrderVenue.NASDAQ].markout_score = -1.0
        t.update_reward(OrderVenue.IEX, -1.0)
        t.update_reward(OrderVenue.NASDAQ, -1.0)
        result = t.select_venue([OrderVenue.IEX, OrderVenue.NASDAQ])
        assert result in (OrderVenue.IEX, OrderVenue.NASDAQ)


# ═══════════════════════════════════════════════════════════════════════════════
#  TestToxicitySOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestToxicitySOR:

    def _sor(self):
        tracker = _tracker()
        return ToxicitySOR(tracker)

    def test_route_returns_routing_decision(self):
        sor = self._sor()
        decision = sor.route("AAPL", "buy", 100, is_aggressive=True)
        assert isinstance(decision, RoutingDecision)

    def test_routing_decision_has_required_fields(self):
        sor = self._sor()
        decision = sor.route("TSLA", "sell", 50, is_aggressive=False)
        assert hasattr(decision, "venue")
        assert hasattr(decision, "reason")
        assert hasattr(decision, "toxicity_score")

    def test_urgent_routing_selects_low_latency(self):
        sor = self._sor()
        decision = sor.route("NVDA", "buy", 100, is_aggressive=True, urgency="urgent")
        assert decision.venue in (OrderVenue.IEX, OrderVenue.NASDAQ, OrderVenue.BATS)

    def test_stealth_routing_selects_dark_pool(self):
        sor = self._sor()
        decision = sor.route("AAPL", "buy", 10000, is_aggressive=False, urgency="stealth")
        assert decision.venue == OrderVenue.DARK_POOL

    def test_toxic_venue_not_selected_in_normal_mode(self):
        tracker = VenueToxicityTracker([OrderVenue.IEX, OrderVenue.NASDAQ])
        tracker._stats[OrderVenue.IEX].markout_score = -1.0
        tracker.update_reward(OrderVenue.IEX, -1.0)
        tracker.update_reward(OrderVenue.NASDAQ, 0.2)
        sor = ToxicitySOR(tracker)
        decision = sor.route("AAPL", "buy", 100, is_aggressive=True)
        assert decision.venue == OrderVenue.NASDAQ


# ═══════════════════════════════════════════════════════════════════════════════
#  TestClickHouseStore
# ═══════════════════════════════════════════════════════════════════════════════

class TestClickHouseStore:

    @pytest.mark.asyncio
    async def test_connect_returns_false_on_failure(self):
        """connect() to an unreachable host returns False (no exception)."""
        from infrastructure.clickhouse_store import ClickHouseClient
        ch = ClickHouseClient(host="127.0.0.1", port=19999, timeout=0.1)
        result = await ch.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_write_always_calls_fallback(self):
        """write() with unavailable CH still writes to fallback."""
        from infrastructure.clickhouse_store import ClickHouseClient, TickWarehouseClickHouse

        ch = ClickHouseClient(host="127.0.0.1", port=19999, timeout=0.1)
        fallback = AsyncMock()
        fallback.open = AsyncMock()
        fallback.write = AsyncMock()
        fallback.close = AsyncMock()

        wh = TickWarehouseClickHouse(ch, fallback)
        wh._connected = False   # simulate unavailable CH

        tick = SimpleNamespace(symbol="AAPL", bid=100.0, ask=100.1, last=100.05,
                               volume=500, timestamp_ns=0, side="")
        await wh.write(tick)
        fallback.write.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_accumulates_without_flush(self):
        """write() 499 times → batch has 499 items (threshold = 500)."""
        from infrastructure.clickhouse_store import ClickHouseClient, TickWarehouseClickHouse

        ch = ClickHouseClient(host="127.0.0.1", port=19999, timeout=0.1)
        fallback = AsyncMock()
        fallback.write = AsyncMock()

        wh = TickWarehouseClickHouse(ch, fallback)
        wh._connected = True  # simulate connected so batching runs

        tick = SimpleNamespace(symbol="X", bid=1.0, ask=1.1, last=1.05,
                               volume=10, timestamp_ns=0, side="")
        for _ in range(499):
            await wh.write(tick)

        assert len(wh._batch) == 499

    @pytest.mark.asyncio
    async def test_open_calls_fallback_open(self):
        """open() always calls fallback.open()."""
        from infrastructure.clickhouse_store import ClickHouseClient, TickWarehouseClickHouse

        ch = ClickHouseClient(host="127.0.0.1", port=19999, timeout=0.1)
        fallback = AsyncMock()
        fallback.open = AsyncMock()
        fallback.close = AsyncMock()

        wh = TickWarehouseClickHouse(ch, fallback)
        await wh.open()
        fallback.open.assert_called_once()
        # Cleanup
        if wh._flush_task:
            wh._flush_task.cancel()
            try:
                await wh._flush_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_close_flushes_remaining(self):
        """close() flushes remaining items before closing."""
        from infrastructure.clickhouse_store import ClickHouseClient, TickWarehouseClickHouse

        ch = ClickHouseClient(host="127.0.0.1", port=19999, timeout=0.1)
        fallback = AsyncMock()
        fallback.write = AsyncMock()
        fallback.close = AsyncMock()

        wh = TickWarehouseClickHouse(ch, fallback)
        wh._connected = True
        # Pre-load some batch items
        wh._batch = [{"ts": 1, "symbol": "X", "price": 100.0,
                      "bid": 99.9, "ask": 100.1, "volume": 10, "side": ""}]

        # Mock _flush_to_clickhouse to verify it's called
        wh._flush_to_clickhouse = AsyncMock()
        await wh.close()
        wh._flush_to_clickhouse.assert_called()
