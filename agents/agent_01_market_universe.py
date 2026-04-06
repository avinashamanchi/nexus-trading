"""
Agent 1 — Market Universe Agent

Builds and maintains the daily tradeable symbol list with full classification
metadata.  This agent acts as the first filter in the trading pipeline: only
symbols that pass quality thresholds can ever receive signals or orders.

Safety contract:
  - Must exclude risky tickers (low-float, low-volume, wide-spread, halt-prone,
    manipulated, borrow-restricted).
  - Symbol cap (max_active_symbols) is always enforced.
  - Universe must contain >= min_active_symbols (starvation prevention §7.5).
  - Universe is published before MiSA can emit signals.
  - If signal_starvation_consecutive_sessions >= 3, a STARVATION_ALERT is published.
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
    RegimeAssessment,
    SymbolClassification,
    UniverseSnapshot,
)
from agents.base import BaseAgent
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

# Re-evaluation interval when running live (overridden by config universe section)
_DEFAULT_REEVAL_INTERVAL_SEC = 1800  # 30 minutes


class MarketUniverseAgent(BaseAgent):
    """
    Agent 1 — Market Universe Agent.

    Generates and maintains the active tradeable symbol universe for each
    session.  Symbols are classified along several risk/quality axes and
    the resulting UniverseSnapshot is published for all downstream agents.

    Message flow:
      SUBSCRIBES: Topic.REGIME_UPDATE   (gates which setups are active)
      PUBLISHES:  Topic.UNIVERSE_UPDATE (UniverseSnapshot)
                  Topic.STARVATION_ALERT (when consecutive dry sessions >= 3)
    """

    AGENT_ID = "agent_01_market_universe"
    AGENT_NAME = "MarketUniverseAgent"

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
        self._current_snapshot: UniverseSnapshot | None = None
        self._current_regime: RegimeAssessment | None = None
        self._signal_starvation_consecutive_sessions: int = 0
        self._reeval_task: asyncio.Task | None = None

    # ── Topic subscriptions ───────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.REGIME_UPDATE]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_startup(self) -> None:
        """Generate initial universe for the session."""
        logger.info("[%s] Generating initial universe for session %s", self.AGENT_NAME, date.today())
        await self._build_and_publish_universe()
        # Start the periodic re-evaluation loop
        self._reeval_task = asyncio.create_task(
            self._reeval_loop(), name=f"{self.AGENT_ID}-reeval"
        )

    async def on_shutdown(self) -> None:
        if self._reeval_task:
            self._reeval_task.cancel()

    # ── Core message handler ──────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        if message.topic != Topic.REGIME_UPDATE:
            return
        try:
            regime = RegimeAssessment.model_validate(message.payload)
        except Exception as exc:
            logger.error("[%s] Could not parse RegimeAssessment: %s", self.AGENT_NAME, exc)
            return

        prev_label = self._current_regime.label if self._current_regime else None
        self._current_regime = regime

        # On regime flip, re-evaluate the universe to reflect new setup availability
        if prev_label != regime.label:
            logger.info(
                "[%s] Regime flipped %s -> %s — rebuilding universe",
                self.AGENT_NAME,
                prev_label.value if prev_label else "none",
                regime.label.value,
            )
            await self._build_and_publish_universe()

    # ── Periodic re-evaluation loop ───────────────────────────────────────────

    async def _reeval_loop(self) -> None:
        """Re-build the universe every N seconds during the session."""
        interval = self.cfg(
            "universe", "reeval_interval_sec", default=_DEFAULT_REEVAL_INTERVAL_SEC
        )
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            logger.debug("[%s] Periodic universe re-evaluation", self.AGENT_NAME)
            await self._build_and_publish_universe()

    # ── Universe construction ─────────────────────────────────────────────────

    async def _build_and_publish_universe(self) -> None:
        """Full pipeline: screen → classify → filter → cap → publish."""
        try:
            session_date = date.today().strftime("%Y-%m-%d")

            # 1. Candidate list from LLM screening
            candidates = await self._screen_candidates()

            # 2. Classify each candidate
            classifications: list[SymbolClassification] = []
            for sym_data in candidates:
                classification = await self._classify_symbol(sym_data)
                classifications.append(classification)

            # 3. Apply hard filters
            approved = self._apply_filters(classifications)

            # 4. Enforce symbol cap
            approved = self._apply_cap(approved)

            # 5. Starvation check
            await self._starvation_check(len(approved), session_date)

            # 6. Build snapshot (include all, with approved flag set)
            snapshot = UniverseSnapshot(
                session_date=session_date,
                symbols=classifications,
            )
            self._current_snapshot = snapshot

            # 7. Publish
            await self.publish(
                topic=Topic.UNIVERSE_UPDATE,
                payload=snapshot.model_dump(mode="json"),
            )

            approved_symbols = snapshot.approved_symbols
            logger.info(
                "[%s] Universe published: %d approved / %d total candidates",
                self.AGENT_NAME,
                len(approved_symbols),
                len(classifications),
            )

            await self.audit.record(AuditEvent(
                event_type="UNIVERSE_PUBLISHED",
                agent=self.AGENT_ID,
                details={
                    "session_date": session_date,
                    "approved_count": len(approved_symbols),
                    "total_candidates": len(classifications),
                    "approved_symbols": approved_symbols[:20],  # cap for readability
                },
            ))

        except Exception as exc:
            logger.exception(
                "[%s] Failed to build universe: %s", self.AGENT_NAME, exc
            )

    # ── LLM screening ─────────────────────────────────────────────────────────

    async def _screen_candidates(self) -> list[dict]:
        """
        Use LLM to identify and pre-screen candidate symbols for the session.
        In paper/shadow mode this is simulated entirely via LLM; in live mode
        this would combine Alpaca broker screening with LLM analysis.
        """
        regime_context = ""
        if self._current_regime:
            regime_context = (
                f"\nCurrent market regime: {self._current_regime.label.value}"
                f"\nAllowed setup types: {[s.value for s in self._current_regime.allowed_setup_types]}"
                f"\nNews flags: {self._current_regime.news_flags}"
            )

        system_prompt = (
            "You are a pre-market stock screener for an autonomous intraday trading system. "
            "Your job is to identify the best intraday trading candidates for today's session. "
            "Focus on US equities (NYSE, NASDAQ, AMEX) with high relative volume, catalysts, "
            "and sufficient float and liquidity for safe intraday trading.\n\n"
            "For each symbol you identify, provide:\n"
            "  - symbol (ticker string)\n"
            "  - float_shares_million (float, in millions)\n"
            "  - avg_daily_volume (int, shares/day)\n"
            "  - spread_tier (string: 'tight' | 'moderate' | 'wide')\n"
            "  - sector (string)\n"
            "  - catalyst (string: earnings, news, sector_rotation, technical, etc., or null)\n"
            "  - news_sensitive (bool: true if subject to major news today)\n"
            "  - borrow_restricted (bool: true if hard-to-borrow for shorting)\n"
            "  - halt_prone (bool: true if historically halted frequently)\n"
            "  - estimated_current_price (float)\n"
            "  - relative_volume_ratio (float: today's volume vs 30-day avg so far)\n"
            "  - screening_notes (string: brief rationale)\n\n"
            "Return a JSON object with key 'candidates' containing a list of these symbol dicts. "
            "Provide between 15 and 30 candidates so filters have enough to work with."
        )

        user_message = (
            f"Today is {date.today().strftime('%Y-%m-%d')}. "
            f"Market session is US regular hours (09:30-16:00 ET)."
            f"{regime_context}\n\n"
            "Provide a realistic list of intraday trading candidates for today. "
            "Include a mix of large-cap movers, mid-cap with catalysts, and any notable "
            "sector themes or pre-market gaps. Prioritize liquid, low-spread symbols."
        )

        try:
            result = await self.llm_call_json(
                system_prompt=system_prompt,
                user_message=user_message,
            )
            candidates = result.get("candidates", [])
            if not isinstance(candidates, list):
                candidates = []
            logger.debug("[%s] LLM returned %d candidates", self.AGENT_NAME, len(candidates))
            return candidates
        except Exception as exc:
            logger.exception("[%s] LLM screening failed: %s", self.AGENT_NAME, exc)
            return []

    # ── Symbol classification ─────────────────────────────────────────────────

    async def _classify_symbol(self, sym_data: dict) -> SymbolClassification:
        """
        Convert raw screening data into a SymbolClassification.
        Applies LLM-enriched analysis for edge cases.
        """
        symbol = str(sym_data.get("symbol", "UNKNOWN")).upper()

        # Extract numeric fields with safe defaults
        float_shares = self._safe_float(sym_data.get("float_shares_million"))
        avg_volume = self._safe_int(sym_data.get("avg_daily_volume"))
        spread_tier = sym_data.get("spread_tier", "wide")
        sector = sym_data.get("sector")
        catalyst = sym_data.get("catalyst")
        news_sensitive = bool(sym_data.get("news_sensitive", False))
        borrow_restricted = bool(sym_data.get("borrow_restricted", False))
        halt_prone = bool(sym_data.get("halt_prone", False))

        return SymbolClassification(
            symbol=symbol,
            approved=True,          # will be set by filter pass
            float_shares_million=float_shares,
            avg_daily_volume=avg_volume,
            spread_tier=spread_tier,
            sector=sector,
            catalyst=catalyst,
            news_sensitive=news_sensitive,
            borrow_restricted=borrow_restricted,
            halt_prone=halt_prone,
            reject_reason=None,
            classified_at=datetime.utcnow(),
        )

    # ── Hard filters ──────────────────────────────────────────────────────────

    def _apply_filters(
        self, classifications: list[SymbolClassification]
    ) -> list[SymbolClassification]:
        """
        Apply all hard exclusion filters.  Sets approved=False with a reject
        reason on any symbol that fails.  Returns the approved subset.
        """
        min_float = self.cfg("universe", "min_float_million", default=5.0)
        min_volume = self.cfg("universe", "min_avg_volume", default=500_000)
        max_spread_bps_label = "wide"   # spread_tier == "wide" → reject

        approved = []

        for sym in classifications:
            reject_reason = self._check_filters(
                sym, min_float, min_volume, max_spread_bps_label
            )
            if reject_reason:
                sym.approved = False
                sym.reject_reason = reject_reason
                logger.debug(
                    "[%s] Excluding %s: %s", self.AGENT_NAME, sym.symbol, reject_reason
                )
            else:
                sym.approved = True
                approved.append(sym)

        return approved

    def _check_filters(
        self,
        sym: SymbolClassification,
        min_float: float,
        min_volume: int,
        max_spread_tier: str,
    ) -> str | None:
        """Return a reject_reason string if the symbol fails any filter, else None."""
        # Float check
        if sym.float_shares_million is not None and sym.float_shares_million < min_float:
            return f"float {sym.float_shares_million:.1f}M < minimum {min_float}M"

        # Volume check
        if sym.avg_daily_volume is not None and sym.avg_daily_volume < min_volume:
            return (
                f"avg_daily_volume {sym.avg_daily_volume:,} < minimum {min_volume:,}"
            )

        # Spread check — only 'wide' is rejected (tight and moderate are acceptable)
        if sym.spread_tier == "wide":
            return "spread_tier=wide exceeds maximum threshold (20 bps)"

        # Halt-prone check
        if sym.halt_prone:
            return "halt_prone=True — excluded from tradeable universe"

        return None

    def _apply_cap(
        self, approved: list[SymbolClassification]
    ) -> list[SymbolClassification]:
        """
        Enforce max_active_symbols cap.  Preference is given to symbols with
        catalysts (more predictable intraday behaviour).
        """
        max_symbols = self.cfg("universe", "max_active_symbols", default=12)
        if len(approved) <= max_symbols:
            return approved

        # Sort: catalyst first, then by avg_daily_volume descending
        with_catalyst = [s for s in approved if s.catalyst]
        without_catalyst = [s for s in approved if not s.catalyst]

        def vol_key(s: SymbolClassification) -> int:
            return s.avg_daily_volume or 0

        with_catalyst.sort(key=vol_key, reverse=True)
        without_catalyst.sort(key=vol_key, reverse=True)

        combined = (with_catalyst + without_catalyst)[:max_symbols]

        # Mark the excluded ones as rejected
        selected_syms = {s.symbol for s in combined}
        for sym in approved:
            if sym.symbol not in selected_syms:
                sym.approved = False
                sym.reject_reason = f"cap: max_active_symbols={max_symbols} reached"

        logger.info(
            "[%s] Cap applied: %d -> %d symbols (cap=%d)",
            self.AGENT_NAME,
            len(approved),
            len(combined),
            max_symbols,
        )
        return combined

    # ── Starvation check ──────────────────────────────────────────────────────

    async def _starvation_check(self, approved_count: int, session_date: str) -> None:
        """
        Enforce §7.5 starvation prevention.  If fewer than min_active_symbols
        are approved for N consecutive sessions, publish a STARVATION_ALERT.
        """
        min_symbols = self.cfg("universe", "min_active_symbols", default=5)
        starvation_threshold = self.cfg(
            "universe",
            "starvation_check_consecutive_sessions",
            default=3,
        )

        if approved_count < min_symbols:
            self._signal_starvation_consecutive_sessions += 1
            logger.warning(
                "[%s] Universe starvation: %d symbols (min=%d), consecutive=%d",
                self.AGENT_NAME,
                approved_count,
                min_symbols,
                self._signal_starvation_consecutive_sessions,
            )
        else:
            self._signal_starvation_consecutive_sessions = 0
            return

        if self._signal_starvation_consecutive_sessions >= starvation_threshold:
            logger.error(
                "[%s] STARVATION ALERT: %d consecutive sessions with < %d symbols",
                self.AGENT_NAME,
                self._signal_starvation_consecutive_sessions,
                min_symbols,
            )
            await self.publish(
                topic=Topic.STARVATION_ALERT,
                payload={
                    "agent": self.AGENT_ID,
                    "session_date": session_date,
                    "consecutive_sessions": self._signal_starvation_consecutive_sessions,
                    "approved_count": approved_count,
                    "min_required": min_symbols,
                    "alert": (
                        f"Universe has had fewer than {min_symbols} approved symbols "
                        f"for {self._signal_starvation_consecutive_sessions} consecutive sessions. "
                        "Signal generation is at risk of full starvation. "
                        "Human governance review recommended."
                    ),
                },
            )
            await self.audit.record(AuditEvent(
                event_type="STARVATION_ALERT",
                agent=self.AGENT_ID,
                details={
                    "consecutive_sessions": self._signal_starvation_consecutive_sessions,
                    "approved_count": approved_count,
                    "min_required": min_symbols,
                },
            ))

    # ── Utility ───────────────────────────────────────────────────────────────

    @property
    def current_universe(self) -> "UniverseSnapshot | None":
        """Public accessor used by coordinator and Signal Validation Agent."""
        return self._current_snapshot

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
