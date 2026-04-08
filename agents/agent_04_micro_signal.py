"""
Agent 4 — Micro-Signal Agent (MiSA)

Detects short-term scalpable setups by continuously scanning approved symbols
for four pattern types: BREAKOUT, VWAP_TAP, LIQUIDITY_SWEEP, MOMENTUM_BURST.

Safety contract:
  - Will NOT emit signals for retired setups.
  - Will NOT emit signals in a no-trade regime.
  - Will NOT emit signals if the data feed is dirty.
  - Will NOT emit signals if spread exceeds max_spread_bps.
  - Will NOT emit signals if volume is below minimum threshold.
  - Will NOT emit signals if an open position already exists in the symbol.
  - Scan loop suspends immediately when feed goes dirty.

Subscribed topics : APPROVED_SETUPS, UNIVERSE_UPDATE, REGIME_UPDATE, FEED_STATUS
Publishes         : CANDIDATE_SIGNAL
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
from typing import Callable

from core.constants import AGENT_IDS
from core.enums import Direction, FeedStatus, SetupType, Topic
from core.models import (
    ApprovedSetupList,
    AuditEvent,
    BusMessage,
    CandidateSignal,
    FeedStatusReport,
    MarketTick,
    RegimeAssessment,
    UniverseSnapshot,
)
from agents.base import BaseAgent
from data.base import DataFeedBase
from data.level2 import OrderBookCache
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

# Minimum bars required before attempting a pattern scan
_MIN_BARS_REQUIRED = 5

# Volume ratio required to call a move a "surge" (relative to recent average)
_VOLUME_SURGE_RATIO = 1.5

# Momentum burst: minimum number of consecutive closes in the same direction
_MOMENTUM_CONSECUTIVE_BARS = 3


class MicroSignalAgent(BaseAgent):
    """
    Agent 4 — Micro-Signal Agent (MiSA).

    Runs a 200 ms scan loop over approved symbols, calls the LLM to assess
    pattern quality for each symbol, and emits CandidateSignal messages when
    all pre-emission gate checks pass.
    """

    # ── Constructor ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        data_feed: DataFeedBase,
        order_book: OrderBookCache,
        open_positions_fn: Callable[[], list[str]],
        agent_id: str | None = None,
        agent_name: str | None = None,
        config: dict | None = None,
        fpga_pipeline: "Any | None" = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id or AGENT_IDS[4],
            agent_name=agent_name or "MicroSignalAgent",
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )
        self._data_feed = data_feed
        self._order_book = order_book
        self._open_positions_fn = open_positions_fn
        self._fpga_pipeline = fpga_pipeline

        # ── Mutable state (updated by bus messages) ──────────────────────────
        self._approved_setups: ApprovedSetupList | None = None
        self._current_universe: UniverseSnapshot | None = None
        self._current_regime: RegimeAssessment | None = None
        self._feed_clean: bool = False

        # Scan loop handle
        self._scan_task: asyncio.Task | None = None

        # Config shortcuts
        self._max_spread_bps: float = float(
            self.cfg("universe", "max_spread_bps", default=20)
        )
        self._min_avg_volume: int = int(
            self.cfg("universe", "min_avg_volume", default=500_000)
        )
        self._scan_interval_sec: float = 0.200  # 200 ms

    # ── Topic subscriptions ───────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [
            Topic.APPROVED_SETUPS,
            Topic.UNIVERSE_UPDATE,
            Topic.REGIME_UPDATE,
            Topic.FEED_STATUS,
        ]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_startup(self) -> None:
        self._scan_task = asyncio.create_task(
            self._scan_loop(), name=f"{self.agent_id}-scan"
        )
        logger.info("[MiSA] Scan loop started (interval=%.0fms)", self._scan_interval_sec * 1000)

    async def on_shutdown(self) -> None:
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        logger.info("[MiSA] Scan loop stopped")

    # ── Message handler (bus → state updates) ────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        topic = message.topic

        if topic == Topic.APPROVED_SETUPS:
            self._approved_setups = ApprovedSetupList(**message.payload)
            logger.debug("[MiSA] Approved setups updated: version=%s", self._approved_setups.version)

        elif topic == Topic.UNIVERSE_UPDATE:
            self._current_universe = UniverseSnapshot(**message.payload)
            logger.debug(
                "[MiSA] Universe updated: %d approved symbols",
                len(self._current_universe.approved_symbols),
            )

        elif topic == Topic.REGIME_UPDATE:
            self._current_regime = RegimeAssessment(**message.payload)
            logger.debug("[MiSA] Regime updated: %s", self._current_regime.label)

        elif topic == Topic.FEED_STATUS:
            report = FeedStatusReport(**message.payload)
            was_clean = self._feed_clean
            self._feed_clean = report.status == FeedStatus.CLEAN
            if was_clean and not self._feed_clean:
                logger.warning("[MiSA] Feed is now DIRTY — scan loop suspended. status=%s", report.status)
            elif not was_clean and self._feed_clean:
                logger.info("[MiSA] Feed restored to CLEAN — scan loop resuming")

    # ── Scan loop ─────────────────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        """
        Main scanning loop. Runs every 200 ms while the agent is alive.
        Suspends scanning when feed is dirty.
        """
        while self._running:
            try:
                await self._run_one_scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error_count += 1
                logger.exception("[MiSA] Unexpected error in scan loop: %s", exc)
            await asyncio.sleep(self._scan_interval_sec)

    async def _run_one_scan(self) -> None:
        """Execute a single pass over all approved symbols."""

        # ── Pre-scan gates ────────────────────────────────────────────────────
        if not self._feed_clean:
            return  # Safety contract: never scan on dirty feed

        if self._current_regime is None:
            return  # No regime data yet

        if not self._current_regime.label.is_tradeable:
            return  # No-trade regime — skip entire scan

        if self._approved_setups is None:
            return  # Edge research not yet published

        if self._current_universe is None:
            return  # Universe not yet available

        approved_symbols = self._current_universe.approved_symbols
        if not approved_symbols:
            return

        open_positions = set(self._open_positions_fn())

        for symbol in approved_symbols:
            if not self._running:
                break
            try:
                await self._scan_symbol(symbol, open_positions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[MiSA] scan_symbol error for %s: %s", symbol, exc)

    async def _scan_symbol(self, symbol: str, open_positions: set[str]) -> None:
        """
        Check a single symbol for all four pattern types.
        Emits at most one CandidateSignal per symbol per scan cycle
        (the highest-confidence pattern detected).
        """
        # ── Position gate ─────────────────────────────────────────────────────
        if symbol in open_positions:
            return

        # ── Data availability ─────────────────────────────────────────────────
        tick = self._data_feed.get_latest_tick(symbol)
        if tick is None:
            return

        bars = self._data_feed.get_bar_history(symbol, n=20)
        if len(bars) < _MIN_BARS_REQUIRED:
            return

        # ── Spread gate ───────────────────────────────────────────────────────
        if tick.spread_bps >= self._max_spread_bps:
            return

        # ── Volume gate (use last bar volume vs 10-bar average) ───────────────
        recent_volumes = [b.volume for b in bars[-10:]]
        if not recent_volumes:
            return
        avg_volume = statistics.mean(recent_volumes)
        if avg_volume < self._min_avg_volume / 390:  # intraday per-bar floor
            return

        # ── Pattern detection ─────────────────────────────────────────────────
        detection = await self._detect_pattern(symbol, tick, bars)
        if detection is None:
            return

        setup_type, direction, entry, stop, target, llm_reasoning = detection

        # ── Approved-setup gate ───────────────────────────────────────────────
        if not self._approved_setups.is_approved(setup_type):
            logger.debug("[MiSA] %s: setup %s is retired/not approved", symbol, setup_type)
            return

        # ── Regime gate ───────────────────────────────────────────────────────
        if setup_type not in self._current_regime.allowed_setup_types:
            logger.debug(
                "[MiSA] %s: setup %s not in regime.allowed_setup_types (%s)",
                symbol, setup_type, self._current_regime.label,
            )
            return

        # ── Emit CandidateSignal ──────────────────────────────────────────────
        signal = CandidateSignal(
            symbol=symbol,
            setup_type=setup_type,
            direction=direction,
            entry_price=entry,
            initial_stop=stop,
            initial_target=target,
            regime=self._current_regime.label,
            tick_at_signal=tick,
            reasoning=llm_reasoning,
        )

        await self.publish(
            Topic.CANDIDATE_SIGNAL,
            signal.model_dump(mode="json"),
            correlation_id=signal.signal_id,
        )

        await self.audit.record(AuditEvent(
            event_type="CANDIDATE_SIGNAL_EMITTED",
            agent=self.agent_id,
            symbol=symbol,
            details={
                "signal_id": signal.signal_id,
                "setup_type": setup_type.value,
                "direction": direction.value,
                "entry_price": entry,
                "initial_stop": stop,
                "initial_target": target,
                "regime": self._current_regime.label.value,
                "spread_bps": tick.spread_bps,
            },
        ))

        logger.info(
            "[MiSA] Signal emitted: %s %s %s @ %.2f | stop=%.2f target=%.2f",
            symbol, setup_type.value, direction.value, entry, stop, target,
        )

    # ── Pattern detection (LLM-assisted) ─────────────────────────────────────

    async def _detect_pattern(
        self,
        symbol: str,
        tick: MarketTick,
        bars: list,
    ) -> tuple[SetupType, Direction, float, float, float, str] | None:
        """
        Ask the LLM to analyse recent price action and identify the highest-
        confidence scalpable pattern from the four approved types.

        Returns (setup_type, direction, entry, stop, target, reasoning) or None.
        """
        # Build a compact market snapshot for the LLM
        bar_summary = [
            {
                "t": b.timestamp.isoformat(),
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "v": b.volume,
                "vwap": b.vwap,
            }
            for b in bars[-10:]
        ]
        tick_summary = {
            "bid": tick.bid,
            "ask": tick.ask,
            "last": tick.last,
            "spread_bps": round(tick.spread_bps, 2),
            "volume": tick.volume,
        }

        # L2 sweep check — performed locally so LLM has the result
        book = self._order_book.get_book(symbol)
        bid_sweep = self._order_book.detect_liquidity_sweep(symbol, "bid") if book else False
        ask_sweep = self._order_book.detect_liquidity_sweep(symbol, "ask") if book else False

        system_prompt = (
            "You are MiSA, the Micro-Signal Agent for an autonomous equity day-trading system. "
            "Your role is to detect short-term scalpable setups from real-time market data. "
            "You must respond with valid JSON only — no markdown, no prose outside the JSON object. "
            "Be precise and conservative. If no credible pattern is present, return null."
        )

        user_message = f"""
Analyse the following market data for {symbol} and identify the single highest-confidence
scalpable pattern, if one exists.

Approved pattern types (and detection rules):
1. BREAKOUT      — price breaks above/below a key level on a volume surge (>={_VOLUME_SURGE_RATIO}x recent avg)
2. VWAP_TAP      — price touches VWAP with clear momentum in the primary direction
3. LIQUIDITY_SWEEP — large order consumed multiple L2 levels (L2 sweep signals provided below)
4. MOMENTUM_BURST  — {_MOMENTUM_CONSECUTIVE_BARS}+ consecutive closes in one direction with expanding volume

Current tick:
{json.dumps(tick_summary)}

Last 10 1-minute bars (oldest first):
{json.dumps(bar_summary)}

L2 sweep signals:
  bid_side_swept (thin bid book — selling pressure): {bid_sweep}
  ask_side_swept (thin ask book — buying pressure): {ask_sweep}

Return ONLY valid JSON in exactly this schema:
{{
  "pattern_found": true | false,
  "setup_type": "breakout" | "vwap_tap" | "liquidity_sweep" | "momentum_burst" | null,
  "direction": "long" | "short" | null,
  "entry_price": <float> | null,
  "stop_price": <float> | null,
  "target_price": <float> | null,
  "confidence": "high" | "medium" | "low" | null,
  "reasoning": "<concise explanation, max 120 words>"
}}

Rules for stop and target:
- Stop must be below a key structural level (for long) or above it (for short).
- Target must be at least 2.0x the distance from entry to stop (2:1 R:R minimum).
- If confidence is "low", set pattern_found to false.
- If pattern_found is false, all other fields (except reasoning) must be null.
"""

        try:
            result = await self.llm_call_json(system_prompt, user_message)
        except Exception as exc:
            logger.warning("[MiSA] LLM call failed for %s: %s", symbol, exc)
            return None

        if not result.get("pattern_found"):
            return None

        # Validate required fields returned by LLM
        required = ["setup_type", "direction", "entry_price", "stop_price", "target_price"]
        if any(result.get(k) is None for k in required):
            logger.debug("[MiSA] %s: LLM returned incomplete signal, skipping", symbol)
            return None

        try:
            setup_type = SetupType(result["setup_type"])
            direction = Direction(result["direction"])
            entry = float(result["entry_price"])
            stop = float(result["stop_price"])
            target = float(result["target_price"])
        except (ValueError, KeyError) as exc:
            logger.warning("[MiSA] %s: LLM response parse error: %s", symbol, exc)
            return None

        # Validate 2:1 R:R floor
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0 or reward / risk < 2.0:
            logger.debug(
                "[MiSA] %s: R:R %.2f below 2.0 floor — discarding",
                symbol, reward / risk if risk > 0 else 0,
            )
            return None

        reasoning: str = result.get("reasoning", "")
        return setup_type, direction, entry, stop, target, reasoning
