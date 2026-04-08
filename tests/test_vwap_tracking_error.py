"""
Tests for Agent 24 — VWAP Volume Profile and Tracking Error.

Covers:
  - INTRADAY_VOLUME_PROFILE shape and normalisation
  - get_volume_fraction() monotonicity and boundary conditions
  - U-shape verification (open/close heavy, midday thin)
  - vwap_tracking_error_bps() correctness
  - VWAP schedule: fast early slices, full completion, fixed child qty
  - summary() tracking error near 0 when fills are at limit_price
"""
from __future__ import annotations

import math
import time
import uuid

import pytest

from agents.agent_24_algo_execution import (
    AlgoExecutionEngine,
    AlgoStatus,
    AlgoType,
    ParentOrder,
    INTRADAY_VOLUME_PROFILE,
    get_volume_fraction,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _engine() -> AlgoExecutionEngine:
    return AlgoExecutionEngine()


def _vwap_order(
    qty: int = 10_000,
    duration_min: int = 390,   # full 6.5-hour session
    limit_price: float = 100.0,
    child_size: int = 100,
) -> ParentOrder:
    return ParentOrder(
        order_id=uuid.uuid4().hex,
        symbol="SPY",
        side="buy",
        total_qty=qty,
        algo_type=AlgoType.VWAP,
        limit_price=limit_price,
        duration_min=duration_min,
        child_size=child_size,
    )


# ─── TestVWAPVolumeProfile ────────────────────────────────────────────────────

class TestVWAPVolumeProfile:

    def test_profile_sums_to_one(self):
        """The volume profile must integrate to 1.0 ± 0.001."""
        total = sum(INTRADAY_VOLUME_PROFILE)
        assert total == pytest.approx(1.0, abs=0.001)

    def test_profile_has_78_buckets(self):
        """78 five-minute buckets cover a 6.5-hour US equity session."""
        assert len(INTRADAY_VOLUME_PROFILE) == 78

    def test_profile_all_positive(self):
        """Every bucket must have positive volume."""
        assert all(v > 0 for v in INTRADAY_VOLUME_PROFILE)

    def test_volume_fraction_zero_at_start(self):
        assert get_volume_fraction(0.0) == pytest.approx(0.0, abs=1e-9)

    def test_volume_fraction_one_at_end(self):
        assert get_volume_fraction(1.0) == pytest.approx(1.0, abs=0.001)

    def test_volume_fraction_monotone(self):
        """Cumulative volume fraction must be non-decreasing."""
        points = [i / 100 for i in range(101)]
        fractions = [get_volume_fraction(p) for p in points]
        for i in range(1, len(fractions)):
            assert fractions[i] >= fractions[i - 1], (
                f"Non-monotone at i={i}: {fractions[i-1]} > {fractions[i]}"
            )

    def test_volume_fraction_stays_in_unit_interval(self):
        """All values must be in [0, 1]."""
        for frac in [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]:
            v = get_volume_fraction(frac)
            assert 0.0 <= v <= 1.0, f"Out of range at elapsed={frac}: {v}"

    def test_volume_fraction_heavy_at_open(self):
        """
        U-shape: opening 10% of session should capture > 10% of daily volume
        (opening rush effect).
        """
        frac_at_10pct = get_volume_fraction(0.10)
        assert frac_at_10pct > 0.10, (
            f"Expected open-heavy profile: {frac_at_10pct:.4f} should be > 0.10"
        )

    def test_volume_fraction_heavy_at_close(self):
        """
        U-shape: last 10% of session (elapsed 0.9→1.0) should contain
        more than 10% of daily volume.
        Volume in last 10% = 1.0 - get_volume_fraction(0.9)
        """
        remaining = 1.0 - get_volume_fraction(0.90)
        assert remaining > 0.10, (
            f"Expected close-heavy profile: last 10% = {remaining:.4f} should be > 0.10"
        )

    def test_opening_buckets_heavier_than_midday(self):
        """First 6 buckets (opening 30 min) each have more volume than midday buckets."""
        opening_avg = sum(INTRADAY_VOLUME_PROFILE[:6]) / 6
        midday_avg = sum(INTRADAY_VOLUME_PROFILE[20:50]) / 30
        assert opening_avg > midday_avg, (
            f"Opening avg {opening_avg:.5f} should exceed midday avg {midday_avg:.5f}"
        )

    def test_closing_buckets_heavier_than_midday(self):
        """Last 18 buckets (closing 90 min) each have more volume than midday buckets."""
        closing_avg = sum(INTRADAY_VOLUME_PROFILE[-18:]) / 18
        midday_avg = sum(INTRADAY_VOLUME_PROFILE[20:50]) / 30
        assert closing_avg > midday_avg, (
            f"Closing avg {closing_avg:.5f} should exceed midday avg {midday_avg:.5f}"
        )


# ─── TestVWAPTrackingError ────────────────────────────────────────────────────

class TestVWAPTrackingError:

    def test_tracking_error_zero_when_fills_at_vwap(self):
        """
        When avg_fill_price == limit_price (VWAP benchmark), error = 0 bps.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0)
        engine.submit(order)
        engine.record_fill(order.order_id, 100.0, 1_000)
        err = engine.vwap_tracking_error_bps(order.order_id)
        assert err == pytest.approx(0.0, abs=1e-9)

    def test_tracking_error_positive_when_paid_above(self):
        """
        Paying above the VWAP benchmark is bad for buys → positive tracking error.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0)
        engine.submit(order)
        engine.record_fill(order.order_id, 101.0, 1_000)
        err = engine.vwap_tracking_error_bps(order.order_id)
        assert err > 0.0

    def test_tracking_error_negative_when_paid_below(self):
        """
        Paying below VWAP is good for buys → negative tracking error.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0)
        engine.submit(order)
        engine.record_fill(order.order_id, 99.0, 1_000)
        err = engine.vwap_tracking_error_bps(order.order_id)
        assert err < 0.0

    def test_tracking_error_in_bps_scale(self):
        """
        1 cent above a $100 VWAP = 0.01/100 * 10_000 = 1.0 bps exactly.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0)
        engine.submit(order)
        engine.record_fill(order.order_id, 100.01, 1_000)
        err = engine.vwap_tracking_error_bps(order.order_id)
        assert err == pytest.approx(1.0, abs=0.001)

    def test_tracking_error_scales_with_price_deviation(self):
        """
        10 cents above $100 = 10 bps.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0)
        engine.submit(order)
        engine.record_fill(order.order_id, 100.10, 1_000)
        err = engine.vwap_tracking_error_bps(order.order_id)
        assert err == pytest.approx(10.0, abs=0.01)

    def test_tracking_error_unfilled_order_is_zero(self):
        """
        No fills yet → avg_fill_price=0 → tracking error = 0.0.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0)
        engine.submit(order)
        err = engine.vwap_tracking_error_bps(order.order_id)
        assert err == pytest.approx(0.0, abs=1e-9)

    def test_tracking_error_weighted_average(self):
        """
        Fill 500 shares at 100.02 and 500 shares at 100.00 → avg = 100.01 → 1 bps.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0)
        engine.submit(order)
        engine.record_fill(order.order_id, 100.02, 500)
        engine.record_fill(order.order_id, 100.00, 500)
        err = engine.vwap_tracking_error_bps(order.order_id)
        assert err == pytest.approx(1.0, abs=0.01)


# ─── TestVWAPSchedule ─────────────────────────────────────────────────────────

class TestVWAPSchedule:

    def test_vwap_fills_faster_at_open(self):
        """
        VWAP schedule should front-load: more than 10% of slices should have
        target times in the first 15% of the session (because open is heavy).
        """
        engine = _engine()
        order = _vwap_order(qty=10_000, duration_min=390, child_size=100)
        engine.submit(order)
        schedule = engine.get_schedule(order.order_id)
        total_slices = len(schedule)
        cutoff_time = 0.15 * 390  # 15% of session in minutes
        slices_in_first_15pct = sum(
            1 for entry in schedule if entry["target_time_min"] <= cutoff_time
        )
        ratio = slices_in_first_15pct / total_slices
        assert ratio > 0.15, (
            f"Expected >15% of slices in first 15% of session; got {ratio:.3f}"
        )

    def test_vwap_completes_full_order(self):
        """
        Running VWAP for a full simulated session should produce all slices.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, duration_min=60, child_size=100)
        engine.submit(order)

        duration_ns = order.duration_min * 60 * int(1e9)
        start_ns = time.perf_counter_ns() - duration_ns - int(1e9)  # past end
        order.start_time_ns = start_ns

        children_sent = []
        for _ in range(order.num_slices + 5):
            # Use a time slightly after start + duration to ensure all are due
            now_ns = start_ns + duration_ns + int(1e9)
            child = engine.next_child_order(order.order_id, now_ns, 100.0)
            if child is None:
                break
            children_sent.append(child)
            engine.record_fill(order.order_id, 100.0, child.qty)

        assert len(children_sent) == order.num_slices
        assert order.status == AlgoStatus.COMPLETED

    def test_vwap_child_qty_always_child_size(self):
        """
        Every child order must have qty == child_size (except possibly the last
        if total_qty is not perfectly divisible — but we test with exact multiples).
        """
        engine = _engine()
        order = _vwap_order(qty=500, duration_min=60, child_size=100)  # 5 slices exact
        engine.submit(order)

        duration_ns = order.duration_min * 60 * int(1e9)
        start_ns = time.perf_counter_ns() - duration_ns - int(1e9)
        order.start_time_ns = start_ns

        children = []
        for _ in range(order.num_slices + 2):
            now_ns = start_ns + duration_ns + int(1e9)
            child = engine.next_child_order(order.order_id, now_ns, 100.0)
            if child is None:
                break
            children.append(child)
            engine.record_fill(order.order_id, 100.0, child.qty)

        assert len(children) == 5
        for c in children:
            assert c.qty == 100

    def test_summary_tracking_error_within_0_5bps_on_perfect_fill(self):
        """
        When all fills happen exactly at limit_price, tracking error should be
        within ±0.5 bps.
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0, child_size=100)
        engine.submit(order)

        # Record 10 fills all at exactly limit_price
        for _ in range(10):
            engine.record_fill(order.order_id, 100.0, 100)

        s = engine.summary(order.order_id)
        assert abs(s["tracking_error_bps"]) <= 0.5

    def test_vwap_schedule_monotone_time(self):
        """
        Target times in the VWAP schedule should be non-decreasing
        (slices are ordered in time).
        """
        engine = _engine()
        order = _vwap_order(qty=2_000, duration_min=60, child_size=100)
        engine.submit(order)
        schedule = engine.get_schedule(order.order_id)
        times = [e["target_time_min"] for e in schedule]
        for i in range(1, len(times)):
            assert times[i] >= times[i - 1], (
                f"Non-monotone schedule at index {i}: {times[i-1]} > {times[i]}"
            )

    def test_vwap_large_order_slices_all_100(self):
        """
        A 100,000-share order with child_size=100 should produce exactly 1,000 slices.
        """
        order = _vwap_order(qty=100_000, child_size=100)
        assert order.num_slices == 1_000

    def test_vwap_tracking_error_multiple_fills_weighted(self):
        """
        Multiple fills at different prices: final tracking error reflects VWAP of fills.
        Fill 200 @ 99.0 and 800 @ 100.25: avg = (200*99 + 800*100.25)/1000 = 100.0
        → error ≈ 0 bps
        """
        engine = _engine()
        order = _vwap_order(qty=1_000, limit_price=100.0)
        engine.submit(order)
        engine.record_fill(order.order_id, 99.0, 200)
        engine.record_fill(order.order_id, 100.25, 800)
        # avg = (200*99 + 800*100.25) / 1000 = (19800 + 80200) / 1000 = 100.0
        err = engine.vwap_tracking_error_bps(order.order_id)
        assert err == pytest.approx(0.0, abs=0.1)
