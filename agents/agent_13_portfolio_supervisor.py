"""
Agent 13 — Portfolio Supervisor Agent (PSA)

The final authority on all halt and restart decisions.
Owns the global system state machine.
Enforces: daily loss cap, drawdown velocity, circuit breaker (3 stopouts in 60s),
anomaly escalation, reconciliation lock, session restart permission.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from agents.base import BaseAgent
from core.enums import SessionState, SystemState, Topic
from core.models import AuditEvent, BusMessage, ClosedTrade, PortfolioState
from infrastructure.audit_log import (
    risk_breach_event,
    system_halt_event,
    system_resume_event,
)
from infrastructure.session_state_machine import SessionStateMachine, SessionTransitionError

logger = logging.getLogger(__name__)


class PortfolioSupervisorAgent(BaseAgent):
    """
    Agent 13: Portfolio Supervisor Agent (PSA).

    Global state machine owner. Has override authority over all agents.

    Circuit breakers:
    - Daily loss cap (hard stop)
    - Drawdown velocity ($300 in 3 min → pause)
    - 3 stopouts in 60s → 5–10 min system pause
    - Bus failure → safe mode
    - Any anomaly → hold, requires explicit clearance
    """

    def __init__(
        self,
        account_equity_fn: Callable[[], Awaitable[float]],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._account_equity_fn = account_equity_fn

        # P/L tracking
        self._daily_pnl: float = 0.0
        self._starting_equity: float = 0.0
        self._current_equity: float = 0.0

        # Legacy state machine (used by broker/audit layer)
        self._system_state = SystemState.INITIALIZING
        self._halt_reason: str | None = None
        self._anomaly_hold: bool = False
        self._pause_until: datetime | None = None

        # §5.6 Formal 9-state session state machine
        self._session_fsm = SessionStateMachine(
            on_transition=self._on_fsm_transition
        )

        # Circuit breaker: stopout timestamps (rolling window)
        self._stopout_times: deque[datetime] = deque(maxlen=20)
        self._loss_velocity_window: deque[tuple[datetime, float]] = deque(maxlen=100)

        # Open position count (tracked via messages)
        self._open_position_count: int = 0
        self._consecutive_losses: int = 0

        # PDT tracking
        self._pdt_trades_used: int = 0

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [
            Topic.TRADE_CLOSED,
            Topic.PSA_ALERT,
            Topic.RECONCILIATION_STATUS,
            Topic.FILL_EVENT,
            Topic.POSITION_UPDATE,
            Topic.DAILY_PNL,
            Topic.AGENT_HEARTBEAT,
            Topic.SYSTEM_STATE,
        ]

    async def on_startup(self) -> None:
        # Restore persisted state
        state, halt_reason, anomaly_hold = await self.store.load_system_state()
        self._system_state = state
        self._halt_reason = halt_reason
        self._anomaly_hold = anomaly_hold

        # Bootstrap equity
        try:
            self._starting_equity = self.cfg("capital", "starting_equity", default=30000.0)
            self._current_equity = await self._account_equity_fn()
        except Exception as exc:
            logger.warning("[PSA] Could not fetch account equity on startup: %s", exc)
            self._current_equity = self._starting_equity

        # Advance §5.6 FSM: BOOTING → WAITING_FOR_DATA_INTEGRITY
        await self._session_fsm.boot_complete()

        # Subscribe to SESSION_START/END from GCA
        self.bus.subscribe(Topic.SESSION_START, self._on_session_start)
        self.bus.subscribe(Topic.SESSION_END, self._on_session_end)
        # Also subscribe to KILL_SWITCH for emergency flatten
        self.bus.subscribe(Topic.KILL_SWITCH, self._on_kill_switch)

        logger.info(
            "[PSA] Startup complete. SystemState=%s SessionState=%s equity=$%.2f",
            self._system_state.value, self._session_fsm.state.value, self._current_equity,
        )
        await self._persist_state()

    async def _on_session_start(self, message: BusMessage) -> None:
        """GCA fires SESSION_START at 09:30 ET. Advance FSM to ACTIVE if ready."""
        try:
            if self._session_fsm.state == SessionState.WAITING_FOR_REGIME:
                await self._session_fsm.regime_ready()
            elif self._session_fsm.state == SessionState.WAITING_FOR_DATA_INTEGRITY:
                # Data not yet clean — stay in waiting state, log warning
                logger.warning("[PSA] SESSION_START but data integrity not yet cleared")
        except SessionTransitionError as exc:
            logger.warning("[PSA] SESSION_START FSM transition failed: %s", exc)

    async def _on_session_end(self, message: BusMessage) -> None:
        """GCA fires SESSION_END at 16:00 ET."""
        logger.info("[PSA] SESSION_END received — no new trades permitted")
        # Do not force shutdown; post-session work (PSRA) still runs

    async def _on_kill_switch(self, message: BusMessage) -> None:
        """Emergency flatten via §8.3 kill switch API."""
        reason = message.payload.get("reason", "kill_switch_triggered")
        initiated_by = message.payload.get("initiated_by", "unknown")
        logger.critical("[PSA] KILL SWITCH activated by %s: %s", initiated_by, reason)
        await self._halt(
            f"Kill switch: {reason} (by {initiated_by})",
            breach_type="kill_switch",
        )

    async def process(self, message: BusMessage) -> None:
        topic = message.topic

        if topic == Topic.TRADE_CLOSED:
            await self._handle_trade_closed(message)

        elif topic == Topic.PSA_ALERT:
            await self._handle_psa_alert(message)

        elif topic == Topic.RECONCILIATION_STATUS:
            await self._handle_reconciliation(message)

        elif topic == Topic.FILL_EVENT:
            self._open_position_count += 1

        elif topic == Topic.DAILY_PNL:
            payload = message.payload
            self._daily_pnl = payload.get("daily_pnl", self._daily_pnl)
            self._current_equity = payload.get("account_equity", self._current_equity)
            await self._check_daily_loss_cap()

        elif topic == Topic.SYSTEM_STATE:
            # HGL can send resume commands via this topic
            payload = message.payload
            command = payload.get("command")
            if command == "resume" and payload.get("approved_by"):
                await self._handle_resume(payload.get("approved_by", "unknown"))

    # ── Trade closed ──────────────────────────────────────────────────────────

    async def _handle_trade_closed(self, message: BusMessage) -> None:
        try:
            trade = ClosedTrade.model_validate(message.payload)
        except Exception:
            return

        self._open_position_count = max(0, self._open_position_count - 1)
        self._daily_pnl += trade.net_pnl

        # Track stopouts
        if not trade.is_winner:
            self._consecutive_losses += 1
            self._stopout_times.append(datetime.utcnow())
            self._loss_velocity_window.append((datetime.utcnow(), trade.net_pnl))
        else:
            self._consecutive_losses = 0

        # PDT counter
        self._pdt_trades_used += 1

        # Check circuit breakers
        await self._check_daily_loss_cap()
        await self._check_circuit_breaker()
        await self._check_pdt_limit()

        # Publish updated daily P/L
        await self.publish(
            Topic.DAILY_PNL,
            payload={
                "daily_pnl": self._daily_pnl,
                "account_equity": self._current_equity + self._daily_pnl,
                "trade_count": self._pdt_trades_used,
                "session_date": datetime.utcnow().strftime("%Y-%m-%d"),
            },
        )

        # Persist
        await self.store.upsert_daily_pnl(
            datetime.utcnow().strftime("%Y-%m-%d"),
            self._current_equity + self._daily_pnl,
            self._daily_pnl,
            self._pdt_trades_used,
        )

    # ── Circuit breakers ──────────────────────────────────────────────────────

    async def _check_daily_loss_cap(self) -> None:
        daily_max_loss_pct = self.cfg("capital", "daily_max_loss_pct", default=0.02)
        max_loss = self._current_equity * daily_max_loss_pct
        if self._daily_pnl <= -max_loss:
            await self._halt(
                f"Daily loss cap reached: P/L=${self._daily_pnl:.2f} "
                f"exceeds -${max_loss:.2f} ({daily_max_loss_pct*100:.0f}% of equity)",
                breach_type="daily_loss_cap",
            )

    async def _check_circuit_breaker(self) -> None:
        """3 stopouts in 60 seconds → 5-10 min pause."""
        cb_window = self.cfg("risk", "circuit_breaker_window_sec", default=60)
        cb_threshold = self.cfg("risk", "circuit_breaker_stopouts", default=3)
        cb_pause = self.cfg("risk", "circuit_breaker_pause_sec", default=600)

        cutoff = datetime.utcnow() - timedelta(seconds=cb_window)
        recent_stopouts = [t for t in self._stopout_times if t >= cutoff]

        if len(recent_stopouts) >= cb_threshold:
            await self._pause(
                f"Circuit breaker: {len(recent_stopouts)} stopouts in {cb_window}s "
                f"— systemic event",
                duration_sec=cb_pause,
            )

        # Drawdown velocity check
        vel_window = self.cfg("risk", "drawdown_velocity_window_sec", default=180)
        vel_threshold = self.cfg("risk", "drawdown_velocity_loss", default=300.0)
        vel_cutoff = datetime.utcnow() - timedelta(seconds=vel_window)
        recent_losses = sum(
            -loss for ts, loss in self._loss_velocity_window
            if ts >= vel_cutoff and loss < 0
        )
        if recent_losses >= vel_threshold:
            await self._pause(
                f"Drawdown velocity: lost ${recent_losses:.2f} in {vel_window}s",
                duration_sec=300,
            )

    async def _check_pdt_limit(self) -> None:
        pdt_min = self.cfg("capital", "pdt_minimum", default=25000.0)
        equity = self._current_equity + self._daily_pnl

        if equity < pdt_min:
            await self._halt(
                f"Account equity ${equity:.2f} below PDT minimum ${pdt_min:.2f}",
                breach_type="account_below_pdt",
            )

        if self._pdt_trades_used >= 3 and equity < pdt_min:
            await self._halt(
                "PDT day trade limit reached on sub-$25k account",
                breach_type="pdt_limit",
            )

    # ── Halt / Pause / Resume ─────────────────────────────────────────────────

    # ── FSM transition callback ───────────────────────────────────────────────

    async def _on_fsm_transition(
        self,
        from_state: SessionState,
        to_state: SessionState,
        reason: str,
    ) -> None:
        """Called by SessionStateMachine after every successful transition."""
        await self.publish(
            Topic.SYSTEM_STATE,
            payload={
                "session_state": to_state.value,
                "previous_state": from_state.value,
                "reason": reason,
                "is_trading_permitted": self._session_fsm.is_trading_permitted(),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
        logger.info("[PSA/FSM] %s → %s | %s", from_state.value, to_state.value, reason)

    # ── Halt / Pause / Resume ─────────────────────────────────────────────────

    async def _halt(self, reason: str, breach_type: str = "unknown") -> None:
        if self._system_state == SystemState.HALTED:
            return

        logger.critical("[PSA] HALT: %s", reason)
        self._system_state = SystemState.HALTED
        self._halt_reason = reason
        self._anomaly_hold = True

        # Drive §5.6 FSM
        try:
            if breach_type in ("reconciliation_mismatch",):
                await self._session_fsm.halt_reconciliation(reason)
            elif breach_type in ("feed_anomaly", "bus_failure", "bus_failure"):
                await self._session_fsm.halt_anomaly(reason)
            else:
                await self._session_fsm.halt_risk(reason)
        except SessionTransitionError as exc:
            logger.warning("[PSA] FSM halt transition skipped: %s", exc)

        await self._persist_state()
        await self.audit.record(system_halt_event(self.agent_id, reason))
        await self.audit.record(risk_breach_event(
            self.agent_id, breach_type, {"reason": reason}
        ))

        await self.publish(
            Topic.HALT_COMMAND,
            payload={
                "reason": reason,
                "state": SystemState.HALTED.value,
                "session_state": self._session_fsm.state.value,
            },
        )

    async def _pause(self, reason: str, duration_sec: int = 600) -> None:
        if self._system_state in (SystemState.HALTED, SystemState.PAUSED):
            return

        logger.warning("[PSA] PAUSE (%ds): %s", duration_sec, reason)
        self._system_state = SystemState.PAUSED
        self._pause_until = datetime.utcnow() + timedelta(seconds=duration_sec)

        # Drive §5.6 FSM (pause_cooldown auto-resumes)
        try:
            await self._session_fsm.pause_cooldown(reason, float(duration_sec))
        except SessionTransitionError as exc:
            logger.warning("[PSA] FSM pause transition skipped: %s", exc)

        await self._persist_state()
        await self.publish(
            Topic.SYSTEM_STATE,
            payload={
                "state": SystemState.PAUSED.value,
                "session_state": SessionState.PAUSED_COOLDOWN.value,
                "reason": reason,
                "resume_at": self._pause_until.isoformat(),
            },
        )

        # Legacy auto-resume (mirrors FSM auto-resume)
        asyncio.create_task(self._auto_resume(duration_sec))

    async def _auto_resume(self, duration_sec: int) -> None:
        await asyncio.sleep(duration_sec)
        if self._system_state == SystemState.PAUSED:
            self._system_state = SystemState.TRADING
            self._pause_until = None
            await self._persist_state()
            logger.info("[PSA] Auto-resumed after pause")
            await self.publish(
                Topic.RESUME_COMMAND,
                payload={"reason": "pause_duration_elapsed"},
            )

    async def _handle_resume(self, approved_by: str) -> None:
        """HGL-approved restart after a halt."""
        if self._system_state != SystemState.HALTED:
            return

        logger.warning("[PSA] RESUME approved by: %s", approved_by)
        self._system_state = SystemState.TRADING
        self._halt_reason = None
        self._anomaly_hold = False

        # Drive §5.6 FSM
        try:
            await self._session_fsm.human_resume(
                approved_by, reason=f"Human clearance by {approved_by}"
            )
        except SessionTransitionError as exc:
            logger.warning("[PSA] FSM resume transition skipped: %s", exc)

        await self._persist_state()
        await self.audit.record(system_resume_event(self.agent_id, approved_by))
        await self.publish(
            Topic.RESUME_COMMAND,
            payload={"approved_by": approved_by, "state": SystemState.TRADING.value},
        )

    # ── Alert handling ────────────────────────────────────────────────────────

    async def _handle_psa_alert(self, message: BusMessage) -> None:
        payload = message.payload
        alert_type = payload.get("alert_type", "unknown")
        reason = payload.get("reason", "unspecified")

        logger.warning("[PSA] Alert received: type=%s reason=%s", alert_type, reason)

        if alert_type in ("feed_anomaly", "reconciliation_mismatch", "bus_failure"):
            self._anomaly_hold = True
            await self._persist_state()
            await self.publish(
                Topic.SYSTEM_STATE,
                payload={
                    "anomaly_hold": True,
                    "alert_type": alert_type,
                    "reason": reason,
                },
            )

        if alert_type == "bus_failure":
            await self._halt(f"Message bus failure: {reason}", breach_type="bus_failure")

    async def _handle_reconciliation(self, message: BusMessage) -> None:
        from core.enums import ReconciliationStatus
        payload = message.payload
        status = payload.get("status")
        if status == ReconciliationStatus.MISMATCH.value:
            discrepancies = payload.get("discrepancies", [])
            await self._handle_psa_alert(BusMessage(
                topic=Topic.PSA_ALERT,
                source_agent=self.agent_id,
                payload={
                    "alert_type": "reconciliation_mismatch",
                    "reason": f"Broker mismatch: {discrepancies}",
                },
            ))

    # ── State persistence ─────────────────────────────────────────────────────

    async def _persist_state(self) -> None:
        await self.store.save_system_state(
            self._system_state, self._halt_reason, self._anomaly_hold
        )

    # ── Public accessors ──────────────────────────────────────────────────────

    @property
    def system_state(self) -> SystemState:
        return self._system_state

    @property
    def session_state(self) -> SessionState:
        """§5.6 formal session state."""
        return self._session_fsm.state

    @property
    def session_fsm(self) -> SessionStateMachine:
        """Direct access to the §5.6 FSM for coordinator bootstrap."""
        return self._session_fsm

    @property
    def is_trading_allowed(self) -> bool:
        # §5.6 FSM is the authoritative gate
        if not self._session_fsm.is_trading_permitted():
            return False
        # Legacy checks for backward compatibility
        if self._anomaly_hold:
            return False
        if self._pause_until and datetime.utcnow() < self._pause_until:
            return False
        return True

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl
