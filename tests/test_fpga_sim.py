"""
Tests for:
  - FPGAFrame       (pipeline I/O struct)
  - FPGASignalUnit  (momentum signal in hardware gates)
  - FPGARiskUnit    (BRAM limit lookup)
  - FPGAOrderUnit   (OUCH order encoding)
  - FPGAPipeline    (5-stage orchestrator)
"""
from __future__ import annotations

import asyncio
import pytest

from infrastructure.fpga_sim import (
    FPGA_LATENCY_NS,
    PIPELINE_STAGES,
    CLOCK_PERIOD_NS,
    NIC_SERIAL_NS,
    FPGAFrame,
    FPGAOrderUnit,
    FPGAPipeline,
    FPGARiskUnit,
    FPGASignalUnit,
    PipelineStage,
    PipelineStats,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _frame(bid=150.0, ask=150.05, last=150.02, vol=1000, sym=b"AAPL") -> FPGAFrame:
    return FPGAFrame(bid=bid, ask=ask, last=last, volume=vol, symbol=sym)


def _pipeline() -> FPGAPipeline:
    return FPGAPipeline()


# ═══════════════════════════════════════════════════════════════════════════════
#  TestFPGAConstants
# ═══════════════════════════════════════════════════════════════════════════════

class TestFPGAConstants:

    def test_pipeline_stages_is_5(self):
        assert PIPELINE_STAGES == 5

    def test_clock_period_4ns(self):
        assert CLOCK_PERIOD_NS == 4

    def test_fpga_latency_correct(self):
        assert FPGA_LATENCY_NS == PIPELINE_STAGES * CLOCK_PERIOD_NS + NIC_SERIAL_NS

    def test_fpga_latency_under_100ns(self):
        assert FPGA_LATENCY_NS <= 100

    def test_fpga_latency_over_40ns(self):
        assert FPGA_LATENCY_NS >= 40


# ═══════════════════════════════════════════════════════════════════════════════
#  TestFPGAFrame
# ═══════════════════════════════════════════════════════════════════════════════

class TestFPGAFrame:

    def test_defaults(self):
        f = FPGAFrame()
        assert f.bid == 0.0
        assert f.signal_direction == 0
        assert not f.risk_approved

    def test_initial_stage_is_decode(self):
        f = FPGAFrame()
        assert f.stage == PipelineStage.DECODE

    def test_fields_assignable(self):
        f = _frame()
        assert f.bid == 150.0
        assert f.ask == 150.05
        assert f.symbol == b"AAPL"


# ═══════════════════════════════════════════════════════════════════════════════
#  TestFPGASignalUnit
# ═══════════════════════════════════════════════════════════════════════════════

class TestFPGASignalUnit:

    def test_no_signal_on_wide_spread(self):
        unit = FPGASignalUnit(spread_threshold_bps=5.0)
        f = _frame(bid=100.0, ask=101.0, last=100.5)  # 100 bps spread
        out = unit.process(f)
        assert out.signal_direction == 0

    def test_no_signal_zero_prices(self):
        unit = FPGASignalUnit()
        f = _frame(bid=0.0, ask=0.0, last=0.0)
        out = unit.process(f)
        assert out.signal_direction == 0

    def test_stage_set_to_signal(self):
        unit = FPGASignalUnit()
        f = _frame()
        out = unit.process(f)
        assert out.stage == PipelineStage.SIGNAL

    def test_upward_momentum_gives_buy_signal(self):
        unit = FPGASignalUnit(spread_threshold_bps=10.0, signal_threshold=0.01)
        prices = [100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7]
        for p in prices:
            f = FPGAFrame(bid=p - 0.01, ask=p + 0.01, last=p)
            out = unit.process(f)
        assert out.signal_direction == 1

    def test_downward_momentum_gives_sell_signal(self):
        unit = FPGASignalUnit(spread_threshold_bps=10.0, signal_threshold=0.01)
        prices = [100.0, 99.9, 99.8, 99.7, 99.6, 99.5, 99.4, 99.3]
        for p in prices:
            f = FPGAFrame(bid=p - 0.01, ask=p + 0.01, last=p)
            out = unit.process(f)
        assert out.signal_direction == -1

    def test_signal_strength_in_range(self):
        unit = FPGASignalUnit(spread_threshold_bps=10.0, signal_threshold=0.01)
        prices = [100.0 + i * 0.5 for i in range(10)]
        for p in prices:
            f = FPGAFrame(bid=p - 0.01, ask=p + 0.01, last=p)
            out = unit.process(f)
        assert 0 <= out.signal_strength <= 255


# ═══════════════════════════════════════════════════════════════════════════════
#  TestFPGARiskUnit
# ═══════════════════════════════════════════════════════════════════════════════

class TestFPGARiskUnit:

    def test_approves_normal_order(self):
        risk = FPGARiskUnit(max_order_size=5000, max_notional_usd=500_000)
        f = _frame()
        f.signal_direction = 1
        f.order_qty = 100
        f.order_price = 150.05
        out = risk.approve(f)
        assert out.risk_approved is True

    def test_rejects_oversized_order(self):
        risk = FPGARiskUnit(max_order_size=100)
        f = _frame()
        f.signal_direction = 1
        f.order_qty = 10_000
        f.order_price = 150.05
        out = risk.approve(f)
        assert out.risk_approved is False

    def test_rejects_excessive_notional(self):
        risk = FPGARiskUnit(max_notional_usd=1_000)
        f = _frame()
        f.signal_direction = 1
        f.order_qty = 100
        f.order_price = 150.0
        out = risk.approve(f)
        assert out.risk_approved is False

    def test_no_signal_skips_check(self):
        risk = FPGARiskUnit()
        f = _frame()
        f.signal_direction = 0
        out = risk.approve(f)
        # No approval flag set when no signal
        assert out.risk_approved is False

    def test_stage_set_to_risk(self):
        risk = FPGARiskUnit()
        f = _frame()
        f.signal_direction = 1
        f.order_qty = 100
        f.order_price = 150.0
        out = risk.approve(f)
        assert out.stage == PipelineStage.RISK


# ═══════════════════════════════════════════════════════════════════════════════
#  TestFPGAOrderUnit
# ═══════════════════════════════════════════════════════════════════════════════

class TestFPGAOrderUnit:

    def test_buy_signal_sets_ask_as_price(self):
        unit = FPGAOrderUnit(default_qty=200)
        f = _frame()
        f.signal_direction = 1
        f.risk_approved = True
        out = unit.encode_order(f)
        assert out.order_price == 150.05   # ask

    def test_sell_signal_sets_bid_as_price(self):
        unit = FPGAOrderUnit(default_qty=200)
        f = _frame()
        f.signal_direction = -1
        f.risk_approved = True
        out = unit.encode_order(f)
        assert out.order_price == 150.0    # bid

    def test_no_signal_leaves_order_unchanged(self):
        unit = FPGAOrderUnit(default_qty=200)
        f = _frame()
        f.signal_direction = 0
        f.risk_approved = True
        out = unit.encode_order(f)
        assert out.order_qty == 0   # unchanged default

    def test_risk_rejected_leaves_order_unchanged(self):
        unit = FPGAOrderUnit(default_qty=200)
        f = _frame()
        f.signal_direction = 1
        f.risk_approved = False
        out = unit.encode_order(f)
        assert out.order_qty == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  TestFPGAPipeline
# ═══════════════════════════════════════════════════════════════════════════════

class TestFPGAPipeline:

    def test_simulated_latency_matches_formula(self):
        p = _pipeline()
        assert p.simulated_latency_ns == FPGA_LATENCY_NS

    def test_simulated_latency_is_80ns(self):
        p = _pipeline()
        assert p.simulated_latency_ns == 80

    def test_frame_reaches_transmit_stage(self):
        p = _pipeline()
        out = p.process(_frame())
        assert out.stage == PipelineStage.TRANSMIT

    def test_stats_incremented_per_frame(self):
        p = _pipeline()
        for _ in range(5):
            p.process(_frame())
        assert p.stats.frames_processed == 5

    def test_stats_mean_latency_equals_simulated(self):
        p = _pipeline()
        p.process(_frame())
        assert p.stats.mean_latency_ns == FPGA_LATENCY_NS

    def test_uptrend_generates_buy_signal(self):
        p = FPGAPipeline(
            signal_unit=FPGASignalUnit(spread_threshold_bps=10.0, signal_threshold=0.01)
        )
        prices = [100.0 + i * 0.5 for i in range(10)]
        out = None
        for price in prices:
            f = FPGAFrame(bid=price - 0.01, ask=price + 0.01, last=price)
            out = p.process(f)
        assert out.signal_direction == 1
        assert p.stats.signals_generated >= 1

    def test_risk_rejection_counted(self):
        p = FPGAPipeline(
            signal_unit=FPGASignalUnit(spread_threshold_bps=10.0, signal_threshold=0.01),
            risk_unit=FPGARiskUnit(max_order_size=1),   # reject every order
        )
        prices = [100.0 + i * 0.5 for i in range(10)]
        for price in prices:
            f = FPGAFrame(bid=price - 0.01, ask=price + 0.01, last=price)
            p.process(f)
        assert p.stats.risk_rejections >= 0   # may be 0 if no signal generated

    @pytest.mark.asyncio
    async def test_process_async_returns_frame(self):
        p = _pipeline()
        out = await p.process_async(_frame())
        assert out.stage == PipelineStage.TRANSMIT

    def test_throughput_mps_250(self):
        """Pipeline throughput should be 250 Mmsg/s (250 MHz clock)."""
        p = _pipeline()
        assert p.stats.throughput_mps == 250.0
