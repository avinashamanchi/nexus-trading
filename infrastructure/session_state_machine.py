"""
§5.6 Formal 9-State Session State Machine.

Owned exclusively by PSA (Agent 13). All transitions must follow the
permitted-transition table defined here.

States:
    booting                    → system initializing, no trading
    waiting_for_data_integrity → waiting for Agent 3 (DIA) to clear feed
    waiting_for_regime         → waiting for Agent 2 (MRA) regime assessment
    active                     → trading is permitted
    paused_cooldown            → temporary pause (circuit breaker, velocity)
    halted_risk                → hard halt due to risk breach (requires human)
    halted_anomaly             → halt due to data/bus anomaly (requires human)
    halted_reconciliation      → halt due to position mismatch (requires human)
    shutdown                   → terminal — no transitions out

Transition constraints:
    - Any state may transition to shutdown (emergency).
    - Halted states may only return to active with an explicit human-approval token.
    - paused_cooldown auto-resumes to active after cooldown_sec.
    - Invalid transitions raise SessionTransitionError.

This module has no I/O dependencies — it is a pure state machine suitable
for testing without mocks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from core.enums import SessionState, SESSION_STATE_TRANSITIONS

logger = logging.getLogger(__name__)


class SessionTransitionError(Exception):
    """Raised when a state transition is not permitted."""


class SessionStateMachine:
    """
    Thread-safe (asyncio-safe) formal session state machine.

    Callers:
        psa.session_state  → current SessionState
        psa.transition_to(new_state, reason, approved_by=None) → bool
        psa.is_trading_permitted() → bool
    """

    # States that require human approval to leave (back to active)
    HUMAN_APPROVAL_REQUIRED: frozenset[SessionState] = frozenset({
        SessionState.HALTED_RISK,
        SessionState.HALTED_ANOMALY,
        SessionState.HALTED_RECONCILIATION,
    })

    def __init__(
        self,
        on_transition: Callable[[SessionState, SessionState, str], Awaitable[None]] | None = None,
    ) -> None:
        """
        Args:
            on_transition: async callback called after every successful transition.
                           Signature: (from_state, to_state, reason) -> None
        """
        self._state: SessionState = SessionState.BOOTING
        self._previous_state: SessionState = SessionState.BOOTING
        self._reason: str = "System startup"
        self._entered_at: datetime = datetime.utcnow()
        self._on_transition = on_transition

        # Approval token for human-gated resumption
        self._pending_approval_by: str | None = None

        # History (last 50 transitions)
        self._history: list[dict] = []

    # ── State accessors ───────────────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def entered_at(self) -> datetime:
        return self._entered_at

    @property
    def time_in_state_sec(self) -> float:
        return (datetime.utcnow() - self._entered_at).total_seconds()

    # ── Trading permission ────────────────────────────────────────────────────

    def is_trading_permitted(self) -> bool:
        """True only when the system is in ACTIVE state."""
        return self._state == SessionState.ACTIVE

    def is_halted(self) -> bool:
        return self._state in self.HUMAN_APPROVAL_REQUIRED

    def is_paused(self) -> bool:
        return self._state == SessionState.PAUSED_COOLDOWN

    # ── Transition ────────────────────────────────────────────────────────────

    async def transition_to(
        self,
        new_state: SessionState,
        reason: str,
        approved_by: str | None = None,
    ) -> bool:
        """
        Attempt a state transition.

        For transitions from a HALTED_* state back to ACTIVE, `approved_by`
        must be a non-empty string (the human approver identity). Automated
        transitions to ACTIVE from halted states are rejected.

        Returns True on success, False if the transition is a no-op (same state).
        Raises SessionTransitionError if the transition is not permitted.
        """
        if new_state == self._state:
            return False  # no-op

        permitted = SESSION_STATE_TRANSITIONS.get(self._state.value, set())
        if new_state.value not in permitted:
            raise SessionTransitionError(
                f"Transition {self._state.value} → {new_state.value} is not permitted. "
                f"Allowed: {permitted}"
            )

        # Human approval gate for halted states
        if (
            self._state in self.HUMAN_APPROVAL_REQUIRED
            and new_state == SessionState.ACTIVE
            and not approved_by
        ):
            raise SessionTransitionError(
                f"Transition from {self._state.value} to active requires human approval. "
                "Provide approved_by."
            )

        from_state = self._state
        self._previous_state = from_state
        self._state = new_state
        self._reason = reason
        self._entered_at = datetime.utcnow()
        if approved_by:
            self._pending_approval_by = approved_by

        entry = {
            "from": from_state.value,
            "to": new_state.value,
            "reason": reason,
            "approved_by": approved_by,
            "at": self._entered_at.isoformat(),
        }
        self._history.append(entry)
        if len(self._history) > 50:
            self._history.pop(0)

        logger.info(
            "[FSM] %s → %s | reason=%s%s",
            from_state.value, new_state.value, reason,
            f" | approved_by={approved_by}" if approved_by else "",
        )

        if self._on_transition:
            await self._on_transition(from_state, new_state, reason)

        return True

    # ── Convenience transition helpers ────────────────────────────────────────

    async def boot_complete(self) -> None:
        """Boot → waiting_for_data_integrity."""
        await self.transition_to(
            SessionState.WAITING_FOR_DATA_INTEGRITY,
            "Boot sequence complete — awaiting data integrity clearance",
        )

    async def data_integrity_cleared(self) -> None:
        """waiting_for_data_integrity → waiting_for_regime."""
        await self.transition_to(
            SessionState.WAITING_FOR_REGIME,
            "Data integrity cleared — awaiting regime assessment",
        )

    async def regime_ready(self) -> None:
        """waiting_for_regime → active."""
        await self.transition_to(
            SessionState.ACTIVE,
            "Regime assessment complete — trading active",
        )

    async def pause_cooldown(self, reason: str, duration_sec: float = 300.0) -> None:
        """active → paused_cooldown. Schedules auto-resume."""
        await self.transition_to(SessionState.PAUSED_COOLDOWN, reason)
        import asyncio
        asyncio.create_task(self._auto_resume(duration_sec, reason))

    async def _auto_resume(self, duration_sec: float, pause_reason: str) -> None:
        import asyncio
        await asyncio.sleep(duration_sec)
        if self._state == SessionState.PAUSED_COOLDOWN:
            try:
                await self.transition_to(
                    SessionState.ACTIVE,
                    f"Auto-resume after {duration_sec:.0f}s cooldown (was: {pause_reason})",
                )
            except SessionTransitionError as exc:
                logger.warning("[FSM] Auto-resume failed: %s", exc)

    async def halt_risk(self, reason: str) -> None:
        """→ halted_risk."""
        await self.transition_to(SessionState.HALTED_RISK, reason)

    async def halt_anomaly(self, reason: str) -> None:
        """→ halted_anomaly (from any non-terminal state)."""
        # Allowed from: waiting_for_data_integrity, waiting_for_regime, active, paused_cooldown
        await self.transition_to(SessionState.HALTED_ANOMALY, reason)

    async def halt_reconciliation(self, reason: str) -> None:
        """→ halted_reconciliation."""
        await self.transition_to(SessionState.HALTED_RECONCILIATION, reason)

    async def human_resume(self, approved_by: str, reason: str = "Human clearance") -> None:
        """halted_* → active. Requires approver identity."""
        await self.transition_to(SessionState.ACTIVE, reason, approved_by=approved_by)

    async def shutdown(self, reason: str = "Shutdown requested") -> None:
        """Any → shutdown (terminal)."""
        await self.transition_to(SessionState.SHUTDOWN, reason)

    # ── History / introspection ───────────────────────────────────────────────

    @property
    def history(self) -> list[dict]:
        """Last 50 transitions (copies)."""
        return list(self._history)

    def to_dict(self) -> dict:
        return {
            "state": self._state.value,
            "reason": self._reason,
            "entered_at": self._entered_at.isoformat(),
            "time_in_state_sec": self.time_in_state_sec,
            "is_trading_permitted": self.is_trading_permitted(),
            "is_halted": self.is_halted(),
        }
