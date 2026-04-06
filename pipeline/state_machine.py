"""
System State Machine — manages the trading session lifecycle.

States:
  INITIALIZING → PRE_SESSION → SESSION_OPEN → TRADING
  TRADING → PAUSED (circuit breaker) → TRADING
  TRADING → HALTED (PSA) → [HGL sign-off required] → TRADING
  TRADING → POST_SESSION → INITIALIZING (next day)
  Any → SHUTDOWN
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, date
from zoneinfo import ZoneInfo

from core.enums import SystemState

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
PRE_SESSION_START = time(9, 0)
POST_SESSION_START = time(16, 5)


class TradingSessionStateMachine:
    """
    Manages the session lifecycle and drives state transitions.

    The state machine is driven by:
    - Wall clock (market open/close)
    - PSA signals (halt, pause, resume)
    - HGL approvals
    """

    def __init__(self) -> None:
        self._state = SystemState.INITIALIZING
        self._state_entered_at = datetime.utcnow()
        self._halt_reason: str | None = None
        self._transition_callbacks: list = []

    def register_callback(self, callback) -> None:
        """Register a coroutine to be called on every state transition."""
        self._transition_callbacks.append(callback)

    async def _transition(self, new_state: SystemState, reason: str = "") -> None:
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        self._state_entered_at = datetime.utcnow()
        logger.info(
            "State: %s → %s%s",
            old.value, new_state.value,
            f" ({reason})" if reason else "",
        )
        for cb in self._transition_callbacks:
            try:
                await cb(old, new_state, reason)
            except Exception as exc:
                logger.error("State transition callback error: %s", exc)

    async def run(self, check_interval_sec: float = 10.0) -> None:
        """Drive the state machine via wall clock. Run as background task."""
        while self._state != SystemState.SHUTDOWN:
            await self._tick()
            await asyncio.sleep(check_interval_sec)

    async def _tick(self) -> None:
        now_et = datetime.now(ET)
        current_time = now_et.time()
        weekday = now_et.weekday()

        # Weekend — no trading
        if weekday >= 5:
            if self._state not in (SystemState.SHUTDOWN, SystemState.INITIALIZING):
                await self._transition(SystemState.INITIALIZING, "weekend")
            return

        # Pre-session window
        if PRE_SESSION_START <= current_time < MARKET_OPEN:
            if self._state == SystemState.INITIALIZING:
                await self._transition(SystemState.PRE_SESSION, "pre-market window")

        # Market open
        elif MARKET_OPEN <= current_time < MARKET_CLOSE:
            if self._state in (SystemState.PRE_SESSION, SystemState.INITIALIZING):
                await self._transition(SystemState.SESSION_OPEN, "market opened")
            if self._state == SystemState.SESSION_OPEN:
                await self._transition(SystemState.TRADING, "session ready")

        # Post-session
        elif current_time >= POST_SESSION_START:
            if self._state in (SystemState.TRADING, SystemState.PAUSED, SystemState.SESSION_OPEN):
                await self._transition(SystemState.POST_SESSION, "market closed")

    # ── External state commands ────────────────────────────────────────────────

    async def halt(self, reason: str) -> None:
        self._halt_reason = reason
        await self._transition(SystemState.HALTED, reason)

    async def pause(self, reason: str) -> None:
        if self._state == SystemState.TRADING:
            await self._transition(SystemState.PAUSED, reason)

    async def resume(self, approved_by: str) -> None:
        if self._state in (SystemState.HALTED, SystemState.PAUSED):
            self._halt_reason = None
            await self._transition(SystemState.TRADING, f"resumed by {approved_by}")

    async def enter_post_session(self) -> None:
        await self._transition(SystemState.POST_SESSION, "forced post-session")

    async def shutdown(self) -> None:
        await self._transition(SystemState.SHUTDOWN, "operator shutdown")

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def state(self) -> SystemState:
        return self._state

    @property
    def is_trading(self) -> bool:
        return self._state == SystemState.TRADING

    @property
    def is_shadow(self) -> bool:
        return self._state == SystemState.SHADOW_MODE

    @property
    def seconds_in_state(self) -> float:
        return (datetime.utcnow() - self._state_entered_at).total_seconds()

    @property
    def is_post_session(self) -> bool:
        return self._state == SystemState.POST_SESSION

    def is_market_hours(self) -> bool:
        now_et = datetime.now(ET)
        t = now_et.time()
        return now_et.weekday() < 5 and MARKET_OPEN <= t < MARKET_CLOSE
