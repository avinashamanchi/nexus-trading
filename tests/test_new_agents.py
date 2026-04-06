"""
Tests for Agents 17 (GCA), 18 (SMRE), 19 (SHA), §5.6 session state machine,
and §8.3 kill switch.
"""
from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from core.enums import (
    HealthDimension,
    HealthSeverity,
    SessionState,
    Topic,
)
from infrastructure.session_state_machine import (
    SessionStateMachine,
    SessionTransitionError,
)


# ── §5.6 Session State Machine ────────────────────────────────────────────────

class TestSessionStateMachine:
    """Validate the §5.6 formal 9-state session state machine."""

    def _make_fsm(self) -> SessionStateMachine:
        return SessionStateMachine()

    @pytest.mark.asyncio
    async def test_initial_state_is_booting(self):
        fsm = self._make_fsm()
        assert fsm.state == SessionState.BOOTING

    @pytest.mark.asyncio
    async def test_boot_complete_advances_to_waiting_data(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        assert fsm.state == SessionState.WAITING_FOR_DATA_INTEGRITY

    @pytest.mark.asyncio
    async def test_full_happy_path_boot_to_active(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.data_integrity_cleared()
        await fsm.regime_ready()
        assert fsm.state == SessionState.ACTIVE
        assert fsm.is_trading_permitted()

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self):
        fsm = self._make_fsm()
        # Cannot go directly from BOOTING to ACTIVE
        with pytest.raises(SessionTransitionError):
            await fsm.transition_to(SessionState.ACTIVE, "test")

    @pytest.mark.asyncio
    async def test_halt_risk_from_active(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.data_integrity_cleared()
        await fsm.regime_ready()
        await fsm.halt_risk("Daily loss cap reached")
        assert fsm.state == SessionState.HALTED_RISK
        assert not fsm.is_trading_permitted()
        assert fsm.is_halted()

    @pytest.mark.asyncio
    async def test_halt_anomaly_from_waiting_data(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.halt_anomaly("Feed anomaly detected")
        assert fsm.state == SessionState.HALTED_ANOMALY

    @pytest.mark.asyncio
    async def test_halt_reconciliation_from_active(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.data_integrity_cleared()
        await fsm.regime_ready()
        await fsm.halt_reconciliation("Position mismatch")
        assert fsm.state == SessionState.HALTED_RECONCILIATION

    @pytest.mark.asyncio
    async def test_human_resume_from_halted_risk(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.data_integrity_cleared()
        await fsm.regime_ready()
        await fsm.halt_risk("Test halt")
        await fsm.human_resume("trader@firm.com")
        assert fsm.state == SessionState.ACTIVE
        assert fsm.is_trading_permitted()

    @pytest.mark.asyncio
    async def test_human_resume_requires_approver(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.data_integrity_cleared()
        await fsm.regime_ready()
        await fsm.halt_risk("Test halt")
        with pytest.raises(SessionTransitionError):
            await fsm.transition_to(SessionState.ACTIVE, "no approver")

    @pytest.mark.asyncio
    async def test_shutdown_is_terminal(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.shutdown("Test shutdown")
        assert fsm.state == SessionState.SHUTDOWN
        with pytest.raises(SessionTransitionError):
            await fsm.transition_to(SessionState.BOOTING, "restart?")

    @pytest.mark.asyncio
    async def test_idempotent_same_state_returns_false(self):
        fsm = self._make_fsm()
        result = await fsm.transition_to(SessionState.BOOTING, "no-op")
        assert result is False

    @pytest.mark.asyncio
    async def test_pause_cooldown_from_active(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.data_integrity_cleared()
        await fsm.regime_ready()
        # patch _auto_resume to avoid asyncio.sleep
        fsm._auto_resume = AsyncMock()
        await fsm.transition_to(SessionState.PAUSED_COOLDOWN, "circuit breaker")
        assert fsm.state == SessionState.PAUSED_COOLDOWN
        assert fsm.is_paused()
        assert not fsm.is_trading_permitted()

    @pytest.mark.asyncio
    async def test_on_transition_callback_fires(self):
        calls = []

        async def cb(from_s, to_s, reason):
            calls.append((from_s, to_s))

        fsm = SessionStateMachine(on_transition=cb)
        await fsm.boot_complete()
        assert len(calls) == 1
        assert calls[0] == (SessionState.BOOTING, SessionState.WAITING_FOR_DATA_INTEGRITY)

    @pytest.mark.asyncio
    async def test_history_records_transitions(self):
        fsm = self._make_fsm()
        await fsm.boot_complete()
        await fsm.data_integrity_cleared()
        assert len(fsm.history) == 2
        assert fsm.history[0]["from"] == "booting"
        assert fsm.history[1]["from"] == "waiting_for_data_integrity"

    @pytest.mark.asyncio
    async def test_to_dict(self):
        fsm = self._make_fsm()
        d = fsm.to_dict()
        assert d["state"] == "booting"
        assert "is_trading_permitted" in d
        assert d["is_trading_permitted"] is False


# ── Agent 17: Global Clock Agent ──────────────────────────────────────────────

class TestGlobalClockAgent:

    def _make_gca(self):
        from agents.agent_17_global_clock import GlobalClockAgent
        bus = MagicMock()
        bus.publish_raw = AsyncMock()
        bus.subscribe = MagicMock()
        bus.start_consuming = AsyncMock()
        store = MagicMock()
        store.save_heartbeat = AsyncMock()
        audit = MagicMock()
        return GlobalClockAgent(bus, store, audit, tick_interval_ms=100, stall_threshold_sec=2.0)

    def test_now_returns_datetime(self):
        gca = self._make_gca()
        t = gca.now()
        assert isinstance(t, datetime)
        assert (datetime.utcnow() - t).total_seconds() < 1.0

    def test_register_timer(self):
        gca = self._make_gca()
        expires = datetime.utcnow() + timedelta(seconds=5)
        gca.register_timer("test_timer", expires, {"key": "val"})
        assert "test_timer" in gca._timers

    def test_cancel_timer_removes_it(self):
        gca = self._make_gca()
        expires = datetime.utcnow() + timedelta(seconds=5)
        gca.register_timer("test_timer", expires)
        result = gca.cancel_timer("test_timer")
        assert result is True
        assert "test_timer" not in gca._timers

    def test_cancel_nonexistent_timer_returns_false(self):
        gca = self._make_gca()
        assert gca.cancel_timer("nonexistent") is False

    def test_time_until_future_timer(self):
        gca = self._make_gca()
        expires = datetime.utcnow() + timedelta(seconds=10)
        gca.register_timer("future", expires)
        remaining = gca.time_until("future")
        assert remaining is not None
        assert 8.0 <= remaining <= 11.0

    def test_time_until_unknown_timer_is_none(self):
        gca = self._make_gca()
        assert gca.time_until("unknown") is None

    def test_record_heartbeat(self):
        gca = self._make_gca()
        gca.record_heartbeat("agent_foo")
        assert "agent_foo" in gca._last_heartbeat

    @pytest.mark.asyncio
    async def test_expired_timer_fires_on_tick(self):
        gca = self._make_gca()
        # Register an already-expired timer
        already_expired = datetime.utcnow() - timedelta(seconds=1)
        gca.register_timer("expired_timer", already_expired, {"info": "test"})
        now = gca.now()
        await gca._fire_expired_timers(now)
        # Timer should be removed after firing
        assert "expired_timer" not in gca._timers
        gca.bus.publish_raw.assert_called_once()

    @pytest.mark.asyncio
    async def test_stall_detection_publishes_alert(self):
        gca = self._make_gca()
        # Inject a stale heartbeat
        gca._last_heartbeat["stale_agent"] = datetime.utcnow() - timedelta(seconds=5)
        now = gca.now()
        await gca._check_stalled_agents(now)
        # Should have published a STARVATION_ALERT
        gca.bus.publish_raw.assert_called_once()
        call_kwargs = gca.bus.publish_raw.call_args.kwargs
        assert call_kwargs["topic"] == Topic.STARVATION_ALERT

    @pytest.mark.asyncio
    async def test_repeating_timer_reschedules(self):
        gca = self._make_gca()
        original_expiry = datetime.utcnow() - timedelta(milliseconds=100)
        gca.register_timer("repeating", original_expiry, {}, repeat_sec=5.0)
        now = gca.now()
        await gca._fire_expired_timers(now)
        # Timer should still exist (rescheduled)
        assert "repeating" in gca._timers
        # New expiry should be original + 5s
        new_expiry = gca._timers["repeating"].expires_at
        assert (new_expiry - original_expiry).total_seconds() == pytest.approx(5.0, abs=0.1)


# ── Agent 18: Shadow Mode Replay Engine ───────────────────────────────────────

class TestShadowReplayAgent:

    @pytest_asyncio.fixture
    async def smre(self, tmp_path):
        from agents.agent_18_shadow_replay import ShadowReplayAgent
        bus = MagicMock()
        bus.publish_raw = AsyncMock()
        bus.subscribe = MagicMock()
        bus.start_consuming = AsyncMock()
        store = MagicMock()
        audit = MagicMock()
        audit.record = AsyncMock()
        agent = ShadowReplayAgent(
            bus=bus,
            store=store,
            audit=audit,
            db_path=tmp_path / "replay.db",
            jsonl_dir=tmp_path / "sessions",
        )
        await agent.initialize()
        yield agent
        await agent.stop()

    @pytest.mark.asyncio
    async def test_initialize_creates_db(self, smre, tmp_path):
        assert (tmp_path / "replay.db").exists()

    @pytest.mark.asyncio
    async def test_record_event_stores_in_db(self, smre):
        from core.models import BusMessage
        msg = BusMessage(
            topic=Topic.TRADE_CLOSED,
            source_agent="test",
            payload={"net_pnl": 100.0, "symbol": "AAPL"},
        )
        await smre._record_event(msg)
        count = await smre.get_recorded_event_count(smre._current_session_date)
        assert count == 1

    @pytest.mark.asyncio
    async def test_checksum_verification_passes_clean(self, smre):
        from core.models import BusMessage
        msg = BusMessage(
            topic=Topic.TRADE_CLOSED,
            source_agent="test",
            payload={"net_pnl": 50.0},
        )
        await smre._record_event(msg)
        events = await smre._load_session_events(smre._current_session_date)
        # Should not raise
        smre._verify_checksums(events)

    @pytest.mark.asyncio
    async def test_checksum_tamper_detected(self, smre):
        from agents.agent_18_shadow_replay import _event_checksum
        import json as _json
        from core.models import BusMessage
        msg = BusMessage(
            topic=Topic.TRADE_CLOSED,
            source_agent="test",
            payload={"net_pnl": 50.0},
        )
        await smre._record_event(msg)
        events = await smre._load_session_events(smre._current_session_date)
        # Tamper with the checksum
        events[0] = dict(events[0])
        events[0]["checksum"] = "deadbeef" * 8
        with pytest.raises(ValueError, match="Checksum mismatch"):
            smre._verify_checksums(events)

    @pytest.mark.asyncio
    async def test_replay_with_no_events_raises(self, smre):
        with pytest.raises(ValueError, match="No events"):
            await smre._run_replay("2099-01-01", "baseline", {})

    @pytest.mark.asyncio
    async def test_baseline_replay_returns_report(self, smre):
        from core.models import BusMessage
        # Record a trade close event
        msg = BusMessage(
            topic=Topic.TRADE_CLOSED,
            source_agent="spa",
            payload={"net_pnl": 200.0, "symbol": "AAPL", "exit_mode": "profit_target"},
        )
        await smre._record_event(msg)
        report = await smre._run_replay(smre._current_session_date, "baseline", {})
        assert report.baseline["total_trades"] == 1
        assert report.baseline["total_pnl"] == pytest.approx(200.0)

    @pytest.mark.asyncio
    async def test_jsonl_written_on_record(self, smre, tmp_path):
        from core.models import BusMessage
        msg = BusMessage(
            topic=Topic.FILL_EVENT,
            source_agent="execution",
            payload={"symbol": "TSLA", "fill_price": 300.0},
        )
        await smre._record_event(msg)
        jsonl_file = tmp_path / "sessions" / f"{smre._current_session_date}.jsonl"
        assert jsonl_file.exists()
        lines = jsonl_file.read_text().strip().split("\n")
        assert len(lines) == 1


# ── Agent 19: System Health Agent ─────────────────────────────────────────────

class TestSystemHealthAgent:

    def _make_sha(self, thresholds=None, ws_fn=None):
        from agents.agent_19_system_health import SystemHealthAgent
        bus = MagicMock()
        bus.publish_raw = AsyncMock()
        bus.subscribe = MagicMock()
        bus.start_consuming = AsyncMock()
        store = MagicMock()
        store.load_open_positions = AsyncMock(return_value=[])
        audit = MagicMock()
        audit.record = AsyncMock()
        return SystemHealthAgent(
            bus=bus,
            store=store,
            audit=audit,
            poll_interval_sec=1.0,
            thresholds=thresholds,
            websocket_status_fn=ws_fn,
        )

    def test_initial_severity_is_ok(self):
        sha = self._make_sha()
        assert sha.overall_severity == HealthSeverity.OK

    def test_get_metric_returns_ok_initially(self):
        sha = self._make_sha()
        result = sha.get_metric(HealthDimension.CPU)
        assert result["severity"] == HealthSeverity.OK.value
        assert result["value"] == 0.0

    @pytest.mark.asyncio
    async def test_warning_threshold_triggers_psa_alert(self):
        from agents.agent_19_system_health import _MetricState
        sha = self._make_sha()

        # Inject a high CPU average
        sha._metrics[HealthDimension.CPU.value]._samples.extend([75.0, 75.0, 75.0])
        sha._metrics[HealthDimension.CPU.value].current = 75.0
        sha._metrics[HealthDimension.CPU.value].severity = HealthSeverity.WARNING

        snapshot = {
            "overall_severity": HealthSeverity.WARNING.value,
            "dimensions": {
                HealthDimension.CPU.value: {
                    "value": 75.0,
                    "unit": "%",
                    "severity": HealthSeverity.WARNING.value,
                }
            },
        }
        await sha._escalate_if_needed(snapshot)
        sha.bus.publish_raw.assert_called_once()
        call_kwargs = sha.bus.publish_raw.call_args.kwargs
        assert call_kwargs["topic"] == Topic.PSA_ALERT

    @pytest.mark.asyncio
    async def test_critical_threshold_triggers_halt(self):
        sha = self._make_sha()
        snapshot = {
            "overall_severity": HealthSeverity.CRITICAL.value,
            "dimensions": {
                HealthDimension.EVENT_LOOP_LAG.value: {
                    "value": 250.0,
                    "unit": "ms",
                    "severity": HealthSeverity.CRITICAL.value,
                }
            },
        }
        await sha._escalate_if_needed(snapshot)
        # Should publish HALT_COMMAND and record audit event
        halt_calls = [
            c for c in sha.bus.publish_raw.call_args_list
            if c.kwargs.get("topic") == Topic.HALT_COMMAND
        ]
        assert len(halt_calls) == 1
        sha.audit.record.assert_called_once()

    @pytest.mark.asyncio
    async def test_ok_severity_no_alerts(self):
        sha = self._make_sha()
        snapshot = {
            "overall_severity": HealthSeverity.OK.value,
            "dimensions": {
                HealthDimension.CPU.value: {
                    "value": 10.0, "unit": "%", "severity": HealthSeverity.OK.value
                }
            },
        }
        await sha._escalate_if_needed(snapshot)
        sha.bus.publish_raw.assert_not_called()

    @pytest.mark.asyncio
    async def test_metric_smoothing(self):
        from agents.agent_19_system_health import _MetricState
        m = _MetricState(warning=70.0, critical=90.0)
        m.update(50.0)
        m.update(80.0)
        # Average of 2 samples: 65 — below warning
        assert m.severity == HealthSeverity.OK

        m.update(80.0)
        # Average of 3 samples: 70 — at warning
        assert m.severity == HealthSeverity.WARNING

    @pytest.mark.asyncio
    async def test_websocket_down_is_critical(self):
        sha = self._make_sha(ws_fn=lambda: False)
        snapshot = await sha._collect_metrics()
        ws_dim = snapshot["dimensions"][HealthDimension.WEBSOCKET.value]
        assert ws_dim["severity"] == HealthSeverity.CRITICAL.value

    @pytest.mark.asyncio
    async def test_event_loop_lag_measurement(self):
        sha = self._make_sha()
        lag_ms = await sha._measure_event_loop_lag()
        # Should be a non-negative float (very small in test env)
        assert lag_ms >= 0.0
        assert lag_ms < 1000.0  # sanity: < 1 second


# ── §8.3 Kill Switch ──────────────────────────────────────────────────────────

class TestKillSwitch:

    def _make_ks(self, mfa_secret=""):
        from infrastructure.kill_switch import KillSwitch
        broker = MagicMock()
        broker.cancel_all_orders = AsyncMock(return_value=3)
        broker.close_all_positions = AsyncMock(return_value=2)
        audit = MagicMock()
        audit.record = AsyncMock()
        bus = MagicMock()
        bus.publish_raw = AsyncMock()
        return KillSwitch(broker=broker, audit=audit, bus=bus, mfa_secret=mfa_secret)

    @pytest.mark.asyncio
    async def test_trigger_without_mfa_succeeds_when_no_secret(self):
        ks = self._make_ks(mfa_secret="")
        result = await ks.trigger("trader", mfa_token="", reason="test")
        assert result["success"] is True
        assert result["trigger_id"] is not None

    @pytest.mark.asyncio
    async def test_trigger_is_idempotent(self):
        ks = self._make_ks()
        result1 = await ks.trigger("trader", "", "first")
        result2 = await ks.trigger("trader", "", "second")
        # Second should return current status (same trigger_id)
        assert result1["trigger_id"] == result2.get("trigger_id")

    @pytest.mark.asyncio
    async def test_trigger_bad_mfa_fails(self):
        ks = self._make_ks(mfa_secret="mysecret")
        result = await ks.trigger("trader", mfa_token="badtoken", reason="test")
        assert result["success"] is False
        assert "MFA" in result["error"]

    @pytest.mark.asyncio
    async def test_flatten_calls_broker(self):
        ks = self._make_ks()
        await ks.trigger("trader", "", "test")
        # Allow background flatten to run
        await asyncio.sleep(0.1)
        ks._broker.cancel_all_orders.assert_called_once()
        ks._broker.close_all_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_status_returns_dict(self):
        ks = self._make_ks()
        status = ks.status()
        assert "state" in status
        assert status["state"] == "armed"

    @pytest.mark.asyncio
    async def test_acknowledge_after_flatten(self):
        ks = self._make_ks()
        await ks.trigger("trader", "", "test")
        await asyncio.sleep(0.1)  # let flatten complete
        result = await ks.acknowledge("operator", "")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_restart_requires_acknowledged_state(self):
        ks = self._make_ks()
        # Not triggered yet — should fail
        result = await ks.restart_request("operator", "")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_audit_event_recorded_on_trigger(self):
        ks = self._make_ks()
        await ks.trigger("trader", "", "test")
        ks._audit.record.assert_called()
        event = ks._audit.record.call_args.args[0]
        assert event.event_type == "KILL_SWITCH_TRIGGERED"
