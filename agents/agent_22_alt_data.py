"""
Agent 22 — Alternative Data Ingestion Agent (AltData)

Provides a pluggable alternative data framework that ingests, normalises,
and scores signals from non-traditional sources:

  Supported sources (§ AltDataSource enum):
    - Satellite imagery (store traffic, parking lot occupancy)
    - Credit card transaction data (sales velocity)
    - Shipping manifests (supply chain flow)
    - News NLP sentiment (FinBERT-style scoring)
    - Social media sentiment (Reddit, X/Twitter momentum)
    - Weather data (sector impacts: energy, agriculture)
    - Job postings (hiring velocity → company growth signal)
    - App download rankings (consumer tech signal)
    - Patent filings (innovation pipeline)
    - Earnings whisper numbers (consensus surprise estimation)

Architecture:
  - `AltDataSource` → `AltDataConnector` (pluggable per-source adapter)
  - Each connector implements `fetch(symbols) -> list[AltDataPoint]`
  - `AltDataAgent` aggregates, normalises, and scores across sources
  - Composite signal: weighted average of per-source bull/bear scores
  - Publishes composite signal to bus (Topic.CANDIDATE_SIGNAL enrichment)
  - Subscribes to CLOCK_TICK for scheduled refresh

Signal confidence:
  - Each source has a configurable staleness window and reliability weight
  - Sources with stale data are excluded from composite score
  - INSUFFICIENT_DATA returned if < min_sources signals are fresh

Publishes: enriched AltDataSignal to bus (topic = APPROVED_SETUPS as metadata)
Subscribes: CLOCK_TICK (scheduled refresh), UNIVERSE_UPDATE (update symbol list)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from core.enums import AltDataSignal, AltDataSource, AgentState, Topic
from core.models import BusMessage
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)


# ─── Alt Data Point ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AltDataPoint:
    """
    Single signal from one alternative data source for one symbol.

    Fields:
        source:      Which AltDataSource produced this signal.
        symbol:      Target equity symbol.
        signal:      Directional signal (BULLISH / BEARISH / NEUTRAL / INSUFFICIENT).
        score:       Normalised score in [-1.0, +1.0]. Positive = bullish.
        confidence:  [0.0, 1.0] confidence in the signal.
        staleness:   Seconds since data was collected.
        metadata:    Source-specific structured data.
    """
    source: AltDataSource
    symbol: str
    signal: AltDataSignal
    score: float            # [-1, +1]
    confidence: float       # [0, 1]
    staleness_sec: float    # seconds since data collection
    metadata: dict = field(default_factory=dict)

    @property
    def timestamp(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def is_fresh(self, max_staleness_sec: float = 3600.0) -> bool:
        return self.staleness_sec <= max_staleness_sec

    def to_dict(self) -> dict:
        return {
            "source": self.source.value,
            "symbol": self.symbol,
            "signal": self.signal.value,
            "score": self.score,
            "confidence": self.confidence,
            "staleness_sec": self.staleness_sec,
            "metadata": self.metadata,
        }


# ─── Composite Score ─────────────────────────────────────────────────────────

@dataclass
class CompositeAltSignal:
    """Aggregated alt-data signal across all sources for one symbol."""
    symbol: str
    composite_score: float       # weighted average [-1, +1]
    signal: AltDataSignal        # discretised
    source_count: int            # number of fresh sources contributing
    source_breakdown: list[dict] # per-source scores
    generated_at: float = field(default_factory=time.time)

    @property
    def is_actionable(self) -> bool:
        return self.signal != AltDataSignal.INSUFFICIENT_DATA and self.source_count >= 2

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "composite_score": self.composite_score,
            "signal": self.signal.value,
            "source_count": self.source_count,
            "is_actionable": self.is_actionable,
            "source_breakdown": self.source_breakdown,
        }


# ─── Connector Interface ──────────────────────────────────────────────────────

class AltDataConnector(ABC):
    """Abstract adapter for a single alternative data source."""

    @property
    @abstractmethod
    def source(self) -> AltDataSource:
        ...

    @property
    def max_staleness_sec(self) -> float:
        """Maximum acceptable age of data from this source."""
        return 3600.0   # 1 hour default

    @property
    def weight(self) -> float:
        """Contribution weight in composite score aggregation."""
        return 1.0

    @abstractmethod
    async def fetch(self, symbols: list[str]) -> list[AltDataPoint]:
        """
        Fetch latest signals for the given symbols.
        Must be non-blocking (use asyncio.sleep or executor for I/O).
        """
        ...

    def _make_point(
        self,
        symbol: str,
        score: float,
        confidence: float,
        staleness_sec: float = 0.0,
        metadata: dict | None = None,
    ) -> AltDataPoint:
        """Helper to construct a typed AltDataPoint from numeric score."""
        if abs(score) < 0.1:
            signal = AltDataSignal.NEUTRAL
        elif score > 0:
            signal = AltDataSignal.BULLISH
        else:
            signal = AltDataSignal.BEARISH

        return AltDataPoint(
            source=self.source,
            symbol=symbol,
            signal=signal,
            score=score,
            confidence=confidence,
            staleness_sec=staleness_sec,
            metadata=metadata or {},
        )


# ─── Built-in Connectors (Simulated) ─────────────────────────────────────────

class NewsSentimentConnector(AltDataConnector):
    """
    News NLP sentiment connector.
    In production: calls FinBERT or a vendor API (Bloomberg NLP, Refinitiv).
    Here: returns plausible synthetic sentiment for demonstration.
    """

    @property
    def source(self) -> AltDataSource:
        return AltDataSource.NEWS_NLP

    @property
    def max_staleness_sec(self) -> float:
        return 900.0   # 15 minutes — news is time-sensitive

    @property
    def weight(self) -> float:
        return 1.5     # news is a high-weight signal

    async def fetch(self, symbols: list[str]) -> list[AltDataPoint]:
        # In production: call NLP API endpoint, parse JSON response
        # Here: simulate sentiment using symbol hash for reproducibility
        points = []
        for sym in symbols:
            h = int(hashlib.md5(f"{sym}{int(time.time() // 900)}".encode()).hexdigest(), 16)
            score = (h % 201 - 100) / 100.0 * 0.8   # [-0.8, +0.8]
            confidence = 0.5 + (h % 50) / 100.0
            points.append(self._make_point(sym, score, confidence, staleness_sec=0.0))
        return points


class SocialSentimentConnector(AltDataConnector):
    """Social media (Reddit WallStreetBets, X) sentiment connector."""

    @property
    def source(self) -> AltDataSource:
        return AltDataSource.SOCIAL_SENTIMENT

    @property
    def max_staleness_sec(self) -> float:
        return 1800.0   # 30 minutes

    @property
    def weight(self) -> float:
        return 0.8

    async def fetch(self, symbols: list[str]) -> list[AltDataPoint]:
        points = []
        for sym in symbols:
            h = int(hashlib.md5(f"social_{sym}{int(time.time() // 1800)}".encode()).hexdigest(), 16)
            score = (h % 201 - 100) / 100.0 * 0.6
            confidence = 0.3 + (h % 40) / 100.0   # social is noisier
            points.append(self._make_point(sym, score, confidence, staleness_sec=300.0))
        return points


class EarningsWhisperConnector(AltDataConnector):
    """
    Earnings whisper number connector.
    Signals: consensus surprise direction based on whisper vs street estimate.
    Only active within 72h of an earnings event.
    """

    @property
    def source(self) -> AltDataSource:
        return AltDataSource.EARNINGS_WHISPER

    @property
    def max_staleness_sec(self) -> float:
        return 86400.0   # 24 hours

    @property
    def weight(self) -> float:
        return 2.0   # highest weight — earnings drives biggest moves

    async def fetch(self, symbols: list[str]) -> list[AltDataPoint]:
        points = []
        for sym in symbols:
            h = int(hashlib.md5(f"whisper_{sym}{int(time.time() // 86400)}".encode()).hexdigest(), 16)
            # 60% of symbols have no earnings signal (INSUFFICIENT_DATA)
            if h % 10 < 6:
                points.append(AltDataPoint(
                    source=self.source,
                    symbol=sym,
                    signal=AltDataSignal.INSUFFICIENT_DATA,
                    score=0.0,
                    confidence=0.0,
                    staleness_sec=0.0,
                ))
            else:
                score = (h % 201 - 100) / 100.0
                confidence = 0.7 + (h % 20) / 100.0
                points.append(self._make_point(sym, score, confidence))
        return points


class SatelliteConnector(AltDataConnector):
    """
    Satellite imagery signal (store traffic, parking lot occupancy).
    In production: calls a vendor API (Orbital Insight, Descartes Labs).
    """

    @property
    def source(self) -> AltDataSource:
        return AltDataSource.SATELLITE

    @property
    def max_staleness_sec(self) -> float:
        return 86400.0   # daily updates

    @property
    def weight(self) -> float:
        return 1.2

    async def fetch(self, symbols: list[str]) -> list[AltDataPoint]:
        # Simulate: only retail/consumer symbols get meaningful signals
        retail_symbols = {sym for sym in symbols if len(sym) <= 4}
        points = []
        for sym in symbols:
            if sym not in retail_symbols:
                points.append(AltDataPoint(
                    source=self.source, symbol=sym,
                    signal=AltDataSignal.INSUFFICIENT_DATA,
                    score=0.0, confidence=0.0, staleness_sec=0.0,
                ))
                continue
            h = int(hashlib.md5(f"sat_{sym}{int(time.time() // 86400)}".encode()).hexdigest(), 16)
            score = (h % 141 - 70) / 100.0
            confidence = 0.6
            points.append(self._make_point(sym, score, confidence, staleness_sec=3600.0))
        return points


# ─── Alt Data Agent ───────────────────────────────────────────────────────────

class AltDataAgent:
    """
    Agent 22: Alternative Data Ingestion Agent.

    Orchestrates multiple AltDataConnector instances, aggregates signals,
    and publishes composite scores to the bus for signal enrichment.

    NOT a BaseAgent subclass — operates independently.

    Args:
        bus:               Message bus.
        store:             State store.
        audit:             Audit log.
        connectors:        List of AltDataConnector instances to use.
        symbols:           Initial symbol universe.
        refresh_interval:  Seconds between full refresh cycles (default 300s = 5 min).
        min_sources:       Minimum fresh sources required for actionable signal.
    """

    AGENT_ID = 22
    AGENT_NAME = "alt_data"

    # Default connector set
    DEFAULT_CONNECTORS: list[type] = [
        NewsSentimentConnector,
        SocialSentimentConnector,
        EarningsWhisperConnector,
        SatelliteConnector,
    ]

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        connectors: list[AltDataConnector] | None = None,
        symbols: list[str] | None = None,
        refresh_interval: float = 300.0,
        min_sources: int = 2,
    ) -> None:
        self._bus = bus
        self._store = store
        self._audit = audit
        self._connectors = connectors or [cls() for cls in self.DEFAULT_CONNECTORS]
        self._symbols = symbols or []
        self._refresh_interval = refresh_interval
        self._min_sources = min_sources

        # Latest composite signals per symbol
        self._signals: dict[str, CompositeAltSignal] = {}

        self._running = False
        self._state = AgentState.IDLE
        self._refresh_task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._state = AgentState.PROCESSING
        logger.info(
            "[AltData] Starting with %d connectors, %d symbols",
            len(self._connectors), len(self._symbols),
        )

        await self._bus.subscribe(Topic.UNIVERSE_UPDATE, self._on_universe_update)
        await self._bus.subscribe(Topic.KILL_SWITCH, self._on_kill_switch)

        # Initial refresh
        await self._refresh_all()

        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="altdata_refresh")
        self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="altdata_hb")

    async def stop(self) -> None:
        self._running = False
        for task in (self._refresh_task, self._hb_task):
            if task and not task.done():
                task.cancel()

    # ── Data Refresh ──────────────────────────────────────────────────────────

    async def _refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._refresh_interval)
            if self._running:
                await self._refresh_all()

    async def _refresh_all(self) -> None:
        """Fetch from all connectors and compute composite signals."""
        if not self._symbols:
            return

        # Gather from all connectors in parallel
        tasks = [
            connector.fetch(self._symbols)
            for connector in self._connectors
        ]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Group by symbol
        by_symbol: dict[str, list[AltDataPoint]] = {sym: [] for sym in self._symbols}
        for idx, result in enumerate(all_results):
            if isinstance(result, Exception):
                logger.warning(
                    "[AltData] Connector %s failed: %s",
                    self._connectors[idx].source.value, result,
                )
                continue
            for point in result:
                if point.symbol in by_symbol:
                    by_symbol[point.symbol].append(point)

        # Compute composite score for each symbol
        for symbol, points in by_symbol.items():
            signal = self._aggregate(symbol, points)
            self._signals[symbol] = signal

            if signal.is_actionable:
                await self._bus.publish(BusMessage(
                    topic=Topic.APPROVED_SETUPS,   # used as metadata enrichment
                    payload={
                        "type": "alt_data_signal",
                        "agent": self.AGENT_NAME,
                        **signal.to_dict(),
                    },
                    source_agent=self.AGENT_NAME,
                ))

        logger.debug(
            "[AltData] Refresh complete: %d symbols | %d actionable",
            len(self._symbols),
            sum(1 for s in self._signals.values() if s.is_actionable),
        )

    def _aggregate(self, symbol: str, points: list[AltDataPoint]) -> CompositeAltSignal:
        """
        Compute composite score as a confidence-weighted, source-weighted
        average of fresh signals.
        """
        total_weight = 0.0
        weighted_score = 0.0
        fresh_sources = 0
        breakdown = []

        connector_by_source = {c.source: c for c in self._connectors}

        for point in points:
            if point.signal == AltDataSignal.INSUFFICIENT_DATA:
                continue
            connector = connector_by_source.get(point.source)
            max_stale = connector.max_staleness_sec if connector else 3600.0
            if not point.is_fresh(max_stale):
                continue

            w = (connector.weight if connector else 1.0) * point.confidence
            weighted_score += point.score * w
            total_weight += w
            fresh_sources += 1
            breakdown.append({
                "source": point.source.value,
                "score": point.score,
                "confidence": point.confidence,
                "signal": point.signal.value,
            })

        if fresh_sources < self._min_sources or total_weight == 0:
            return CompositeAltSignal(
                symbol=symbol,
                composite_score=0.0,
                signal=AltDataSignal.INSUFFICIENT_DATA,
                source_count=fresh_sources,
                source_breakdown=breakdown,
            )

        composite = weighted_score / total_weight
        if composite > 0.2:
            agg_signal = AltDataSignal.BULLISH
        elif composite < -0.2:
            agg_signal = AltDataSignal.BEARISH
        else:
            agg_signal = AltDataSignal.NEUTRAL

        return CompositeAltSignal(
            symbol=symbol,
            composite_score=composite,
            signal=agg_signal,
            source_count=fresh_sources,
            source_breakdown=breakdown,
        )

    # ── Event Handlers ────────────────────────────────────────────────────────

    async def _on_universe_update(self, message: BusMessage) -> None:
        new_symbols = message.payload.get("symbols", [])
        if isinstance(new_symbols, list):
            self._symbols = new_symbols
            logger.info("[AltData] Universe updated to %d symbols", len(self._symbols))

    async def _on_kill_switch(self, message: BusMessage) -> None:
        self._running = False

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            await self._bus.publish(BusMessage(
                topic=Topic.AGENT_HEARTBEAT,
                payload={
                    "agent_id": self.AGENT_ID,
                    "agent_name": self.AGENT_NAME,
                    "state": self._state.value,
                    "symbols": len(self._symbols),
                    "connectors": len(self._connectors),
                    "actionable_signals": sum(
                        1 for s in self._signals.values() if s.is_actionable
                    ),
                },
                source_agent=self.AGENT_NAME,
            ))

    # ── Query Interface ───────────────────────────────────────────────────────

    def get_signal(self, symbol: str) -> CompositeAltSignal | None:
        return self._signals.get(symbol)

    def get_all_signals(self) -> dict[str, CompositeAltSignal]:
        return dict(self._signals)

    def add_connector(self, connector: AltDataConnector) -> None:
        """Plug in a new data source at runtime."""
        self._connectors.append(connector)
        logger.info("[AltData] Added connector: %s", connector.source.value)

    def signal_summary(self) -> list[dict]:
        return [
            {
                "symbol": sym,
                "signal": sig.signal.value,
                "score": sig.composite_score,
                "sources": sig.source_count,
                "actionable": sig.is_actionable,
            }
            for sym, sig in self._signals.items()
        ]
