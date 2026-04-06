"""
Agent 17 — Global Clock Agent (GCA).

Single authoritative time source for the entire system.

Responsibilities (per §5.x cross-cutting spec):
  - Issues CLOCK_TICK heartbeats at a configurable rate (default 100ms).
  - Detects session open (09:30 ET) and close (16:00 ET), publishing
    SESSION_START / SESSION_END on the bus.
  - Owns a named-timer registry: agents register timers here instead of
    managing their own asyncio.sleep calls. When a timer expires, GCA
    publishes TIMER_EXPIRED with the timer name and metadata.
  - Provides a synchronous now() method for callers within the process.
  - Detects agent stall: if an agent's heartbeat gap exceeds the stall
    threshold, GCA publishes a STARVATION_ALERT.

Design constraints:
  - GCA never modifies trading state. It is read-only infrastructure.
  - All timestamps issued by GCA use UTC (datetime.utcnow()).
  - Timer resolution is bounded by tick_interval_ms (default 100ms).
  - GCA has no LLM dependency.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.constants import AGENT_IDS, AGENT_TIMEOUT_SEC
from core.enums import AgentState, Topic
from core.models import BusMessage, AuditEvent
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)   # hour, minute ET
MARKET_CLOSE = (16, 0)  # hour, minute ET


class _Timer:
    """Internal representation of a registered timer."""
    __slots__ = ("name", "expires_at", "metadata", "repeat_sec")

    def __init__(
        self,
        name: str,
        expires_at: datetime,
        metadata: dict,
        repeat_sec: float | None = None,
    ) -> None:
        self.name = name
        self.expires_at = expires_at
        self.metadata = metadata
        self.repeat_sec = repeat_sec  # if set, auto-reschedule


class GlobalClockAgent:
    """
    Global Clock Agent — not a BaseAgent subclass because GCA is infrastructure,
    not a pipeline processor. It owns its own run loop directly.

    Usage:
        gca = GlobalClockAgent(bus, store, audit)
        await gca.start()
        # gca.now() → current UTC datetime
        # gca.register_timer("my_timer", expires_at, metadata)
        await gca.stop()
    """

    AGENT_ID = AGENT_IDS[17]
    AGENT_NAME = "GlobalClockAgent"

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        tick_interval_ms: float = 100.0,
        stall_threshold_sec: float = AGENT_TIMEOUT_SEC,
    ) -> None:
        self.bus = bus
        self.store = store
        self.audit = audit
        self._tick_interval_sec = tick_interval_ms / 1000.0
        self._stall_threshold_sec = stall_threshold_sec

        self._running = False
        self._tick_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

        # Named timer registry: name → _Timer
        self._timers: dict[str, _Timer] = {}

        # Session-open tracking (avoid re-firing)
        self._session_open_fired: bool = False
        self._session_close_fired: bool = False

        # Last known heartbeat per agent_id: datetime
        self._last_heartbeat: dict[str, datetime] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def now(self) -> datetime:
        """Authoritative current time (UTC). Use this instead of datetime.utcnow()."""
        return datetime.utcnow()

    def register_timer(
        self,
        name: str,
        expires_at: datetime,
        metadata: dict | None = None,
        repeat_sec: float | None = None,
    ) -> None:
        """
        Register a named timer. When `expires_at` passes, GCA publishes
        TIMER_EXPIRED with {name, metadata}. If `repeat_sec` is set, the
        timer auto-reschedules.
        """
        self._timers[name] = _Timer(
            name=name,
            expires_at=expires_at,
            metadata=metadata or {},
            repeat_sec=repeat_sec,
        )
        logger.debug("[GCA] Timer registered: %s expires=%s", name, expires_at.isoformat())

    def cancel_timer(self, name: str) -> bool:
        """Cancel a named timer. Returns True if it existed."""
        if name in self._timers:
            del self._timers[name]
            logger.debug("[GCA] Timer cancelled: %s", name)
            return True
        return False

    def time_until(self, name: str) -> float | None:
        """Seconds until named timer fires. None if not registered or already expired."""
        timer = self._timers.get(name)
        if timer is None:
            return None
        remaining = (timer.expires_at - self.now()).total_seconds()
        return max(0.0, remaining)

    def record_heartbeat(self, agent_id: str) -> None:
        """Called when an AGENT_HEARTBEAT message is received for a given agent."""
        self._last_heartbeat[agent_id] = self.now()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self.bus.subscribe(Topic.AGENT_HEARTBEAT, self._on_heartbeat)
        await self.bus.start_consuming(consumer_name=self.AGENT_ID)
        self._tick_task = asyncio.create_task(self._tick_loop(), name="gca-tick")
        self._heartbeat_task = asyncio.create_task(
            self._self_heartbeat_loop(), name="gca-self-heartbeat"
        )
        logger.info("[GCA] Started (tick=%.0fms, stall=%.0fs)",
                    self._tick_interval_sec * 1000, self._stall_threshold_sec)

    async def stop(self) -> None:
        self._running = False
        for task in (self._tick_task, self._heartbeat_task):
            if task:
                task.cancel()
        logger.info("[GCA] Stopped")

    # ── Main tick loop ────────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                now = self.now()
                await self._publish_tick(now)
                await self._check_session_boundaries(now)
                await self._fire_expired_timers(now)
                await self._check_stalled_agents(now)
            except Exception as exc:
                logger.exception("[GCA] tick loop error: %s", exc)
            await asyncio.sleep(self._tick_interval_sec)

    async def _publish_tick(self, now: datetime) -> None:
        await self.bus.publish_raw(
            topic=Topic.CLOCK_TICK,
            source_agent=self.AGENT_ID,
            payload={"ts": now.isoformat(), "epoch_ms": int(now.timestamp() * 1000)},
        )

    async def _check_session_boundaries(self, now: datetime) -> None:
        now_et = now.astimezone(ET)
        h, m = now_et.hour, now_et.minute

        # Session open: exactly 09:30 ET on weekdays
        if (h, m) == MARKET_OPEN and now_et.weekday() < 5:
            if not self._session_open_fired:
                self._session_open_fired = True
                self._session_close_fired = False  # reset for today
                await self._publish_session_event(Topic.SESSION_START, now)
                logger.info("[GCA] SESSION_START published at %s", now.isoformat())
        else:
            # Reset when we're well past open time (handle next day)
            if h < MARKET_OPEN[0] or (h == MARKET_OPEN[0] and m < MARKET_OPEN[1]):
                self._session_open_fired = False

        # Session close: exactly 16:00 ET on weekdays
        if (h, m) >= MARKET_CLOSE and now_et.weekday() < 5:
            if not self._session_close_fired and self._session_open_fired:
                self._session_close_fired = True
                await self._publish_session_event(Topic.SESSION_END, now)
                logger.info("[GCA] SESSION_END published at %s", now.isoformat())

    async def _publish_session_event(self, topic: Topic, now: datetime) -> None:
        now_et = now.astimezone(ET)
        await self.bus.publish_raw(
            topic=topic,
            source_agent=self.AGENT_ID,
            payload={
                "ts": now.isoformat(),
                "ts_et": now_et.isoformat(),
                "date": now_et.strftime("%Y-%m-%d"),
            },
        )
        await self.audit.record(AuditEvent(
            event_type=topic.value.upper(),
            agent=self.AGENT_ID,
            details={"ts": now.isoformat()},
        ))

    async def _fire_expired_timers(self, now: datetime) -> None:
        expired = [t for t in self._timers.values() if t.expires_at <= now]
        for timer in expired:
            try:
                await self.bus.publish_raw(
                    topic=Topic.TIMER_EXPIRED,
                    source_agent=self.AGENT_ID,
                    payload={
                        "timer_name": timer.name,
                        "expired_at": timer.expires_at.isoformat(),
                        "fired_at": now.isoformat(),
                        **timer.metadata,
                    },
                )
                logger.debug("[GCA] Timer fired: %s", timer.name)
                if timer.repeat_sec is not None:
                    # Reschedule from the original expiry to avoid drift
                    timer.expires_at = timer.expires_at + timedelta(seconds=timer.repeat_sec)
                else:
                    del self._timers[timer.name]
            except Exception as exc:
                logger.warning("[GCA] Error firing timer %s: %s", timer.name, exc)

    async def _check_stalled_agents(self, now: datetime) -> None:
        for agent_id, last_hb in list(self._last_heartbeat.items()):
            gap = (now - last_hb).total_seconds()
            if gap > self._stall_threshold_sec:
                logger.warning("[GCA] Agent stall detected: %s (gap=%.1fs)", agent_id, gap)
                await self.bus.publish_raw(
                    topic=Topic.STARVATION_ALERT,
                    source_agent=self.AGENT_ID,
                    payload={
                        "agent_id": agent_id,
                        "last_heartbeat": last_hb.isoformat(),
                        "gap_sec": gap,
                        "threshold_sec": self._stall_threshold_sec,
                    },
                )
                # Remove so we don't spam — will re-add on next heartbeat
                del self._last_heartbeat[agent_id]

    async def _on_heartbeat(self, message: BusMessage) -> None:
        agent_id = message.payload.get("agent_id")
        if agent_id:
            self.record_heartbeat(agent_id)

    async def _self_heartbeat_loop(self) -> None:
        """GCA publishes its own heartbeat so the system knows it is alive."""
        from core.models import AgentHeartbeat
        while self._running:
            try:
                hb = AgentHeartbeat(
                    agent_id=self.AGENT_ID,
                    agent_name=self.AGENT_NAME,
                    state=AgentState.PROCESSING,
                    error_count=0,
                )
                await self.bus.publish_raw(
                    topic=Topic.AGENT_HEARTBEAT,
                    source_agent=self.AGENT_ID,
                    payload=hb.model_dump(mode="json"),
                )
                await self.store.save_heartbeat(
                    self.AGENT_ID, self.AGENT_NAME, AgentState.PROCESSING.value, 0
                )
            except Exception as exc:
                logger.warning("[GCA] Self-heartbeat error: %s", exc)
            await asyncio.sleep(0.5)
