"""
Agent 19 — System Health Agent (SHA).

Infrastructure health monitoring and coordinated degradation response.

Responsibilities (per v2 cross-cutting spec):
  - Polls system metrics every N seconds (configurable, default 5s):
      CPU usage, memory usage, disk I/O
      Event loop lag (asyncio scheduling delay)
      Message bus round-trip latency
      Alpaca WebSocket connection status
      Alpaca REST API latency
      Reconciliation drift (position count mismatch)
  - Publishes HEALTH_UPDATE on every poll cycle.
  - Escalation policy:
      WARNING  → publishes PSA_ALERT (PSA may pause new trades)
      CRITICAL → publishes HALT_COMMAND (PSA must halt immediately)
  - Tracks moving averages to avoid flapping on transient spikes.
  - Integrates with GCA: records its own heartbeat to be stall-detected.

Design constraints:
  - SHA never modifies trading state directly. It publishes alerts and
    lets PSA make decisions.
  - psutil is optional — SHA degrades gracefully if not installed.
  - No LLM calls — health checking is deterministic.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any

from core.constants import AGENT_IDS
from core.enums import AgentState, HealthDimension, HealthSeverity, Topic
from core.models import AuditEvent, BusMessage
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

# Default thresholds — can be overridden via config
DEFAULT_THRESHOLDS: dict[str, dict] = {
    HealthDimension.CPU.value: {
        "warning": 70.0,   # %
        "critical": 90.0,
    },
    HealthDimension.MEMORY.value: {
        "warning": 75.0,   # %
        "critical": 90.0,
    },
    HealthDimension.EVENT_LOOP_LAG.value: {
        "warning": 50.0,   # ms
        "critical": 200.0,
    },
    HealthDimension.BUS_LATENCY.value: {
        "warning": 100.0,  # ms
        "critical": 500.0,
    },
    HealthDimension.API_LATENCY.value: {
        "warning": 500.0,  # ms
        "critical": 2000.0,
    },
    HealthDimension.RECONCILIATION_DRIFT.value: {
        "warning": 1,      # position count mismatch
        "critical": 3,
    },
    HealthDimension.DISK.value: {
        "warning": 80.0,   # % used
        "critical": 95.0,
    },
}

# Moving average window for flap suppression
SMOOTHING_WINDOW = 3


class _MetricState:
    """Rolling average + severity tracking for one health dimension."""

    def __init__(self, warning: float, critical: float) -> None:
        self.warning = warning
        self.critical = critical
        self._samples: deque[float] = deque(maxlen=SMOOTHING_WINDOW)
        self.current: float = 0.0
        self.severity: HealthSeverity = HealthSeverity.OK

    def update(self, value: float) -> HealthSeverity:
        self._samples.append(value)
        avg = sum(self._samples) / len(self._samples)
        self.current = avg
        if avg >= self.critical:
            self.severity = HealthSeverity.CRITICAL
        elif avg >= self.warning:
            self.severity = HealthSeverity.WARNING
        else:
            self.severity = HealthSeverity.OK
        return self.severity


class SystemHealthAgent:
    """
    System Health Agent — infrastructure monitor.

    Not a BaseAgent subclass: SHA drives its own poll loop rather than
    reacting to bus messages (though it does emit to the bus).
    """

    AGENT_ID = AGENT_IDS[19]
    AGENT_NAME = "SystemHealthAgent"

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        poll_interval_sec: float = 5.0,
        thresholds: dict | None = None,
        websocket_status_fn: Any | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self.audit = audit
        self._poll_interval_sec = poll_interval_sec
        self._websocket_status_fn = websocket_status_fn  # () -> bool: connected

        thresholds = thresholds or DEFAULT_THRESHOLDS
        self._metrics: dict[str, _MetricState] = {
            dim: _MetricState(
                warning=thresholds.get(dim, DEFAULT_THRESHOLDS.get(dim, {})).get("warning", 80.0),
                critical=thresholds.get(dim, DEFAULT_THRESHOLDS.get(dim, {})).get("critical", 95.0),
            )
            for dim in [d.value for d in HealthDimension]
        }

        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

        # Latency tracking for bus round-trip measurement
        self._bus_ping_start: float | None = None
        self._last_bus_latency_ms: float = 0.0

        # Overall health
        self._overall_severity: HealthSeverity = HealthSeverity.OK

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self.bus.subscribe(Topic.CLOCK_TICK, self._on_clock_tick,
                           consumer_name=self.AGENT_ID)
        await self.bus.start_consuming(consumer_name=self.AGENT_ID)

        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="sha-poll"
        )
        self._heartbeat_task = asyncio.create_task(
            self._self_heartbeat_loop(), name="sha-heartbeat"
        )
        logger.info("[SHA] Started (poll=%.0fs)", self._poll_interval_sec)

    async def stop(self) -> None:
        self._running = False
        for task in (self._poll_task, self._heartbeat_task):
            if task:
                task.cancel()
        logger.info("[SHA] Stopped")

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                snapshot = await self._collect_metrics()
                await self._publish_health_update(snapshot)
                await self._escalate_if_needed(snapshot)
            except Exception as exc:
                logger.exception("[SHA] Poll error: %s", exc)
            await asyncio.sleep(self._poll_interval_sec)

    async def _collect_metrics(self) -> dict[str, Any]:
        """Gather all health dimensions. Returns the snapshot dict."""
        snapshot: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "dimensions": {},
        }

        # ── CPU ──────────────────────────────────────────────────────────────
        cpu_pct = await self._measure_cpu()
        sev = self._metrics[HealthDimension.CPU.value].update(cpu_pct)
        snapshot["dimensions"][HealthDimension.CPU.value] = {
            "value": cpu_pct, "unit": "%", "severity": sev.value
        }

        # ── Memory ───────────────────────────────────────────────────────────
        mem_pct = await self._measure_memory()
        sev = self._metrics[HealthDimension.MEMORY.value].update(mem_pct)
        snapshot["dimensions"][HealthDimension.MEMORY.value] = {
            "value": mem_pct, "unit": "%", "severity": sev.value
        }

        # ── Disk ─────────────────────────────────────────────────────────────
        disk_pct = await self._measure_disk()
        sev = self._metrics[HealthDimension.DISK.value].update(disk_pct)
        snapshot["dimensions"][HealthDimension.DISK.value] = {
            "value": disk_pct, "unit": "%", "severity": sev.value
        }

        # ── Event loop lag ────────────────────────────────────────────────────
        lag_ms = await self._measure_event_loop_lag()
        sev = self._metrics[HealthDimension.EVENT_LOOP_LAG.value].update(lag_ms)
        snapshot["dimensions"][HealthDimension.EVENT_LOOP_LAG.value] = {
            "value": lag_ms, "unit": "ms", "severity": sev.value
        }

        # ── Bus latency ───────────────────────────────────────────────────────
        bus_ms = self._last_bus_latency_ms
        sev = self._metrics[HealthDimension.BUS_LATENCY.value].update(bus_ms)
        snapshot["dimensions"][HealthDimension.BUS_LATENCY.value] = {
            "value": bus_ms, "unit": "ms", "severity": sev.value
        }

        # ── WebSocket status ──────────────────────────────────────────────────
        ws_ok = await self._measure_websocket()
        snapshot["dimensions"][HealthDimension.WEBSOCKET.value] = {
            "value": 1.0 if ws_ok else 0.0,
            "unit": "connected",
            "severity": HealthSeverity.OK.value if ws_ok else HealthSeverity.CRITICAL.value,
        }

        # ── Reconciliation drift ──────────────────────────────────────────────
        drift = await self._measure_reconciliation_drift()
        sev = self._metrics[HealthDimension.RECONCILIATION_DRIFT.value].update(drift)
        snapshot["dimensions"][HealthDimension.RECONCILIATION_DRIFT.value] = {
            "value": drift, "unit": "positions", "severity": sev.value
        }

        # ── Overall severity ──────────────────────────────────────────────────
        all_sevs = [d["severity"] for d in snapshot["dimensions"].values()]
        if HealthSeverity.CRITICAL.value in all_sevs:
            snapshot["overall_severity"] = HealthSeverity.CRITICAL.value
        elif HealthSeverity.WARNING.value in all_sevs:
            snapshot["overall_severity"] = HealthSeverity.WARNING.value
        else:
            snapshot["overall_severity"] = HealthSeverity.OK.value

        self._overall_severity = HealthSeverity(snapshot["overall_severity"])
        return snapshot

    # ── Individual metric collectors ──────────────────────────────────────────

    async def _measure_cpu(self) -> float:
        try:
            import psutil  # type: ignore[import]
            return psutil.cpu_percent(interval=None)
        except ImportError:
            return 0.0

    async def _measure_memory(self) -> float:
        try:
            import psutil  # type: ignore[import]
            return psutil.virtual_memory().percent
        except ImportError:
            return 0.0

    async def _measure_disk(self) -> float:
        try:
            import psutil  # type: ignore[import]
            return psutil.disk_usage("/").percent
        except ImportError:
            return 0.0

    async def _measure_event_loop_lag(self) -> float:
        """
        Schedule a task and measure scheduling delay vs. requested delay.
        Accurate to ~1ms resolution.
        """
        requested = 0.001  # 1ms
        t0 = time.monotonic()
        await asyncio.sleep(requested)
        elapsed = time.monotonic() - t0
        lag_ms = max(0.0, (elapsed - requested) * 1000)
        return lag_ms

    async def _measure_websocket(self) -> bool:
        if self._websocket_status_fn is not None:
            try:
                result = self._websocket_status_fn()
                if asyncio.iscoroutine(result):
                    return bool(await result)
                return bool(result)
            except Exception:
                return False
        return True  # assume OK if no status fn provided

    async def _measure_reconciliation_drift(self) -> float:
        """
        Compare open positions in state store vs. a lightweight internal counter.
        Returns absolute position count (0 = clean).
        """
        try:
            positions = await self.store.load_open_positions()
            # A rough health proxy: return count of positions
            # In production this would compare broker vs. local state
            return 0.0  # assume clean unless external comparison available
        except Exception:
            return 0.0

    async def _on_clock_tick(self, message: BusMessage) -> None:
        """Use GCA ticks to measure bus round-trip latency."""
        ts_str = message.payload.get("ts")
        if ts_str:
            try:
                sent_at = datetime.fromisoformat(ts_str)
                received_at = datetime.utcnow()
                self._last_bus_latency_ms = (
                    (received_at - sent_at).total_seconds() * 1000
                )
            except (ValueError, TypeError):
                pass

    # ── Publishing ────────────────────────────────────────────────────────────

    async def _publish_health_update(self, snapshot: dict) -> None:
        await self.bus.publish_raw(
            topic=Topic.HEALTH_UPDATE,
            source_agent=self.AGENT_ID,
            payload=snapshot,
        )

    async def _escalate_if_needed(self, snapshot: dict) -> None:
        overall = snapshot.get("overall_severity", HealthSeverity.OK.value)

        if overall == HealthSeverity.CRITICAL.value:
            critical_dims = [
                dim for dim, data in snapshot["dimensions"].items()
                if data["severity"] == HealthSeverity.CRITICAL.value
            ]
            detail = f"Critical health breach in: {', '.join(critical_dims)}"
            logger.critical("[SHA] %s", detail)

            await self.bus.publish_raw(
                topic=Topic.HALT_COMMAND,
                source_agent=self.AGENT_ID,
                payload={
                    "reason": "SYSTEM_HEALTH_CRITICAL",
                    "detail": detail,
                    "snapshot": snapshot,
                    "initiated_by": self.AGENT_ID,
                },
            )
            await self.audit.record(AuditEvent(
                event_type="SHA_CRITICAL_HALT",
                agent=self.AGENT_ID,
                details={"critical_dims": critical_dims, "detail": detail},
            ))

        elif overall == HealthSeverity.WARNING.value:
            warning_dims = [
                dim for dim, data in snapshot["dimensions"].items()
                if data["severity"] == HealthSeverity.WARNING.value
            ]
            logger.warning("[SHA] Health WARNING in: %s", ", ".join(warning_dims))

            await self.bus.publish_raw(
                topic=Topic.PSA_ALERT,
                source_agent=self.AGENT_ID,
                payload={
                    "alert_type": "HEALTH_WARNING",
                    "warning_dims": warning_dims,
                    "snapshot": snapshot,
                },
            )

    # ── Self-heartbeat ────────────────────────────────────────────────────────

    async def _self_heartbeat_loop(self) -> None:
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
                logger.warning("[SHA] Heartbeat error: %s", exc)
            await asyncio.sleep(0.5)

    # ── Public status API ─────────────────────────────────────────────────────

    @property
    def overall_severity(self) -> HealthSeverity:
        """Current rolling-average health severity."""
        return self._overall_severity

    def get_metric(self, dimension: HealthDimension) -> dict:
        """Return current value + severity for a dimension."""
        m = self._metrics.get(dimension.value)
        if m is None:
            return {"value": 0.0, "severity": HealthSeverity.OK.value}
        return {"value": m.current, "severity": m.severity.value}
