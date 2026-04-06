"""
Agent 10 — Latency & Execution Quality Agent

Measures fill quality on every order and feeds aggregated metrics back to SPA
and PSA so they can widen tolerances or pause trading when quality degrades.

Safety contract:
  - All execution metrics are immutable once written to the state store.
  - Quality degradation (avg_slippage_bps > 15 OR rejection_rate > 0.15) raises
    an EXECUTION_QUALITY event so SPA can widen entry tolerances.
  - Severe degradation (slippage > 2x threshold) also publishes a PSA_ALERT.
  - Session summary is published every 60 seconds regardless of activity.

Subscribes to: ORDER_SUBMITTED, FILL_EVENT, ORDER_UPDATE
Publishes to: EXECUTION_QUALITY
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from collections import deque
from datetime import datetime
from typing import Any

from agents.base import BaseAgent
from core.enums import OrderStatus, Topic
from core.models import (
    AuditEvent,
    BusMessage,
    ExecutionMetrics,
    SessionExecutionSummary,
)
from infrastructure.audit_log import AuditLog
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore

logger = logging.getLogger(__name__)

_SUMMARY_PUBLISH_INTERVAL_SEC = 60.0
_ROLLING_WINDOW_SIZE = 50


class ExecutionQualityAgent(BaseAgent):
    """
    Agent 10 — Latency & Execution Quality Agent.

    Tracks every order submission and fill to compute real-time and
    session-level execution quality statistics.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        config: dict | None = None,
    ) -> None:
        super().__init__(
            agent_id="agent_10_execution_quality",
            agent_name="ExecutionQualityAgent",
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )

        # ── Per-order submission tracking ──────────────────────────────────────
        # order_id → (submit_timestamp, expected_price)
        self._pending_fills: dict[str, tuple[datetime, float]] = {}

        # ── Rolling window of ExecutionMetrics (last 50) ───────────────────────
        self._metrics_window: deque[ExecutionMetrics] = deque(
            maxlen=_ROLLING_WINDOW_SIZE
        )

        # Rejection tracking (order_id → True)
        self._rejections: dict[str, bool] = {}

        # Total orders seen this session (for rejection_rate denominator)
        self._total_orders: int = 0

        # Quality degradation flag
        self._quality_degraded: bool = False

        # Session date string (YYYY-MM-DD)
        self._session_date: str = datetime.utcnow().strftime("%Y-%m-%d")

        # Background summary publish task
        self._summary_task: asyncio.Task | None = None

        # Thresholds (read from config)
        self._slippage_pause_threshold_bps: float = 15.0
        self._rejection_rate_threshold: float = 0.15

    # ── Topic registration ─────────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.ORDER_SUBMITTED, Topic.FILL_EVENT, Topic.ORDER_UPDATE]

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_startup(self) -> None:
        # Load thresholds from config
        self._slippage_pause_threshold_bps = float(
            self.cfg("execution_quality", "slippage_pause_threshold_bps", default=15.0)
        )
        self._rejection_rate_threshold = float(
            self.cfg("execution_quality", "rejection_rate_pause_threshold", default=0.15)
        )
        self._session_date = datetime.utcnow().strftime("%Y-%m-%d")

        self._summary_task = asyncio.create_task(
            self._session_summary_publish_loop(),
            name=f"{self.agent_id}-summary-loop",
        )
        logger.info(
            "[%s] Started — slippage_threshold=%.1f bps rejection_rate_threshold=%.2f",
            self.agent_name,
            self._slippage_pause_threshold_bps,
            self._rejection_rate_threshold,
        )

    async def on_shutdown(self) -> None:
        if self._summary_task:
            self._summary_task.cancel()
        logger.info("[%s] Shutdown complete", self.agent_name)

    # ── Main dispatch ──────────────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        if message.topic == Topic.ORDER_SUBMITTED:
            await self._handle_order_submitted(message)
        elif message.topic == Topic.FILL_EVENT:
            await self._handle_fill_event(message)
        elif message.topic == Topic.ORDER_UPDATE:
            await self._handle_order_update(message)

    # ── ORDER_SUBMITTED: record submission timestamp ───────────────────────────

    async def _handle_order_submitted(self, message: BusMessage) -> None:
        order_id: str = message.payload.get("order_id", "")
        if not order_id:
            return

        # Prefer the submitted_at from the payload; fall back to message timestamp
        submitted_at_str: str | None = message.payload.get("submitted_at")
        if submitted_at_str:
            try:
                submit_time = datetime.fromisoformat(submitted_at_str)
            except ValueError:
                submit_time = message.timestamp
        else:
            submit_time = message.timestamp

        expected_price: float = float(message.payload.get("entry_price", 0.0))

        self._pending_fills[order_id] = (submit_time, expected_price)
        self._total_orders += 1

        logger.debug(
            "[%s] Tracking ORDER_SUBMITTED order_id=%s expected_price=%.4f",
            self.agent_name, order_id, expected_price,
        )

    # ── FILL_EVENT: compute latency + slippage, publish metrics ───────────────

    async def _handle_fill_event(self, message: BusMessage) -> None:
        order_id: str = message.payload.get("order_id", "")
        symbol: str = message.payload.get("symbol", "")
        fill_price: float = float(message.payload.get("price", 0.0))

        # Parse fill timestamp
        filled_at_str: str | None = message.payload.get("filled_at")
        if filled_at_str:
            try:
                fill_time = datetime.fromisoformat(filled_at_str)
            except ValueError:
                fill_time = datetime.utcnow()
        else:
            fill_time = datetime.utcnow()

        # Retrieve submission record
        if order_id not in self._pending_fills:
            logger.debug(
                "[%s] FILL_EVENT for unknown order_id=%s — no submit record",
                self.agent_name, order_id,
            )
            # Record fill with unknown latency
            metrics = ExecutionMetrics(
                order_id=order_id,
                symbol=symbol,
                expected_price=fill_price,
                actual_fill_price=fill_price,
                slippage_bps=0.0,
                order_to_fill_latency_ms=-1.0,
            )
        else:
            submit_time, expected_price = self._pending_fills.pop(order_id)

            latency_ms = (fill_time - submit_time).total_seconds() * 1000.0
            # Slippage: absolute difference in basis points
            if expected_price > 0:
                slippage_bps = abs(fill_price - expected_price) / expected_price * 10_000
            else:
                slippage_bps = 0.0

            metrics = ExecutionMetrics(
                order_id=order_id,
                symbol=symbol,
                expected_price=expected_price,
                actual_fill_price=fill_price,
                slippage_bps=round(slippage_bps, 4),
                order_to_fill_latency_ms=round(max(latency_ms, 0.0), 3),
            )

            logger.info(
                "[%s] FILL order_id=%s symbol=%s fill=%.4f expected=%.4f "
                "slippage=%.2f bps latency=%.1f ms",
                self.agent_name, order_id, symbol,
                fill_price, expected_price, slippage_bps, latency_ms,
            )

        # Add to rolling window
        self._metrics_window.append(metrics)

        # Immutable storage in audit log
        await self.audit.record(AuditEvent(
            event_type="EXECUTION_METRICS",
            agent=self.agent_id,
            symbol=symbol,
            details=metrics.model_dump(mode="json"),
            immutable=True,
        ))

        # Publish individual metrics immediately
        await self.publish(
            topic=Topic.EXECUTION_QUALITY,
            payload=metrics.model_dump(mode="json"),
        )

        # Check for quality degradation after each fill
        await self._check_quality_degradation()

    # ── ORDER_UPDATE: detect rejections ───────────────────────────────────────

    async def _handle_order_update(self, message: BusMessage) -> None:
        status_str: str = message.payload.get("status", "")
        order_id: str = message.payload.get("order_id", "")
        symbol: str = message.payload.get("symbol", "")

        if status_str != OrderStatus.REJECTED.value:
            return

        reject_reason: str = message.payload.get("reject_reason", "unknown")
        self._rejections[order_id] = True

        # Clean up pending fills if this order was rejected
        expected_price_for_reject = 0.0
        if order_id in self._pending_fills:
            _, expected_price_for_reject = self._pending_fills.pop(order_id)

        rejected_metrics = ExecutionMetrics(
            order_id=order_id,
            symbol=symbol,
            expected_price=expected_price_for_reject,
            actual_fill_price=0.0,
            slippage_bps=0.0,
            order_to_fill_latency_ms=0.0,
            rejected=True,
            reject_reason=reject_reason,
        )
        self._metrics_window.append(rejected_metrics)

        await self.audit.record(AuditEvent(
            event_type="ORDER_REJECTED",
            agent=self.agent_id,
            symbol=symbol,
            details={
                "order_id": order_id,
                "reject_reason": reject_reason,
            },
            immutable=True,
        ))

        await self.publish(
            topic=Topic.EXECUTION_QUALITY,
            payload=rejected_metrics.model_dump(mode="json"),
        )

        logger.warning(
            "[%s] ORDER REJECTED order_id=%s symbol=%s reason=%s",
            self.agent_name, order_id, symbol, reject_reason,
        )

        await self._check_quality_degradation()

    # ── Quality degradation check ──────────────────────────────────────────────

    async def _check_quality_degradation(self) -> None:
        summary = self._compute_session_summary()
        was_degraded = self._quality_degraded

        newly_degraded = (
            summary.avg_slippage_bps > self._slippage_pause_threshold_bps
            or summary.rejection_rate > self._rejection_rate_threshold
        )

        self._quality_degraded = newly_degraded

        if newly_degraded and not was_degraded:
            logger.warning(
                "[%s] QUALITY DEGRADED — avg_slippage=%.2f bps (thresh=%.1f) "
                "rejection_rate=%.3f (thresh=%.2f)",
                self.agent_name,
                summary.avg_slippage_bps, self._slippage_pause_threshold_bps,
                summary.rejection_rate, self._rejection_rate_threshold,
            )
            # Publish summary with quality_degraded=True so SPA can widen tolerances
            summary.quality_degraded = True
            await self.publish(
                topic=Topic.EXECUTION_QUALITY,
                payload=summary.model_dump(mode="json"),
            )

            # Escalate to PSA if slippage is severe (>2x threshold)
            if summary.avg_slippage_bps > 2 * self._slippage_pause_threshold_bps:
                await self.publish(
                    topic=Topic.PSA_ALERT,
                    payload={
                        "source": self.agent_id,
                        "alert_type": "SEVERE_EXECUTION_DEGRADATION",
                        "avg_slippage_bps": round(summary.avg_slippage_bps, 2),
                        "threshold_bps": self._slippage_pause_threshold_bps,
                        "rejection_rate": round(summary.rejection_rate, 4),
                        "fill_rate": round(summary.fill_rate, 4),
                        "session_date": self._session_date,
                    },
                )
                logger.error(
                    "[%s] PSA_ALERT raised: severe execution degradation "
                    "avg_slippage=%.2f bps (2x threshold=%.1f)",
                    self.agent_name,
                    summary.avg_slippage_bps, self._slippage_pause_threshold_bps,
                )

        elif was_degraded and not newly_degraded:
            logger.info(
                "[%s] Execution quality recovered — avg_slippage=%.2f bps "
                "rejection_rate=%.3f",
                self.agent_name,
                summary.avg_slippage_bps,
                summary.rejection_rate,
            )
            summary.quality_degraded = False
            await self.publish(
                topic=Topic.EXECUTION_QUALITY,
                payload=summary.model_dump(mode="json"),
            )

    @property
    def session_summary(self) -> "SessionExecutionSummary":
        """Public accessor used by SPA to check execution quality degradation."""
        return self._compute_session_summary()

    # ── Session summary computation ────────────────────────────────────────────

    def _compute_session_summary(self) -> SessionExecutionSummary:
        window = list(self._metrics_window)

        if not window:
            return SessionExecutionSummary(
                session_date=self._session_date,
                total_orders=self._total_orders,
                fill_rate=0.0,
                avg_slippage_bps=0.0,
                p50_latency_ms=0.0,
                p95_latency_ms=0.0,
                p99_latency_ms=0.0,
                rejection_rate=0.0,
                quality_degraded=self._quality_degraded,
            )

        # Filled orders only for latency + slippage stats
        fills = [m for m in window if not m.rejected and m.order_to_fill_latency_ms >= 0]
        rejections = [m for m in window if m.rejected]

        total_in_window = len(window)
        rejection_rate = len(rejections) / total_in_window if total_in_window > 0 else 0.0
        fill_rate = len(fills) / total_in_window if total_in_window > 0 else 0.0

        if fills:
            avg_slippage_bps = statistics.mean(m.slippage_bps for m in fills)
            latencies = sorted(m.order_to_fill_latency_ms for m in fills)
            n = len(latencies)
            p50 = _percentile(latencies, 50)
            p95 = _percentile(latencies, 95)
            p99 = _percentile(latencies, 99)
        else:
            avg_slippage_bps = 0.0
            p50 = p95 = p99 = 0.0

        return SessionExecutionSummary(
            session_date=self._session_date,
            total_orders=self._total_orders,
            fill_rate=round(fill_rate, 4),
            avg_slippage_bps=round(avg_slippage_bps, 4),
            p50_latency_ms=round(p50, 3),
            p95_latency_ms=round(p95, 3),
            p99_latency_ms=round(p99, 3),
            rejection_rate=round(rejection_rate, 4),
            quality_degraded=self._quality_degraded,
        )

    # ── Periodic session summary publish loop ──────────────────────────────────

    async def _session_summary_publish_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(_SUMMARY_PUBLISH_INTERVAL_SEC)
                await self._publish_session_summary()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception(
                    "[%s] Error in summary publish loop: %s", self.agent_name, exc
                )

    async def _publish_session_summary(self) -> None:
        summary = self._compute_session_summary()
        await self.publish(
            topic=Topic.EXECUTION_QUALITY,
            payload=summary.model_dump(mode="json"),
        )
        logger.info(
            "[%s] Session summary: orders=%d fills=%.1f%% avg_slip=%.2f bps "
            "p50=%.1fms p95=%.1fms p99=%.1fms rejections=%.1f%% degraded=%s",
            self.agent_name,
            summary.total_orders,
            summary.fill_rate * 100,
            summary.avg_slippage_bps,
            summary.p50_latency_ms,
            summary.p95_latency_ms,
            summary.p99_latency_ms,
            summary.rejection_rate * 100,
            summary.quality_degraded,
        )


# ── Utility ────────────────────────────────────────────────────────────────────

def _percentile(sorted_data: list[float], pct: int) -> float:
    """Return the pct-th percentile of a pre-sorted list.  Returns 0 if empty."""
    if not sorted_data:
        return 0.0
    n = len(sorted_data)
    idx = (pct / 100) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_data[-1]
    frac = idx - lo
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])
