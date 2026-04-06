"""
Agent 3 — Data Integrity Agent

Monitors the live market data stream for anomalies and gates MiSA by
publishing FEED_STATUS reports.  MiSA must not emit signals until it receives
a CLEAN feed confirmation from this agent.

Safety contract:
  - MiSA is blocked until this agent publishes FeedStatus.CLEAN.
  - Any anomaly triggers immediate hold on new signals.
  - Feed must pass 3 consecutive clean checks before CLEAN is declared.
  - Publishes a health heartbeat every 5 seconds regardless of state.
  - Anomaly history is preserved for PSRA post-session review.

Architecture note:
  This agent does NOT subscribe to upstream message bus topics.  Instead it
  reads directly from the AlpacaDataFeed and OrderBookCache references
  injected at construction time.  Its output (Topic.FEED_STATUS) is read
  by Agent 2 (MarketRegime) and implicitly gates all downstream agents.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import Any

from core.enums import (
    FeedAnomaly,
    FeedStatus,
    Topic,
)
from core.models import (
    AuditEvent,
    BusMessage,
    FeedStatusReport,
)
from agents.base import BaseAgent
from data.feed import AlpacaDataFeed
from data.level2 import OrderBookCache
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_MONITOR_INTERVAL_SEC = 0.10          # 100 ms polling interval
_HEARTBEAT_INTERVAL_SEC = 5.0         # publish status every 5 seconds always
_CLEAN_CONSECUTIVE_REQUIRED = 3       # consecutive clean checks before CLEAN


class DataIntegrityAgent(BaseAgent):
    """
    Agent 3 — Data Integrity Agent.

    Continuously monitors the AlpacaDataFeed tick cache and OrderBookCache
    for stale ticks, crossed markets, missing L2 data, and sequence gaps.

    Message flow:
      SUBSCRIBES: (none — reads directly from feed/order book)
      PUBLISHES:  Topic.FEED_STATUS  (FeedStatusReport, every 5 sec + on change)
                  Topic.PSA_ALERT    (on anomaly detection)
    """

    AGENT_ID = "agent_03_data_integrity"
    AGENT_NAME = "DataIntegrityAgent"

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        data_feed: AlpacaDataFeed,
        order_book: OrderBookCache,
        config: dict | None = None,
    ) -> None:
        super().__init__(
            agent_id=self.AGENT_ID,
            agent_name=self.AGENT_NAME,
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )
        self._data_feed = data_feed
        self._order_book = order_book

        # Feed gate state
        self._feed_clean: bool = False          # pessimistic start
        self._consecutive_clean_checks: int = 0

        # Sequence tracking: symbol → last seen sequence number
        self._last_sequence: dict[str, int] = {}

        # Anomaly history for PSRA review
        self._anomaly_history: deque[dict] = deque(maxlen=1000)

        # Background tasks
        self._monitor_task: asyncio.Task | None = None
        self._heartbeat_task_feed: asyncio.Task | None = None

        # Timing for heartbeat cadence
        self._last_heartbeat_at: datetime | None = None

    # ── Topic subscriptions ───────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        # This agent does not subscribe to any upstream message bus topics.
        return []

    # ── Lifecycle override ────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Override BaseAgent.start() to also launch the monitoring loop.
        The monitoring loop runs independently of the bus consumer loop.
        """
        self._running = True
        from core.enums import AgentState as AS
        self.state = AS.IDLE
        logger.info("[%s] Starting", self.AGENT_NAME)

        await self.on_startup()

        # This agent has no subscribed_topics so the bus wont register any
        # handlers, but we call start_consuming for completeness (it will be a
        # no-op for the empty handler list).
        await self.bus.start_consuming(consumer_name=self.AGENT_ID)

        # Start the agent's standard heartbeat (base class)
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"{self.AGENT_ID}-heartbeat"
        )

        # Start the data integrity monitoring loop
        self._monitor_task = asyncio.create_task(
            self._monitoring_loop(), name=f"{self.AGENT_ID}-monitor"
        )

        # Start the feed-status heartbeat (publishes every 5 sec regardless)
        self._heartbeat_task_feed = asyncio.create_task(
            self._feed_heartbeat_loop(), name=f"{self.AGENT_ID}-feed-heartbeat"
        )

        logger.info("[%s] Ready", self.AGENT_NAME)

    async def on_startup(self) -> None:
        logger.info(
            "[%s] Data Integrity Agent initializing — feed is BLOCKED until CLEAN",
            self.AGENT_NAME,
        )
        # Publish an initial HALTED status so downstream agents start blocked
        await self._publish_feed_status(
            status=FeedStatus.HALTED,
            anomalies=[],
            affected_symbols=[],
            details="Agent starting — feed not yet verified",
        )

    async def on_shutdown(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._heartbeat_task_feed:
            self._heartbeat_task_feed.cancel()

    # ── Required abstract method (no-op since we have no subscribed topics) ──

    async def process(self, message: BusMessage) -> None:
        """Not used — this agent reads directly from the feed."""
        pass

    # ── Monitoring loop ───────────────────────────────────────────────────────

    async def _monitoring_loop(self) -> None:
        """
        Core 100 ms loop.  Checks all subscribed symbols for anomalies.
        After 3 consecutive clean cycles, marks feed as CLEAN and unblocks
        downstream agents.
        """
        while self._running:
            try:
                await self._run_checks()
            except Exception as exc:
                logger.exception(
                    "[%s] Unexpected error in monitoring loop: %s", self.AGENT_NAME, exc
                )
            await asyncio.sleep(_MONITOR_INTERVAL_SEC)

    async def _run_checks(self) -> None:
        """Run all checks for the current cycle and update feed state."""
        symbols = list(self._data_feed.subscribed_symbols)
        if not symbols:
            # No symbols yet — wait for feed to have subscriptions
            self._consecutive_clean_checks = 0
            return

        anomalies: list[FeedAnomaly] = []
        affected_symbols: list[str] = []
        details_parts: list[str] = []

        for symbol in symbols:
            sym_anomalies, sym_details = self._check_symbol(symbol)
            if sym_anomalies:
                anomalies.extend(sym_anomalies)
                affected_symbols.append(symbol)
                details_parts.extend(sym_details)

        # Also check for sequence gaps across the latest batch of ticks
        seq_anomalies, seq_details, seq_symbols = self._check_sequence_gaps(symbols)
        if seq_anomalies:
            anomalies.extend(seq_anomalies)
            affected_symbols.extend(seq_symbols)
            details_parts.extend(seq_details)

        # Remove duplicates while preserving order
        seen: set[str] = set()
        affected_symbols = [
            s for s in affected_symbols if not (s in seen or seen.add(s))  # type: ignore[func-returns-value]
        ]

        if anomalies:
            self._consecutive_clean_checks = 0

            if self._feed_clean:
                # Transition to degraded
                self._feed_clean = False
                await self._handle_anomaly_detected(anomalies, affected_symbols, details_parts)
            else:
                # Already in degraded/halted — log but don't flood the bus
                logger.debug(
                    "[%s] Ongoing anomalies: %s on %s",
                    self.AGENT_NAME,
                    [a.value for a in anomalies],
                    affected_symbols,
                )
                # Still record to history
                self._record_anomaly(anomalies, affected_symbols, details_parts)
        else:
            self._consecutive_clean_checks += 1
            if self._consecutive_clean_checks >= _CLEAN_CONSECUTIVE_REQUIRED:
                if not self._feed_clean:
                    self._feed_clean = True
                    await self._handle_feed_clean()

    def _check_symbol(
        self, symbol: str
    ) -> tuple[list[FeedAnomaly], list[str]]:
        """
        Run per-symbol checks:
          - Stale tick (no update in > max_tick_staleness_sec)
          - Crossed market (bid >= ask)
          - Missing L2 (order book snapshot too old)

        Returns (anomalies, detail_strings).
        """
        anomalies: list[FeedAnomaly] = []
        details: list[str] = []

        max_staleness = self.cfg(
            "data_integrity", "max_tick_staleness_sec", default=5.0
        )
        max_l2_age = self.cfg(
            "data_integrity", "l2_snapshot_max_age_sec", default=10.0
        )

        tick = self._data_feed.get_latest_tick(symbol)

        # ── Stale tick ────────────────────────────────────────────────────────
        if tick is None:
            anomalies.append(FeedAnomaly.STALE_TICK)
            details.append(f"{symbol}: no tick received yet (stale)")
        else:
            staleness = (datetime.utcnow() - tick.timestamp).total_seconds()
            if staleness > max_staleness:
                anomalies.append(FeedAnomaly.STALE_TICK)
                details.append(
                    f"{symbol}: tick stale for {staleness:.1f}s > {max_staleness}s"
                )
            # ── Crossed market ────────────────────────────────────────────────
            if tick.bid > 0 and tick.ask > 0 and tick.bid >= tick.ask:
                anomalies.append(FeedAnomaly.CROSSED_MARKET)
                details.append(
                    f"{symbol}: crossed market bid={tick.bid} >= ask={tick.ask}"
                )

        # ── Missing L2 ────────────────────────────────────────────────────────
        if self._order_book.is_stale(symbol, max_age_sec=max_l2_age):
            last_updated = self._order_book.get_last_updated(symbol)
            if last_updated is None:
                details.append(f"{symbol}: L2 never received")
            else:
                age = (datetime.utcnow() - last_updated).total_seconds()
                details.append(f"{symbol}: L2 stale for {age:.1f}s > {max_l2_age}s")
            anomalies.append(FeedAnomaly.MISSING_L2)

        return anomalies, details

    def _check_sequence_gaps(
        self, symbols: list[str]
    ) -> tuple[list[FeedAnomaly], list[str], list[str]]:
        """
        Check that tick sequence numbers increment monotonically for each symbol.
        Returns (anomalies, details, affected_symbols).
        """
        anomalies: list[FeedAnomaly] = []
        details: list[str] = []
        affected: list[str] = []

        for symbol in symbols:
            tick = self._data_feed.get_latest_tick(symbol)
            if tick is None:
                continue

            last_seq = self._last_sequence.get(symbol)
            current_seq = tick.sequence

            if last_seq is not None:
                # Sequence must increment by exactly 1; allow equality (duplicate)
                if current_seq > last_seq + 1:
                    anomalies.append(FeedAnomaly.OUT_OF_SEQUENCE)
                    details.append(
                        f"{symbol}: sequence gap {last_seq} -> {current_seq} "
                        f"(missing {current_seq - last_seq - 1} ticks)"
                    )
                    affected.append(symbol)

            # Update last seen sequence regardless
            self._last_sequence[symbol] = current_seq

        return anomalies, details, affected

    # ── State transition handlers ─────────────────────────────────────────────

    async def _handle_anomaly_detected(
        self,
        anomalies: list[FeedAnomaly],
        affected_symbols: list[str],
        details: list[str],
    ) -> None:
        """React to newly detected anomalies: publish DEGRADED/HALTED, alert PSA."""
        self._record_anomaly(anomalies, affected_symbols, details)

        # Determine severity: HALTED if crossed market or sequence gap (worse),
        # DEGRADED for stale tick or missing L2 alone
        critical = {FeedAnomaly.CROSSED_MARKET, FeedAnomaly.OUT_OF_SEQUENCE}
        has_critical = any(a in critical for a in anomalies)
        status = FeedStatus.HALTED if has_critical else FeedStatus.DEGRADED

        details_str = "; ".join(details[:10])  # cap for readability

        logger.warning(
            "[%s] Feed anomaly detected — status=%s, anomalies=%s, symbols=%s",
            self.AGENT_NAME,
            status.value,
            [a.value for a in anomalies],
            affected_symbols,
        )

        await self._publish_feed_status(
            status=status,
            anomalies=anomalies,
            affected_symbols=affected_symbols,
            details=details_str,
        )

        await self.publish(
            topic=Topic.PSA_ALERT,
            payload={
                "alert_type": "FEED_ANOMALY",
                "status": status.value,
                "anomalies": [a.value for a in anomalies],
                "affected_symbols": affected_symbols,
                "details": details_str,
                "agent": self.AGENT_ID,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

        await self.audit.record(AuditEvent(
            event_type="FEED_ANOMALY",
            agent=self.AGENT_ID,
            details={
                "status": status.value,
                "anomalies": [a.value for a in anomalies],
                "affected_symbols": affected_symbols,
                "details": details_str,
            },
        ))

    async def _handle_feed_clean(self) -> None:
        """Feed has passed N consecutive clean checks — unblock MiSA."""
        logger.info(
            "[%s] Feed CLEAN after %d consecutive clean checks — unblocking MiSA",
            self.AGENT_NAME,
            _CLEAN_CONSECUTIVE_REQUIRED,
        )
        await self._publish_feed_status(
            status=FeedStatus.CLEAN,
            anomalies=[],
            affected_symbols=[],
            details=f"Feed passed {_CLEAN_CONSECUTIVE_REQUIRED} consecutive integrity checks",
        )
        await self.audit.record(AuditEvent(
            event_type="FEED_CLEAN",
            agent=self.AGENT_ID,
            details={
                "consecutive_clean_checks": self._consecutive_clean_checks,
                "symbols_monitored": list(self._data_feed.subscribed_symbols),
            },
        ))

    # ── Heartbeat loop (feed-status, every 5 seconds) ─────────────────────────

    async def _feed_heartbeat_loop(self) -> None:
        """
        Publish a FeedStatusReport every 5 seconds regardless of state change.
        This keeps downstream agents up to date even when nothing is changing.
        """
        while self._running:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)
            if not self._running:
                break
            try:
                if self._feed_clean:
                    status = FeedStatus.CLEAN
                    details = "Heartbeat — feed clean"
                elif self._consecutive_clean_checks > 0:
                    status = FeedStatus.DEGRADED
                    details = (
                        f"Heartbeat — recovering "
                        f"({self._consecutive_clean_checks}/{_CLEAN_CONSECUTIVE_REQUIRED} checks)"
                    )
                else:
                    status = FeedStatus.DEGRADED
                    details = "Heartbeat — feed degraded or not yet verified"

                await self._publish_feed_status(
                    status=status,
                    anomalies=[],
                    affected_symbols=[],
                    details=details,
                )
            except Exception as exc:
                logger.warning(
                    "[%s] Feed heartbeat publish error: %s", self.AGENT_NAME, exc
                )

    # ── Publishing ────────────────────────────────────────────────────────────

    async def _publish_feed_status(
        self,
        status: FeedStatus,
        anomalies: list[FeedAnomaly],
        affected_symbols: list[str],
        details: str | None,
    ) -> None:
        report = FeedStatusReport(
            status=status,
            anomalies=anomalies,
            affected_symbols=affected_symbols,
            details=details,
            reported_at=datetime.utcnow(),
        )
        await self.publish(
            topic=Topic.FEED_STATUS,
            payload=report.model_dump(mode="json"),
        )

    # ── Anomaly history ───────────────────────────────────────────────────────

    def _record_anomaly(
        self,
        anomalies: list[FeedAnomaly],
        affected_symbols: list[str],
        details: list[str],
    ) -> None:
        self._anomaly_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "anomalies": [a.value for a in anomalies],
            "affected_symbols": affected_symbols,
            "details": details,
        })

    def get_anomaly_history(self) -> list[dict]:
        """Return the full anomaly history for this session (PSRA use)."""
        return list(self._anomaly_history)

    @property
    def feed_clean(self) -> bool:
        """Public accessor so other components can query the feed gate state."""
        return self._feed_clean
