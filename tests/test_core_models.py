"""
Tests for core data models.
Validates: field constraints, computed properties, model validators.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta

from core.enums import Direction, ExitMode, RegimeLabel, SetupType
from core.models import (
    ApprovedSetup,
    ApprovedSetupList,
    CandidateSignal,
    CostModel,
    MarketTick,
    Position,
    TradePlan,
)


# ── MarketTick ────────────────────────────────────────────────────────────────

class TestMarketTick:
    def test_spread(self):
        tick = MarketTick(
            symbol="AAPL", timestamp=datetime.utcnow(),
            bid=150.00, ask=150.02, last=150.01, volume=1000,
        )
        assert abs(tick.spread - 0.02) < 1e-9

    def test_spread_bps(self):
        tick = MarketTick(
            symbol="AAPL", timestamp=datetime.utcnow(),
            bid=100.00, ask=100.10, last=100.05, volume=500,
        )
        # spread = 0.10, mid = 100.05 → bps ≈ 9.995
        assert 9 < tick.spread_bps < 11

    def test_mid(self):
        tick = MarketTick(
            symbol="TSLA", timestamp=datetime.utcnow(),
            bid=200.00, ask=200.04, last=200.02, volume=100,
        )
        assert abs(tick.mid - 200.02) < 1e-9


# ── ApprovedSetupList ─────────────────────────────────────────────────────────

class TestApprovedSetupList:
    def _make_setup(self, setup_type: SetupType, retired: bool = False) -> ApprovedSetup:
        return ApprovedSetup(
            setup_type=setup_type,
            expectancy_per_share=0.05,
            win_rate=0.55,
            avg_win=0.20,
            avg_loss=-0.12,
            sample_count=1200,
            out_of_sample_validated=True,
            allowed_regimes=[RegimeLabel.TREND_DAY],
            retired=retired,
        )

    def test_is_approved_active(self):
        lst = ApprovedSetupList(setups=[self._make_setup(SetupType.BREAKOUT)])
        assert lst.is_approved(SetupType.BREAKOUT)

    def test_is_approved_retired(self):
        lst = ApprovedSetupList(setups=[self._make_setup(SetupType.BREAKOUT, retired=True)])
        assert not lst.is_approved(SetupType.BREAKOUT)

    def test_get_setup_returns_active(self):
        lst = ApprovedSetupList(setups=[self._make_setup(SetupType.VWAP_TAP)])
        setup = lst.get_setup(SetupType.VWAP_TAP)
        assert setup is not None
        assert setup.setup_type == SetupType.VWAP_TAP

    def test_get_setup_returns_none_for_retired(self):
        lst = ApprovedSetupList(setups=[self._make_setup(SetupType.VWAP_TAP, retired=True)])
        assert lst.get_setup(SetupType.VWAP_TAP) is None


# ── TradePlan validation ──────────────────────────────────────────────────────

class TestTradePlan:
    def _base_plan(self, net_ev: float = 0.05, stop: float = 149.00) -> TradePlan:
        return TradePlan(
            signal_id="sig-001",
            symbol="AAPL",
            direction=Direction.LONG,
            entry_price=150.00,
            stop_price=stop,
            target_price=152.00,
            shares=100,
            time_stop_sec=300,
            invalidation_reason="price closes below VWAP",
            cost_model=CostModel(commission=0, sec_fee=0.01, finra_taf=0.02, estimated_slippage=0.01),
            net_expectancy_per_share=net_ev,
            account_equity_at_plan=30000.0,
            risk_per_share=1.00,
            total_risk=100.0,
            worst_case_slippage=0.05,
        )

    def test_valid_plan(self):
        plan = self._base_plan()
        assert plan.symbol == "AAPL"
        assert plan.net_expectancy_per_share > 0

    def test_negative_expectancy_rejected(self):
        with pytest.raises(ValueError, match="net_expectancy_per_share"):
            self._base_plan(net_ev=-0.01)

    def test_zero_expectancy_rejected(self):
        with pytest.raises(ValueError, match="net_expectancy_per_share"):
            self._base_plan(net_ev=0.0)

    def test_no_stop_rejected(self):
        with pytest.raises(ValueError, match="stop_price"):
            self._base_plan(stop=0.0)


# ── Position ──────────────────────────────────────────────────────────────────

class TestPosition:
    def _make_position(self, current: float = 152.0) -> Position:
        return Position(
            plan_id="plan-001",
            symbol="AAPL",
            direction=Direction.LONG,
            shares=100,
            entry_price=150.0,
            current_price=current,
            stop_price=149.0,
            target_price=152.0,
            time_stop_at=datetime.utcnow() + timedelta(minutes=5),
        )

    def test_unrealized_pnl_profit(self):
        pos = self._make_position(current=152.0)
        assert abs(pos.unrealized_pnl - 200.0) < 0.01   # (152-150)*100

    def test_unrealized_pnl_loss(self):
        pos = self._make_position(current=149.5)
        assert abs(pos.unrealized_pnl - (-50.0)) < 0.01  # (149.5-150)*100

    def test_unrealized_pnl_pct(self):
        pos = self._make_position(current=153.0)
        expected_pct = 300.0 / (150.0 * 100)  # 3/150 ≈ 2%
        assert abs(pos.unrealized_pnl_pct - expected_pct) < 1e-6


# ── CandidateSignal ───────────────────────────────────────────────────────────

class TestCandidateSignal:
    def test_risk_reward(self):
        tick = MarketTick(
            symbol="TSLA", timestamp=datetime.utcnow(),
            bid=199.98, ask=200.02, last=200.0, volume=5000,
        )
        sig = CandidateSignal(
            symbol="TSLA",
            setup_type=SetupType.BREAKOUT,
            direction=Direction.LONG,
            entry_price=200.0,
            initial_stop=198.0,   # 2-point risk
            initial_target=204.0, # 4-point reward
            regime=RegimeLabel.TREND_DAY,
            tick_at_signal=tick,
        )
        assert abs(sig.risk_reward - 2.0) < 1e-9  # 4/2 = 2.0

    def test_minimum_risk_reward_check(self):
        """Verify we can detect signals with insufficient R:R."""
        tick = MarketTick(
            symbol="TSLA", timestamp=datetime.utcnow(),
            bid=199.98, ask=200.02, last=200.0, volume=5000,
        )
        sig = CandidateSignal(
            symbol="TSLA",
            setup_type=SetupType.BREAKOUT,
            direction=Direction.LONG,
            entry_price=200.0,
            initial_stop=199.5,   # 0.5 risk
            initial_target=200.8, # 0.8 reward → 1.6:1 (below 2:1)
            regime=RegimeLabel.TREND_DAY,
            tick_at_signal=tick,
        )
        assert sig.risk_reward < 2.0  # SPA will reject this
