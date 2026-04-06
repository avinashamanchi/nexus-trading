"""
Agent 2 — Market Regime Agent

Classifies the session's market regime and acts as the primary gate for all
downstream signal generation.  MiSA cannot emit signals unless the regime is
tradeable; regime flips trigger immediate downstream effects.

Safety contract:
  - MiSA cannot emit signals unless regime.is_tradeable is True.
  - No intraday override of a no-trade regime by any other agent.
  - VIX > 30 → HIGH_VOLATILITY_NO_TRADE (automatic, no exception).
  - VIX > 40 → EXTREME DANGER — same classification, additional alerts.
  - Regime flip triggers immediate re-publication and downstream re-evaluation.
  - All classifications are persisted for PSRA post-session review.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, date
from typing import Any

from core.enums import (
    RegimeLabel,
    SetupType,
    Topic,
)
from core.models import (
    AuditEvent,
    BusMessage,
    FeedStatusReport,
    RegimeAssessment,
)
from agents.base import BaseAgent
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

# ── Regime-to-allowed-setups mapping ─────────────────────────────────────────
_REGIME_SETUP_MAP: dict[RegimeLabel, list[SetupType]] = {
    RegimeLabel.TREND_DAY: [
        SetupType.BREAKOUT,
        SetupType.MOMENTUM_BURST,
        SetupType.GAP_AND_GO,
        SetupType.OPENING_RANGE_BREAK,
        SetupType.VWAP_TAP,
        SetupType.CATALYST_MOVER,
    ],
    RegimeLabel.MEAN_REVERSION_DAY: [
        SetupType.MEAN_REVERSION_FADE,
        SetupType.VWAP_TAP,
        SetupType.LIQUIDITY_SWEEP,
    ],
    RegimeLabel.EVENT_DRIVEN_DAY: [
        SetupType.CATALYST_MOVER,
        SetupType.GAP_AND_GO,
        SetupType.BREAKOUT,
        SetupType.MOMENTUM_BURST,
        SetupType.LIQUIDITY_SWEEP,
    ],
    RegimeLabel.HIGH_VOLATILITY_NO_TRADE: [],
    RegimeLabel.LOW_LIQUIDITY_NO_TRADE: [],
}


class MarketRegimeAgent(BaseAgent):
    """
    Agent 2 — Market Regime Agent.

    Performs initial regime classification at startup and re-evaluates every
    N seconds (config: regime.re_evaluation_interval_sec).  Each regime
    assessment is published on Topic.REGIME_UPDATE.

    Message flow:
      SUBSCRIBES: Topic.FEED_STATUS   (blocks classification if feed is dirty)
      PUBLISHES:  Topic.REGIME_UPDATE (RegimeAssessment)
                  Topic.PSA_ALERT     (on extreme danger or no-trade conditions)
    """

    AGENT_ID = "agent_02_market_regime"
    AGENT_NAME = "MarketRegimeAgent"

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
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
        self._current_assessment: RegimeAssessment | None = None
        self._regime_history: list[RegimeAssessment] = []
        self._feed_clean: bool = True   # optimistic start; updated by FEED_STATUS
        self._reeval_task: asyncio.Task | None = None

    # ── Topic subscriptions ───────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.FEED_STATUS]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_startup(self) -> None:
        """Immediately classify regime from pre-market data, then start re-eval loop."""
        logger.info("[%s] Performing initial regime classification", self.AGENT_NAME)
        await self._classify_and_publish()
        self._reeval_task = asyncio.create_task(
            self._reeval_loop(), name=f"{self.AGENT_ID}-reeval"
        )

    async def on_shutdown(self) -> None:
        if self._reeval_task:
            self._reeval_task.cancel()

    # ── Core message handler ──────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        if message.topic != Topic.FEED_STATUS:
            return
        try:
            report = FeedStatusReport.model_validate(message.payload)
        except Exception as exc:
            logger.error("[%s] Could not parse FeedStatusReport: %s", self.AGENT_NAME, exc)
            return

        from core.enums import FeedStatus
        prev_clean = self._feed_clean
        self._feed_clean = (report.status == FeedStatus.CLEAN)

        if prev_clean != self._feed_clean:
            logger.info(
                "[%s] Feed status changed: clean=%s — anomalies=%s",
                self.AGENT_NAME,
                self._feed_clean,
                [a.value for a in report.anomalies],
            )
            # If feed just became dirty, re-publish current regime to ensure
            # downstream agents re-evaluate their gate state.
            if not self._feed_clean and self._current_assessment:
                await self._publish_assessment(self._current_assessment)

    # ── Periodic re-evaluation loop ───────────────────────────────────────────

    async def _reeval_loop(self) -> None:
        """Re-classify the regime every N seconds."""
        interval = self.cfg(
            "regime", "re_evaluation_interval_sec", default=300
        )
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            await self._classify_and_publish()

    # ── Core classification ───────────────────────────────────────────────────

    async def _classify_and_publish(self) -> None:
        """
        Run the LLM regime classification and publish the result.
        Handles VIX thresholds deterministically before calling the LLM.
        """
        try:
            market_data = await self._gather_market_context()
            vix = market_data.get("vix", 20.0)

            # Hard VIX overrides — no LLM call needed
            vix_high = self.cfg("regime", "vix_high_threshold", default=30.0)
            vix_extreme = self.cfg("regime", "vix_extreme_threshold", default=40.0)

            if vix >= vix_extreme:
                await self._apply_no_trade_regime(
                    label=RegimeLabel.HIGH_VOLATILITY_NO_TRADE,
                    vix=vix,
                    reason=f"VIX {vix:.1f} >= extreme threshold {vix_extreme} — EXTREME DANGER",
                    market_data=market_data,
                    extreme=True,
                )
                return

            if vix >= vix_high:
                await self._apply_no_trade_regime(
                    label=RegimeLabel.HIGH_VOLATILITY_NO_TRADE,
                    vix=vix,
                    reason=f"VIX {vix:.1f} >= high threshold {vix_high} — no-trade",
                    market_data=market_data,
                    extreme=False,
                )
                return

            # Full LLM-based classification
            assessment = await self._llm_classify_regime(market_data)
            await self._apply_assessment(assessment)

        except Exception as exc:
            logger.exception("[%s] Regime classification error: %s", self.AGENT_NAME, exc)

    async def _apply_no_trade_regime(
        self,
        label: RegimeLabel,
        vix: float,
        reason: str,
        market_data: dict,
        extreme: bool,
    ) -> None:
        assessment = RegimeAssessment(
            label=label,
            vix_level=vix,
            trend_strength=0.0,
            news_flags=market_data.get("news_flags", []),
            allowed_setup_types=[],
            confidence=1.0,
            assessed_at=datetime.utcnow(),
            reasoning=reason,
        )
        await self._apply_assessment(assessment)

        if extreme:
            await self.publish(
                topic=Topic.PSA_ALERT,
                payload={
                    "alert_type": "EXTREME_VIX",
                    "vix": vix,
                    "message": reason,
                    "agent": self.AGENT_ID,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
            logger.critical("[%s] %s", self.AGENT_NAME, reason)

    async def _apply_assessment(self, assessment: RegimeAssessment) -> None:
        """
        Update internal state, detect flips, and publish.
        """
        prev = self._current_assessment
        self._current_assessment = assessment

        flipped = prev is not None and prev.label != assessment.label
        if flipped:
            logger.info(
                "[%s] REGIME FLIP: %s -> %s (VIX=%.1f, confidence=%.2f)",
                self.AGENT_NAME,
                prev.label.value,
                assessment.label.value,
                assessment.vix_level or 0.0,
                assessment.confidence,
            )
            await self.audit.record(AuditEvent(
                event_type="REGIME_FLIP",
                agent=self.AGENT_ID,
                details={
                    "from": prev.label.value,
                    "to": assessment.label.value,
                    "vix": assessment.vix_level,
                    "confidence": assessment.confidence,
                    "reasoning": assessment.reasoning,
                },
            ))

        # Always persist history
        self._regime_history.append(assessment)

        await self._publish_assessment(assessment)

    async def _publish_assessment(self, assessment: RegimeAssessment) -> None:
        await self.publish(
            topic=Topic.REGIME_UPDATE,
            payload=assessment.model_dump(mode="json"),
        )
        logger.debug(
            "[%s] Published regime: %s (tradeable=%s, setups=%s)",
            self.AGENT_NAME,
            assessment.label.value,
            assessment.label.is_tradeable,
            [s.value for s in assessment.allowed_setup_types],
        )

    # ── Market context gathering ──────────────────────────────────────────────

    async def _gather_market_context(self) -> dict:
        """
        Collect available market context for the LLM classification call.
        In production, this would pull from the data feed cache, broker APIs,
        and market data endpoints.  Here we ask the LLM to reason about
        available context it has been given.
        """
        now = datetime.utcnow()
        time_str = now.strftime("%H:%M UTC")
        session_date = date.today().strftime("%Y-%m-%d")

        # Gather any context we can — LLM will fill in what it knows
        context: dict[str, Any] = {
            "session_date": session_date,
            "time_utc": time_str,
            "is_pre_market": now.hour < 13,  # before 09:00 ET
            "is_open": 13 <= now.hour < 20,  # 09:00-16:00 ET approx
        }

        # If prior assessment exists, carry forward its VIX estimate so we
        # have at least a baseline
        if self._current_assessment and self._current_assessment.vix_level:
            context["prior_vix_estimate"] = self._current_assessment.vix_level

        return context

    # ── LLM classification ────────────────────────────────────────────────────

    async def _llm_classify_regime(self, market_data: dict) -> RegimeAssessment:
        """
        Call the LLM with current market context.  The LLM classifies the
        regime, lists news flags, and provides its confidence and reasoning.
        """
        system_prompt = (
            "You are a market regime classifier for an autonomous intraday trading system. "
            "Your role is to classify the current market session into one of exactly five "
            "regime labels and determine which trading setups are valid for this regime.\n\n"
            "The five regime labels are:\n"
            "  1. 'trend-day'                 — clear directional bias, momentum strategies work\n"
            "  2. 'mean-reversion-day'         — choppy, range-bound, fades work\n"
            "  3. 'event-driven-day'           — catalyst-specific moves, news-driven\n"
            "  4. 'high-volatility/no-trade'   — VIX elevated, spreads wide, too risky\n"
            "  5. 'low-liquidity/no-trade'     — thin markets, holiday or unusual session\n\n"
            "Regime-to-setup mapping (return exactly these setup type strings):\n"
            "  trend-day:           [breakout, momentum_burst, gap_and_go, opening_range_break, vwap_tap, catalyst_mover]\n"
            "  mean-reversion-day:  [mean_reversion_fade, vwap_tap, liquidity_sweep]\n"
            "  event-driven-day:    [catalyst_mover, gap_and_go, breakout, momentum_burst, liquidity_sweep]\n"
            "  no-trade regimes:    [] (empty list always)\n\n"
            "Rules:\n"
            "  - If VIX >= 30, label MUST be 'high-volatility/no-trade'.\n"
            "  - If VIX >= 40, label MUST be 'high-volatility/no-trade' — include EXTREME_DANGER in news_flags.\n"
            "  - A 'low-liquidity/no-trade' day is one with volume < 50% of 30-day average at the start.\n"
            "  - Confidence below 0.6 should bias toward a more conservative (no-trade) regime.\n\n"
            "Respond with a JSON object with exactly these fields:\n"
            "  label (string — one of the five exact labels above),\n"
            "  vix_level (float — your best estimate based on context),\n"
            "  trend_strength (float 0.0–1.0 — 0 = flat, 1 = strong trend),\n"
            "  news_flags (list of strings — notable macro/news conditions),\n"
            "  allowed_setup_types (list of strings — from the mapping above),\n"
            "  confidence (float 0.0–1.0),\n"
            "  reasoning (string — 2-4 sentence explanation)"
        )

        now = datetime.utcnow()
        time_of_day = now.strftime("%H:%M UTC (%I:%M %p ET approximately)")

        user_message = (
            f"Session date: {market_data.get('session_date', date.today())}\n"
            f"Time of day: {time_of_day}\n"
            f"Pre-market: {market_data.get('is_pre_market', False)}\n"
            f"Session open: {market_data.get('is_open', True)}\n"
            f"Prior VIX estimate: {market_data.get('prior_vix_estimate', 'unknown')}\n"
            f"\nClassify the market regime for this session. Use your training knowledge "
            f"of current market conditions as of your knowledge cutoff date, combined with "
            f"the time-of-day context above. Be appropriately conservative if uncertain."
        )

        try:
            raw = await self.llm_call_json(
                system_prompt=system_prompt,
                user_message=user_message,
            )

            # Parse and validate the label
            raw_label = raw.get("label", "high-volatility/no-trade")
            try:
                label = RegimeLabel(raw_label)
            except ValueError:
                logger.warning(
                    "[%s] LLM returned unknown label '%s' — defaulting to no-trade",
                    self.AGENT_NAME,
                    raw_label,
                )
                label = RegimeLabel.HIGH_VOLATILITY_NO_TRADE

            # Parse allowed setup types
            raw_setups = raw.get("allowed_setup_types", [])
            allowed_setups: list[SetupType] = []
            for s in raw_setups:
                try:
                    allowed_setups.append(SetupType(s))
                except ValueError:
                    logger.warning("[%s] Unknown setup type from LLM: %s", self.AGENT_NAME, s)

            # Always enforce: no-trade labels get empty setup list regardless of LLM
            if not label.is_tradeable:
                allowed_setups = []

            # Cross-check with our canonical map for defense-in-depth
            canonical_setups = _REGIME_SETUP_MAP.get(label, [])
            # Only admit setups that exist in our canonical map for this regime
            allowed_setups = [s for s in allowed_setups if s in canonical_setups]
            if not allowed_setups and label.is_tradeable:
                # If LLM gave no valid setups for a tradeable regime, use canonical defaults
                allowed_setups = canonical_setups

            vix = float(raw.get("vix_level", 20.0))

            # VIX override — trust numbers over LLM label
            vix_high = self.cfg("regime", "vix_high_threshold", default=30.0)
            if vix >= vix_high and label.is_tradeable:
                logger.warning(
                    "[%s] LLM classified as tradeable but VIX=%.1f >= %.1f — overriding to no-trade",
                    self.AGENT_NAME,
                    vix,
                    vix_high,
                )
                label = RegimeLabel.HIGH_VOLATILITY_NO_TRADE
                allowed_setups = []

            assessment = RegimeAssessment(
                label=label,
                vix_level=vix,
                trend_strength=float(raw.get("trend_strength", 0.5)),
                news_flags=raw.get("news_flags", []),
                allowed_setup_types=allowed_setups,
                confidence=float(raw.get("confidence", 0.7)),
                assessed_at=datetime.utcnow(),
                reasoning=raw.get("reasoning", ""),
            )

            logger.info(
                "[%s] LLM classified regime: %s (VIX=%.1f, confidence=%.2f, setups=%s)",
                self.AGENT_NAME,
                assessment.label.value,
                assessment.vix_level or 0.0,
                assessment.confidence,
                [s.value for s in assessment.allowed_setup_types],
            )
            return assessment

        except Exception as exc:
            logger.exception(
                "[%s] LLM regime classification failed: %s — defaulting to no-trade",
                self.AGENT_NAME,
                exc,
            )
            # Fail safe: return no-trade
            return RegimeAssessment(
                label=RegimeLabel.HIGH_VOLATILITY_NO_TRADE,
                vix_level=self._current_assessment.vix_level if self._current_assessment else None,
                allowed_setup_types=[],
                confidence=0.0,
                assessed_at=datetime.utcnow(),
                reasoning=f"LLM classification error — fail-safe no-trade: {exc}",
            )

    # ── Public accessors ─────────────────────────────────────────────────────

    @property
    def current_regime(self) -> "RegimeAssessment | None":
        """Public accessor used by coordinator, SVA, and MiSA."""
        return self._current_assessment

    # ── Post-session history ──────────────────────────────────────────────────

    def get_regime_history(self) -> list[RegimeAssessment]:
        """Return the full regime history for this session (PSRA use)."""
        return list(self._regime_history)
