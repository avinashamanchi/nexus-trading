"""
Tests for Agent 24 — Child Order Slicing (TWAP, VWAP, POV, IS).

Covers:
  - ParentOrder properties (remaining_qty, fill_rate, num_slices)
  - AlgoExecutionEngine submit/cancel lifecycle
  - Per-algo next_child_order() timing/triggering logic
  - record_fill() state updates
  - Order completion detection
  - summary() and get_schedule() output contracts
  - ChildOrder field correctness (parent_id, slice_index sequential)
"""
from __future__ import annotations

import time
import uuid

import pytest

from agents.agent_24_algo_execution import (
    AlgoExecutionEngine,
    AlgoStatus,
    AlgoType,
    ChildOrder,
    ParentOrder,
    INTRADAY_VOLUME_PROFILE,
    get_volume_fraction,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _engine() -> AlgoExecutionEngine:
    return AlgoExecutionEngine()


def _make_order(
    qty: int = 1_000,
    duration_min: int = 60,
    algo: AlgoType = AlgoType.TWAP,
    child_size: int = 100,
    limit_price: float = 150.0,
    pov_rate: float = 0.05,
    urgency: float = 0.5,
) -> ParentOrder:
    return ParentOrder(
        order_id=uuid.uuid4().hex,
        symbol="AAPL",
        side="buy",
        total_qty=qty,
        algo_type=algo,
        limit_price=limit_price,
        duration_min=duration_min,
        child_size=child_size,
        pov_rate=pov_rate,
        urgency=urgency,
    )


def _twap_order(qty: int = 1_000, duration_min: int = 60) -> ParentOrder:
    return _make_order(qty=qty, duration_min=duration_min, algo=AlgoType.TWAP)


def _vwap_order(qty: int = 1_000, duration_min: int = 60) -> ParentOrder:
    return _make_order(qty=qty, duration_min=duration_min, algo=AlgoType.VWAP)


def _pov_order(qty: int = 1_000, pov_rate: float = 0.05) -> ParentOrder:
    return _make_order(qty=qty, algo=AlgoType.POV, pov_rate=pov_rate)


def _is_order(qty: int = 1_000, urgency: float = 1.0) -> ParentOrder:
    return _make_order(qty=qty, algo=AlgoType.IS, urgency=urgency)


# ─── TestParentOrder ───────────────────────────────────────────────────────────

class TestParentOrder:

    def test_remaining_qty_correct(self):
        order = _make_order(qty=10_000)
        assert order.remaining_qty == 10_000
        order.filled_qty = 3_000
        assert order.remaining_qty == 7_000

    def test_fill_rate_zero_initially(self):
        order = _make_order(qty=5_000)
        assert order.fill_rate == 0.0

    def test_fill_rate_partial(self):
        order = _make_order(qty=1_000)
        order.filled_qty = 500
        assert order.fill_rate == pytest.approx(0.5)

    def test_num_slices_ceil_division(self):
        # 150_000 / 100 = 1500 exactly
        order = _make_order(qty=150_000, child_size=100)
        assert order.num_slices == 1_500

    def test_num_slices_exact(self):
        # 1_000 / 100 = 10 exactly
        order = _make_order(qty=1_000, child_size=100)
        assert order.num_slices == 10

    def test_num_slices_rounds_up(self):
        # 1_001 / 100 = 10.01 → ceil = 11
        order = _make_order(qty=1_001, child_size=100)
        assert order.num_slices == 11

    def test_fill_rate_complete(self):
        order = _make_order(qty=1_000)
        order.filled_qty = 1_000
        assert order.fill_rate == pytest.approx(1.0)


# ─── TestAlgoExecutionEngine ───────────────────────────────────────────────────

class TestAlgoExecutionEngine:

    def test_submit_returns_order_id(self):
        engine = _engine()
        order = _twap_order()
        returned_id = engine.submit(order)
        assert returned_id == order.order_id

    def test_submit_sets_status_active(self):
        engine = _engine()
        order = _twap_order()
        engine.submit(order)
        assert order.status == AlgoStatus.ACTIVE

    def test_cancel_returns_true(self):
        engine = _engine()
        order = _twap_order()
        engine.submit(order)
        result = engine.cancel(order.order_id)
        assert result is True
        assert order.status == AlgoStatus.CANCELLED

    def test_cancel_nonexistent_returns_false(self):
        engine = _engine()
        result = engine.cancel("does-not-exist")
        assert result is False

    def test_cancel_completed_returns_false(self):
        engine = _engine()
        order = _twap_order(qty=100)   # single slice
        engine.submit(order)
        order.status = AlgoStatus.COMPLETED
        result = engine.cancel(order.order_id)
        assert result is False

    # ── TWAP ──────────────────────────────────────────────────────────────────

    def test_twap_returns_child_on_schedule(self):
        """First slice fires immediately at t=start."""
        engine = _engine()
        order = _twap_order(qty=1_000, duration_min=60)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns  # exactly at start
        child = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert child is not None
        assert isinstance(child, ChildOrder)

    def test_twap_returns_none_before_interval(self):
        """After sending slice 0, sending again immediately should be None."""
        engine = _engine()
        order = _twap_order(qty=1_000, duration_min=60)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns
        # Send first slice
        first = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert first is not None
        # Immediately try again — not enough time has passed for slice 2
        second = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert second is None

    def test_twap_child_has_correct_qty(self):
        """Child orders must have qty == child_size."""
        engine = _engine()
        order = _twap_order(qty=1_000)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns
        child = engine.next_child_order(order.order_id, now_ns, 155.0)
        assert child is not None
        assert child.qty == 100

    def test_twap_sends_multiple_slices_after_enough_time(self):
        """Backdating start by full duration should allow many slices."""
        engine = _engine()
        order = _twap_order(qty=500, duration_min=1)  # 5 slices
        engine.submit(order)
        # Backdate start by 2 minutes so all 5 slices are due
        order.start_time_ns = time.perf_counter_ns() - int(2 * 60 * 1e9)
        sent = 0
        for _ in range(10):
            child = engine.next_child_order(
                order.order_id, time.perf_counter_ns(), 150.0
            )
            if child is None:
                break
            sent += 1
        assert sent == 5

    # ── VWAP ──────────────────────────────────────────────────────────────────

    def test_vwap_returns_child_when_behind_volume(self):
        """
        At 50% elapsed time, ~50%+ of volume has traded (U-shape front-loads).
        With 0% filled the algo is behind → should send.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, duration_min=60)
        engine.submit(order)
        # Backdate start by 30 minutes (half elapsed)
        order.start_time_ns = time.perf_counter_ns() - int(30 * 60 * 1e9)
        now_ns = time.perf_counter_ns()
        child = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert child is not None

    def test_vwap_returns_none_when_ahead_of_volume(self):
        """
        At 10% elapsed time, only a small fraction of volume traded.
        If we've already sent 90% of slices we're ahead → should not send.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, duration_min=60)
        engine.submit(order)
        # Elapsed 10% of 60 min = 6 min
        order.start_time_ns = time.perf_counter_ns() - int(6 * 60 * 1e9)
        # Pretend 90% already sent
        order.slices_sent = 9  # 9 of 10 slices sent
        order.filled_qty = 900
        now_ns = time.perf_counter_ns()
        child = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert child is None

    # ── POV ───────────────────────────────────────────────────────────────────

    def test_pov_returns_child_when_volume_threshold_met(self):
        """volume=5000, pov_rate=0.05 → 5000 * 0.05 = 250 >= child_size=100."""
        engine = _engine()
        order = _pov_order(qty=1_000, pov_rate=0.05)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        child = engine.next_child_order(
            order.order_id, now_ns, 150.0, market_volume_this_period=5_000
        )
        assert child is not None

    def test_pov_returns_none_when_volume_insufficient(self):
        """volume=100, pov_rate=0.05 → 100 * 0.05 = 5 < child_size=100."""
        engine = _engine()
        order = _pov_order(qty=1_000, pov_rate=0.05)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        child = engine.next_child_order(
            order.order_id, now_ns, 150.0, market_volume_this_period=100
        )
        assert child is None

    def test_pov_exact_threshold(self):
        """volume=2000, pov_rate=0.05 → 2000 * 0.05 = 100 == child_size=100 → sends."""
        engine = _engine()
        order = _pov_order(qty=1_000, pov_rate=0.05)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        child = engine.next_child_order(
            order.order_id, now_ns, 150.0, market_volume_this_period=2_000
        )
        assert child is not None

    # ── IS (Implementation Shortfall) ─────────────────────────────────────────

    def test_is_aggressive_front_loads(self):
        """urgency=1.0, elapsed=0 → weight=1.0, threshold=0.5 → sends immediately."""
        engine = _engine()
        order = _is_order(qty=1_000, urgency=1.0)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns
        child = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert child is not None

    def test_is_patient_also_sends_at_start(self):
        """urgency=0.0, elapsed=0 → weight=1.0, threshold=0.5 → sends."""
        engine = _engine()
        order = _is_order(qty=1_000, urgency=0.0)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns
        child = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert child is not None

    def test_is_aggressive_sends_mid_execution(self):
        """urgency=1.0, elapsed=50% → weight=exp(-1*0.5*5)=exp(-2.5)≈0.082, threshold=0.25 → doesn't send."""
        engine = _engine()
        order = _is_order(qty=1_000, urgency=1.0)
        engine.submit(order)
        # 50% elapsed of 60 min = 30 min ago
        order.start_time_ns = time.perf_counter_ns() - int(30 * 60 * 1e9)
        order.slices_sent = 8  # already sent 8/10
        order.filled_qty = 800
        now_ns = time.perf_counter_ns()
        # weight = exp(-1.0 * 0.5 * 5) ≈ 0.082, threshold = 0.5 - 0.5*0.5 = 0.25
        # 0.082 < 0.25 → should NOT send (already filled most; urgency = front-load done)
        child = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert child is None

    # ── Fill Recording ────────────────────────────────────────────────────────

    def test_record_fill_updates_filled_qty(self):
        engine = _engine()
        order = _twap_order()
        engine.submit(order)
        engine.record_fill(order.order_id, 150.0, 100)
        assert order.filled_qty == 100

    def test_record_fill_updates_avg_price(self):
        engine = _engine()
        order = _twap_order()
        engine.submit(order)
        engine.record_fill(order.order_id, 150.0, 100)
        assert order.avg_fill_price == pytest.approx(150.0)

    def test_record_fill_avg_price_incremental(self):
        engine = _engine()
        order = _twap_order()
        engine.submit(order)
        engine.record_fill(order.order_id, 100.0, 100)
        engine.record_fill(order.order_id, 200.0, 100)
        # avg = (100*100 + 200*100) / 200 = 150
        assert order.avg_fill_price == pytest.approx(150.0)

    def test_order_completes_when_fully_filled(self):
        engine = _engine()
        order = _twap_order(qty=100)   # one slice
        engine.submit(order)
        engine.record_fill(order.order_id, 150.0, 100)
        assert order.status == AlgoStatus.COMPLETED

    def test_order_not_completed_when_partially_filled(self):
        engine = _engine()
        order = _twap_order(qty=1_000)
        engine.submit(order)
        engine.record_fill(order.order_id, 150.0, 100)
        assert order.status == AlgoStatus.ACTIVE

    # ── Summary & Schedule ────────────────────────────────────────────────────

    def test_summary_has_required_keys(self):
        engine = _engine()
        order = _twap_order()
        engine.submit(order)
        s = engine.summary(order.order_id)
        required_keys = {
            "order_id", "symbol", "algo", "total_qty", "filled_qty",
            "fill_rate", "slices_sent", "avg_fill_price",
            "tracking_error_bps", "status",
        }
        assert required_keys.issubset(s.keys())

    def test_summary_values_correct(self):
        engine = _engine()
        order = _twap_order(qty=1_000)
        engine.submit(order)
        engine.record_fill(order.order_id, 150.0, 500)
        s = engine.summary(order.order_id)
        assert s["filled_qty"] == 500
        assert s["fill_rate"] == pytest.approx(0.5, abs=1e-4)

    def test_get_schedule_length_matches_num_slices(self):
        engine = _engine()
        order = _twap_order(qty=1_000)  # 10 slices
        engine.submit(order)
        schedule = engine.get_schedule(order.order_id)
        assert len(schedule) == order.num_slices

    def test_get_schedule_has_required_keys(self):
        engine = _engine()
        order = _twap_order(qty=500)  # 5 slices
        engine.submit(order)
        schedule = engine.get_schedule(order.order_id)
        for entry in schedule:
            assert "slice_index" in entry
            assert "target_qty" in entry
            assert "target_time_min" in entry

    # ── Child Order Fields ────────────────────────────────────────────────────

    def test_child_order_parent_id_correct(self):
        engine = _engine()
        order = _twap_order(qty=1_000)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns
        child = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert child is not None
        assert child.parent_id == order.order_id

    def test_child_order_slice_index_sequential(self):
        """Slice indices should be 0, 1, 2, … in order."""
        engine = _engine()
        order = _twap_order(qty=300, duration_min=1)  # 3 slices
        engine.submit(order)
        # Backdate to ensure all slices are due
        order.start_time_ns = time.perf_counter_ns() - int(2 * 60 * 1e9)
        indices = []
        for _ in range(5):
            child = engine.next_child_order(
                order.order_id, time.perf_counter_ns(), 150.0
            )
            if child is None:
                break
            indices.append(child.slice_index)
        assert indices == list(range(len(indices)))

    def test_child_order_symbol_matches_parent(self):
        engine = _engine()
        order = _make_order(qty=100, algo=AlgoType.TWAP)
        order.symbol = "MSFT"
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns
        child = engine.next_child_order(order.order_id, now_ns, 200.0)
        assert child is not None
        assert child.symbol == "MSFT"

    def test_child_order_price_matches_market_price(self):
        engine = _engine()
        order = _twap_order(qty=100)
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns
        market_price = 175.25
        child = engine.next_child_order(order.order_id, now_ns, market_price)
        assert child is not None
        assert child.price == pytest.approx(market_price)

    def test_child_order_side_matches_parent(self):
        engine = _engine()
        order = _make_order(qty=100, algo=AlgoType.TWAP)
        order.side = "sell"
        engine.submit(order)
        now_ns = time.perf_counter_ns()
        order.start_time_ns = now_ns
        child = engine.next_child_order(order.order_id, now_ns, 150.0)
        assert child is not None
        assert child.side == "sell"
