"""
Tests for TERA (Agent 6) risk rule enforcement.
These are the most critical safety tests in the system.
All 10 risk rules must be independently verified.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from core.enums import (
    Direction,
    RegimeLabel,
    RiskDecision,
    RiskRejectReason,
    SetupType,
)
from core.models import (
    Position,
    WashSaleFlag,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_position(symbol: str = "AAPL") -> Position:
    return Position(
        plan_id="plan-001",
        symbol=symbol,
        direction=Direction.LONG,
        shares=100,
        entry_price=150.0,
        current_price=150.5,
        stop_price=149.0,
        target_price=152.0,
        time_stop_at=datetime.utcnow() + timedelta(minutes=5),
    )


# ── TERA instantiation helper ─────────────────────────────────────────────────

def _make_tera(
    equity: float = 30_000.0,
    daily_pnl: float = 0.0,
    positions: list | None = None,
    wash_sale: WashSaleFlag | None = None,
):
    """Return a TERAAgent with mocked bus/store/audit and state pre-set.

    NOTE: account_equity_fn, get_wash_sale_fn, and open_positions_fn are all
    called synchronously inside _evaluate_all_rules — use MagicMock, not AsyncMock.
    """
    from agents.agent_06_tera import TERAAgent
    from infrastructure.message_bus import MessageBus
    from infrastructure.state_store import StateStore
    from infrastructure.audit_log import AuditLog
    from unittest.mock import AsyncMock

    bus = MagicMock(spec=MessageBus)
    bus.publish_raw = AsyncMock()
    bus.subscribe = MagicMock()

    store = MagicMock(spec=StateStore)

    audit = MagicMock(spec=AuditLog)
    audit.record = AsyncMock()

    pos_list = positions or []
    agent = TERAAgent(
        bus=bus,
        store=store,
        audit=audit,
        account_equity_fn=MagicMock(return_value=equity),
        get_wash_sale_fn=MagicMock(return_value=wash_sale),
        open_positions_fn=MagicMock(return_value=pos_list),
    )
    agent._daily_pnl = daily_pnl
    return agent


# ── Rule 1: Daily loss cap ─────────────────────────────────────────────────────

class TestDailyLossCap:
    def test_blocked_at_loss_cap(self):
        # 2% of 30k = $600; PnL = -600 → should block
        agent = _make_tera(equity=30_000.0, daily_pnl=-600.0)
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.DAILY_LOSS_CAP

    def test_allowed_below_cap(self):
        agent = _make_tera(equity=30_000.0, daily_pnl=-300.0)
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        # Should pass daily loss cap (still other rules may fire but not this one)
        assert reason != RiskRejectReason.DAILY_LOSS_CAP

    def test_exact_boundary_blocks(self):
        # Exactly at -2% → should block (<=)
        agent = _make_tera(equity=30_000.0, daily_pnl=-600.0)
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.DAILY_LOSS_CAP

    def test_one_cent_below_cap_allows(self):
        agent = _make_tera(equity=30_000.0, daily_pnl=-599.99)
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert reason != RiskRejectReason.DAILY_LOSS_CAP


# ── Rule 2: Drawdown velocity ─────────────────────────────────────────────────

class TestDrawdownVelocity:
    def test_pause_triggered_on_velocity_breach(self):
        """$300+ loss in 3 minutes → mandatory pause."""
        agent = _make_tera(equity=30_000.0)
        now = datetime.utcnow()
        # Inject 3 recent losses totalling $350 within 180s (exceeds $300 threshold)
        agent._loss_timestamps.extend([
            (now - timedelta(seconds=30), 150.0),
            (now - timedelta(seconds=60), 100.0),
            (now - timedelta(seconds=90), 100.0),
        ])
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.DRAWDOWN_VELOCITY

    def test_no_pause_below_threshold(self):
        agent = _make_tera(equity=30_000.0)
        now = datetime.utcnow()
        agent._loss_timestamps.extend([
            (now - timedelta(seconds=30), 100.0),
            (now - timedelta(seconds=60), 50.0),
        ])
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert reason != RiskRejectReason.DRAWDOWN_VELOCITY

    def test_stale_losses_outside_window_are_excluded(self):
        """Losses older than velocity window should not count."""
        agent = _make_tera(equity=30_000.0)
        now = datetime.utcnow()
        # 400 loss but all outside the 180s window
        agent._loss_timestamps.extend([
            (now - timedelta(seconds=200), 400.0),
        ])
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert reason != RiskRejectReason.DRAWDOWN_VELOCITY

    def test_active_pause_blocks(self):
        """Once pause is set, subsequent calls are still blocked."""
        agent = _make_tera(equity=30_000.0)
        agent._drawdown_velocity_pause_until = datetime.utcnow() + timedelta(seconds=200)
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.DRAWDOWN_VELOCITY


# ── Rule 3: Max concurrent trades ────────────────────────────────────────────

class TestMaxConcurrentTrades:
    def test_blocked_at_max(self):
        positions = [_make_position(f"TICK{i}") for i in range(5)]  # default max is 5
        agent = _make_tera(positions=positions)
        decision, reason, _ = agent._evaluate_all_rules("NEWTICK", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.MAX_CONCURRENT_TRADES

    def test_allowed_below_max(self):
        positions = [_make_position(f"TICK{i}") for i in range(3)]
        agent = _make_tera(positions=positions)
        decision, reason, _ = agent._evaluate_all_rules("NEWTICK", 30_000.0)
        assert reason != RiskRejectReason.MAX_CONCURRENT_TRADES

    def test_exactly_at_max_blocks(self):
        positions = [_make_position(f"TICK{i}") for i in range(5)]
        agent = _make_tera(positions=positions)
        decision, reason, _ = agent._evaluate_all_rules("NEWTICK", 30_000.0)
        assert decision == RiskDecision.REJECTED


# ── Rule 7: Consecutive losses cooldown ───────────────────────────────────────

class TestConsecutiveLosses:
    def test_blocked_at_consecutive_loss_limit(self):
        agent = _make_tera()
        agent._consecutive_losses = 3  # default cooldown trigger is 3
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.CONSECUTIVE_LOSSES

    def test_allowed_below_limit(self):
        agent = _make_tera()
        agent._consecutive_losses = 1
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert reason != RiskRejectReason.CONSECUTIVE_LOSSES

    def test_active_cooldown_blocks(self):
        agent = _make_tera()
        agent._consecutive_loss_cooldown_until = datetime.utcnow() + timedelta(seconds=200)
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.CONSECUTIVE_LOSSES


# ── Rule 8: Anomaly hold ──────────────────────────────────────────────────────

class TestAnomalyHold:
    def test_blocked_when_anomaly_hold(self):
        agent = _make_tera()
        agent._anomaly_hold = True
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.ANOMALY_HOLD

    def test_allowed_when_no_anomaly(self):
        agent = _make_tera()
        agent._anomaly_hold = False
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert reason != RiskRejectReason.ANOMALY_HOLD


# ── Rule 9: PDT limit ─────────────────────────────────────────────────────────

class TestPDTLimit:
    def test_blocked_when_pdt_limit_reached_under_equity(self):
        # Under PDT minimum equity ($25k) with 3 day trades used
        agent = _make_tera(equity=20_000.0)
        agent._pdt_trades_used = 3
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 20_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.PDT_LIMIT

    def test_allowed_above_pdt_equity(self):
        # Above $25k PDT minimum — PDT rule does not apply
        agent = _make_tera(equity=30_000.0)
        agent._pdt_trades_used = 3
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert reason != RiskRejectReason.PDT_LIMIT


# ── Rule 10: Wash sale ────────────────────────────────────────────────────────

class TestWashSaleBlock:
    def test_blocked_on_wash_sale_flag(self):
        flag = WashSaleFlag(
            symbol="AAPL",
            last_exit_at=datetime.utcnow() - timedelta(days=5),
            last_exit_price=148.0,
            net_loss=-500.0,
            wash_sale_window_end=datetime.utcnow() + timedelta(days=25),
        )
        agent = _make_tera(wash_sale=flag)
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.REJECTED
        assert reason == RiskRejectReason.WASH_SALE_FLAG

    def test_allowed_no_wash_sale(self):
        agent = _make_tera(wash_sale=None)
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert reason != RiskRejectReason.WASH_SALE_FLAG


# ── Rule priority order ───────────────────────────────────────────────────────

class TestRulePriority:
    def test_daily_loss_cap_beats_anomaly_hold(self):
        """Rule 1 (daily loss cap) should fire before Rule 8 (anomaly hold)."""
        agent = _make_tera(equity=30_000.0, daily_pnl=-600.0)
        agent._anomaly_hold = True
        decision, reason, _ = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert reason == RiskRejectReason.DAILY_LOSS_CAP

    def test_all_rules_pass_returns_approved(self):
        """Clean slate should result in APPROVED."""
        agent = _make_tera(equity=30_000.0, daily_pnl=0.0)
        decision, reason, detail = agent._evaluate_all_rules("AAPL", 30_000.0)
        assert decision == RiskDecision.APPROVED
        assert reason is None
        assert detail is None


# ── No-position-without-stop structural guarantee ────────────────────────────

class TestStructuralGuarantees:
    def test_trade_plan_requires_stop(self):
        """Model validator ensures TradePlan.stop_price > 0 — structural, not runtime."""
        from core.models import TradePlan, CostModel
        with pytest.raises(Exception):
            TradePlan(
                signal_id="s", symbol="X", direction=Direction.LONG,
                entry_price=10.0, stop_price=0.0, target_price=12.0,
                shares=100, time_stop_sec=300,
                invalidation_reason="test",
                cost_model=CostModel(),
                net_expectancy_per_share=0.05,
                account_equity_at_plan=30000.0,
                risk_per_share=1.0, total_risk=100.0, worst_case_slippage=0.05,
            )

    def test_trade_plan_requires_positive_expectancy(self):
        from core.models import TradePlan, CostModel
        with pytest.raises(Exception):
            TradePlan(
                signal_id="s", symbol="X", direction=Direction.LONG,
                entry_price=10.0, stop_price=9.0, target_price=12.0,
                shares=100, time_stop_sec=300,
                invalidation_reason="test",
                cost_model=CostModel(),
                net_expectancy_per_share=-0.01,
                account_equity_at_plan=30000.0,
                risk_per_share=1.0, total_risk=100.0, worst_case_slippage=0.05,
            )
