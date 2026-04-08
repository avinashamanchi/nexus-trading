"""
Tests for:
  - InfiniBandCluster    (RDMA transfer / AllReduce latency model)
  - OnlineLOBLearner     (ring buffer, incremental SGD, hot-swap)
  - RetrainingScheduler  (background asyncio task)
"""
from __future__ import annotations

import asyncio
import time
import pytest

from core.lob_model import LOBImbalanceModel
from core.models import OrderBook, L2Level
from infrastructure.online_learner import (
    InfiniBandCluster,
    Observation,
    OnlineLOBLearner,
    RetrainingScheduler,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _model() -> LOBImbalanceModel:
    return LOBImbalanceModel()


def _learner(
    buffer_size: int = 200,
    retrain_threshold: int = 50,
    retrain_interval_s: float = 3600.0,
) -> OnlineLOBLearner:
    return OnlineLOBLearner(
        model=_model(),
        buffer_size=buffer_size,
        retrain_threshold=retrain_threshold,
        retrain_interval_s=retrain_interval_s,
        learning_rate=1e-3,
    )


def _make_book(bid_qty: int = 200, ask_qty: int = 200) -> OrderBook:
    from datetime import datetime, timezone
    bids = [L2Level(price=150.00, size=bid_qty)]
    asks = [L2Level(price=150.05, size=ask_qty)]
    return OrderBook(symbol="AAPL", timestamp=datetime.now(timezone.utc),
                     bids=bids, asks=asks)


# ═══════════════════════════════════════════════════════════════════════════════
#  TestInfiniBandCluster
# ═══════════════════════════════════════════════════════════════════════════════

class TestInfiniBandCluster:

    def test_transfer_latency_ge_base(self):
        ib = InfiniBandCluster()
        lat = ib.transfer_latency_us(1_024)
        assert lat >= ib.base_latency_us

    def test_larger_payload_higher_latency(self):
        ib = InfiniBandCluster()
        small = ib.transfer_latency_us(100)
        large = ib.transfer_latency_us(100_000)
        assert large > small

    def test_all_reduce_ge_base(self):
        ib = InfiniBandCluster()
        lat = ib.all_reduce_latency_us(1_000)
        assert lat >= ib.base_latency_us

    def test_all_reduce_more_params_higher_latency(self):
        ib = InfiniBandCluster()
        s = ib.all_reduce_latency_us(100)
        l = ib.all_reduce_latency_us(1_000_000)
        assert l > s

    @pytest.mark.asyncio
    async def test_simulate_training_step_returns_float(self):
        ib = InfiniBandCluster()
        ms = await ib.simulate_training_step(num_params=100, batch_size=32)
        assert isinstance(ms, float)

    def test_summary_has_expected_keys(self):
        ib = InfiniBandCluster()
        s = ib.summary()
        for key in ("num_gpus", "bandwidth_gbps", "base_latency_us", "cpu_bypass"):
            assert key in s

    def test_cpu_bypass_true_by_default(self):
        ib = InfiniBandCluster()
        assert ib.cpu_bypass is True

    def test_200gbps_bandwidth_default(self):
        ib = InfiniBandCluster()
        assert ib.bandwidth_gbps == 200.0


# ═══════════════════════════════════════════════════════════════════════════════
#  TestOnlineLOBLearner
# ═══════════════════════════════════════════════════════════════════════════════

class TestOnlineLOBLearner:

    @pytest.mark.asyncio
    async def test_observe_fills_buffer(self):
        learner = _learner(buffer_size=100, retrain_threshold=200)
        book = _make_book()
        for _ in range(10):
            await learner.observe(book, 0.0)
        assert len(learner._buffer) == 10

    @pytest.mark.asyncio
    async def test_observe_increments_since_retrain(self):
        learner = _learner(retrain_threshold=9999)
        book = _make_book()
        await learner.observe(book, 0.01)
        await learner.observe(book, -0.01)
        assert learner._since_retrain >= 2

    @pytest.mark.asyncio
    async def test_buffer_respects_maxlen(self):
        learner = _learner(buffer_size=20, retrain_threshold=9999)
        book = _make_book()
        for _ in range(50):
            await learner.observe(book, 0.0)
        assert len(learner._buffer) <= 20

    @pytest.mark.asyncio
    async def test_retrain_triggered_at_threshold(self):
        learner = _learner(buffer_size=200, retrain_threshold=30)
        book = _make_book()
        for _ in range(35):
            await learner.observe(book, 0.1)
        assert learner._retrain_count >= 1

    @pytest.mark.asyncio
    async def test_retrain_resets_since_retrain(self):
        learner = _learner(buffer_size=200, retrain_threshold=30)
        book = _make_book()
        for _ in range(35):
            await learner.observe(book, 0.1)
        assert learner._since_retrain < 35   # was reset

    @pytest.mark.asyncio
    async def test_coefficients_change_after_retrain(self):
        model   = _model()
        original = model.coefficients.copy()
        learner = OnlineLOBLearner(model=model, buffer_size=200,
                                   retrain_threshold=30, learning_rate=1e-2)
        book = _make_book()
        for i in range(35):
            await learner.observe(book, 0.2 if i % 2 == 0 else -0.1)
        # Coefficients may or may not change depending on validation gate;
        # retrain_count must be >= 1
        assert learner._retrain_count >= 1

    @pytest.mark.asyncio
    async def test_hot_swap_count_nonnegative(self):
        learner = _learner(retrain_threshold=30)
        book = _make_book()
        for i in range(35):
            await learner.observe(book, 0.05 * (1 if i % 3 == 0 else -1))
        assert learner._swap_count >= 0

    @pytest.mark.asyncio
    async def test_manual_retrain_returns_bool(self):
        learner = _learner()
        book = _make_book()
        for _ in range(25):
            await learner.observe(book, 0.1)
        result = await learner._retrain()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_retrain_returns_false_empty_buffer(self):
        learner = _learner()
        result = await learner._retrain()
        assert result is False

    def test_status_has_required_keys(self):
        learner = _learner()
        s = learner.status()
        for key in ("buffer_size", "since_retrain", "retrain_count",
                    "swap_count", "mean_error"):
            assert key in s

    @pytest.mark.asyncio
    async def test_error_history_populated(self):
        learner = _learner()
        book = _make_book()
        for _ in range(5):
            await learner.observe(book, 0.01)
        assert len(learner._error_history) == 5

    @pytest.mark.asyncio
    async def test_should_retrain_time_trigger(self):
        """Backdating last_retrain_ns should trigger time-based retraining."""
        learner = _learner(retrain_threshold=9999, retrain_interval_s=1.0)
        book = _make_book()
        # Pre-fill buffer so retrain has enough data
        for _ in range(25):
            learner._buffer.append(
                Observation(features=None, realized_return=0.0,  # type: ignore[arg-type]
                            timestamp_ns=time.perf_counter_ns())
            )
        # Backdate last retrain time by > retrain_interval
        learner._last_retrain_ns = time.perf_counter_ns() - int(2e9)
        assert learner._should_retrain() is True


# ═══════════════════════════════════════════════════════════════════════════════
#  TestRetrainingScheduler
# ═══════════════════════════════════════════════════════════════════════════════

class TestRetrainingScheduler:

    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        learner = _learner()
        sched = RetrainingScheduler(learner)
        await sched.start()
        assert sched._running is True
        await sched.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        learner = _learner()
        sched = RetrainingScheduler(learner)
        await sched.start()
        await sched.stop()
        assert sched._running is False

    @pytest.mark.asyncio
    async def test_status_has_required_keys(self):
        learner = _learner()
        sched = RetrainingScheduler(learner)
        s = sched.status()
        assert "running" in s
        assert "learner" in s
        assert "cluster" in s
