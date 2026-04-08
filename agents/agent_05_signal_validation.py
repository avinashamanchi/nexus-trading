"""
Agent 5 — Signal Validation Agent

Confirms signal quality across five dimensions before a signal is passed to
the risk gate. A single dimension failure causes an outright rejection —
signals are never forwarded with warnings.

Five validation dimensions:
  1. HTF Alignment  — signal direction agrees with the 5-minute trend
  2. Volume Context — signal bar volume > 1.5x average of last 10 bars
  3. Spread Quality — tick.spread_bps < max_spread_bps (re-checked at validation time)
  4. Catalyst Consistency — symbol catalyst is consistent with setup_type
  5. Regime Consistency — setup_type is in regime.allowed_setup_types

Safety contract:
  - A signal that fails ANY dimension is REJECTED, not conditionally forwarded.
  - Statistics are accumulated per-session for PSRA post-session review.

Subscribed topics : CANDIDATE_SIGNAL
Publishes         : VALIDATED_SIGNAL
"""
from __future__ import annotations

import json
import logging
import statistics
from typing import Callable

from core.constants import AGENT_IDS
from core.enums import (
    Direction,
    SetupType,
    Topic,
    ValidationFailReason,
    ValidationResult,
)
from core.models import (
    AuditEvent,
    BusMessage,
    CandidateSignal,
    RegimeAssessment,
    UniverseSnapshot,
    ValidatedSignal,
)
from agents.base import BaseAgent
from data.base import DataFeedBase
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

# Catalyst keywords that imply event-driven setups are appropriate
_EVENT_DRIVEN_CATALYSTS = frozenset({"earnings", "news", "fda", "merger", "ipo", "split"})

# Setups that are compatible with event/catalyst-driven conditions
_EVENT_COMPATIBLE_SETUPS = frozenset({
    SetupType.MOMENTUM_BURST,
    SetupType.BREAKOUT,
    SetupType.CATALYST_MOVER,
    SetupType.GAP_AND_GO,
    SetupType.LIQUIDITY_SWEEP,
})

# Volume surge threshold: signal bar must exceed this multiple of the 10-bar average
_VOLUME_SURGE_RATIO = 1.5

# Minimum bars required for volume context calculation
_MIN_BARS_FOR_VOLUME = 5


class SignalValidationAgent(BaseAgent):
    """
    Agent 5 — Signal Validation Agent.

    Receives every CandidateSignal, evaluates it across five quality dimensions
    using both rule-based checks and an LLM holistic judgment, then publishes
    a ValidatedSignal with result=PASS or result=FAIL plus a list of fail_reasons.
    """

    # ── Constructor ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        data_feed: DataFeedBase,
        current_regime_fn: Callable[[], RegimeAssessment | None],
        current_universe_fn: Callable[[], UniverseSnapshot | None],
        config: dict | None = None,
    ) -> None:
        super().__init__(
            agent_id=AGENT_IDS[5],
            agent_name="SignalValidationAgent",
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )
        self._data_feed = data_feed
        self._current_regime_fn = current_regime_fn
        self._current_universe_fn = current_universe_fn

        # Config shortcut
        self._max_spread_bps: float = float(
            self.cfg("universe", "max_spread_bps", default=20)
        )

        # ── Session statistics (for PSRA) ─────────────────────────────────────
        self._stats: dict[str, int] = {
            "total_received": 0,
            "total_passed": 0,
            "total_failed": 0,
            "fail_htf_alignment": 0,
            "fail_volume": 0,
            "fail_spread": 0,
            "fail_catalyst": 0,
            "fail_regime": 0,
        }

    # ── Topic subscriptions ───────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.CANDIDATE_SIGNAL]

    # ── Main message handler ──────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        if message.topic != Topic.CANDIDATE_SIGNAL:
            return

        try:
            candidate = CandidateSignal(**message.payload)
        except Exception as exc:
            logger.error("[SVA] Failed to parse CandidateSignal: %s", exc)
            return

        self._stats["total_received"] += 1
        validated = await self._validate(candidate)

        # Publish result regardless of pass/fail
        await self.publish(
            Topic.VALIDATED_SIGNAL,
            validated.model_dump(mode="json"),
            correlation_id=candidate.signal_id,
        )

        # Update session statistics
        if validated.result == ValidationResult.PASS:
            self._stats["total_passed"] += 1
        else:
            self._stats["total_failed"] += 1
            for reason in validated.fail_reasons:
                stat_key = f"fail_{reason.value.split('_')[0].lower()}"
                # Map enum values to stat keys
                stat_map = {
                    ValidationFailReason.NO_HTF_ALIGNMENT: "fail_htf_alignment",
                    ValidationFailReason.INSUFFICIENT_VOLUME: "fail_volume",
                    ValidationFailReason.SPREAD_TOO_WIDE: "fail_spread",
                    ValidationFailReason.CATALYST_MISMATCH: "fail_catalyst",
                    ValidationFailReason.REGIME_CONTRADICTION: "fail_regime",
                }
                key = stat_map.get(reason)
                if key:
                    self._stats[key] += 1

        await self.audit.record(AuditEvent(
            event_type="SIGNAL_VALIDATED",
            agent=self.agent_id,
            symbol=candidate.symbol,
            details={
                "signal_id": candidate.signal_id,
                "setup_type": candidate.setup_type.value,
                "result": validated.result.value,
                "fail_reasons": [r.value for r in validated.fail_reasons],
                "htf_aligned": validated.htf_aligned,
                "volume_ok": validated.volume_ok,
                "spread_ok": validated.spread_ok,
                "catalyst_consistent": validated.catalyst_consistent,
                "regime_consistent": validated.regime_consistent,
                "session_stats": dict(self._stats),
            },
        ))

        logger.info(
            "[SVA] signal_id=%s symbol=%s result=%s fail_reasons=%s",
            candidate.signal_id,
            candidate.symbol,
            validated.result.value,
            [r.value for r in validated.fail_reasons],
        )

    # ── Validation logic ──────────────────────────────────────────────────────

    async def _validate(self, candidate: CandidateSignal) -> ValidatedSignal:
        """
        Run all five validation dimensions. Collect results, then call the LLM
        for a holistic review of the combined evidence. Reject on any failure.
        """
        symbol = candidate.symbol
        setup_type = candidate.setup_type
        direction = candidate.direction

        # ── 1. HTF Alignment ──────────────────────────────────────────────────
        htf_aligned, htf_note = self._check_htf_alignment(symbol, direction)

        # ── 2. Volume Context ─────────────────────────────────────────────────
        volume_ok, volume_note = self._check_volume(symbol, candidate)

        # ── 3. Spread Quality ─────────────────────────────────────────────────
        spread_ok, spread_note = self._check_spread(candidate)

        # ── 4. Catalyst Consistency ───────────────────────────────────────────
        catalyst_consistent, catalyst_note = self._check_catalyst(symbol, setup_type)

        # ── 5. Regime Consistency ─────────────────────────────────────────────
        regime_consistent, regime_note = self._check_regime(setup_type)

        # ── LLM holistic judgment ─────────────────────────────────────────────
        llm_result = await self._llm_holistic_check(
            candidate,
            htf_aligned=htf_aligned,
            htf_note=htf_note,
            volume_ok=volume_ok,
            volume_note=volume_note,
            spread_ok=spread_ok,
            spread_note=spread_note,
            catalyst_consistent=catalyst_consistent,
            catalyst_note=catalyst_note,
            regime_consistent=regime_consistent,
            regime_note=regime_note,
        )

        # ── Collect fail reasons ──────────────────────────────────────────────
        # Rule-based failures take precedence; LLM can ADD fails but not remove them.
        fail_reasons: list[ValidationFailReason] = []

        if not htf_aligned:
            fail_reasons.append(ValidationFailReason.NO_HTF_ALIGNMENT)
        if not volume_ok:
            fail_reasons.append(ValidationFailReason.INSUFFICIENT_VOLUME)
        if not spread_ok:
            fail_reasons.append(ValidationFailReason.SPREAD_TOO_WIDE)
        if not catalyst_consistent:
            fail_reasons.append(ValidationFailReason.CATALYST_MISMATCH)
        if not regime_consistent:
            fail_reasons.append(ValidationFailReason.REGIME_CONTRADICTION)

        # Merge any additional LLM-identified fail reasons not already present
        for extra_reason in llm_result.get("additional_fail_reasons", []):
            try:
                reason_enum = ValidationFailReason(extra_reason)
                if reason_enum not in fail_reasons:
                    fail_reasons.append(reason_enum)
            except ValueError:
                pass

        overall_pass = len(fail_reasons) == 0
        result = ValidationResult.PASS if overall_pass else ValidationResult.FAIL

        return ValidatedSignal(
            signal_id=candidate.signal_id,
            candidate=candidate,
            result=result,
            fail_reasons=fail_reasons,
            htf_aligned=htf_aligned,
            volume_ok=volume_ok,
            spread_ok=spread_ok,
            catalyst_consistent=catalyst_consistent,
            regime_consistent=regime_consistent,
        )

    # ── Dimension 1: HTF Alignment ────────────────────────────────────────────

    def _check_htf_alignment(
        self, symbol: str, direction: Direction
    ) -> tuple[bool, str]:
        """
        Check whether the 5-minute bar trend agrees with the signal direction.
        Uses a simple higher-high / lower-low measure across the last 5 bars.
        """
        bars = self._data_feed.get_bar_history(symbol, n=5)
        if len(bars) < 3:
            return False, "Insufficient bar history for HTF alignment check"

        closes = [b.close for b in bars]
        # Trend: last close > first close → uptrend, else downtrend
        uptrend = closes[-1] > closes[0]

        if direction == Direction.LONG and uptrend:
            return True, f"5m trend is up ({closes[0]:.2f}→{closes[-1]:.2f}), LONG aligned"
        if direction == Direction.SHORT and not uptrend:
            return True, f"5m trend is down ({closes[0]:.2f}→{closes[-1]:.2f}), SHORT aligned"

        note = (
            f"5m trend is {'up' if uptrend else 'down'} "
            f"({closes[0]:.2f}→{closes[-1]:.2f}), conflicts with {direction.value.upper()}"
        )
        return False, note

    # ── Dimension 2: Volume Context ───────────────────────────────────────────

    def _check_volume(
        self, symbol: str, candidate: CandidateSignal
    ) -> tuple[bool, str]:
        """
        Signal bar volume must be at least 1.5x the average of the last 10 bars.
        Uses the tick volume as a proxy when bar data is sparse.
        """
        bars = self._data_feed.get_bar_history(symbol, n=10)
        if len(bars) < _MIN_BARS_FOR_VOLUME:
            return False, f"Only {len(bars)} bars available, need {_MIN_BARS_FOR_VOLUME}"

        avg_vol = statistics.mean(b.volume for b in bars)
        signal_bar_volume = bars[-1].volume if bars else candidate.tick_at_signal.volume

        if avg_vol <= 0:
            return False, "Average volume is zero — cannot validate"

        ratio = signal_bar_volume / avg_vol
        passed = ratio >= _VOLUME_SURGE_RATIO
        note = (
            f"Signal bar volume={signal_bar_volume:,}, "
            f"10-bar avg={avg_vol:,.0f}, ratio={ratio:.2f} "
            f"(threshold={_VOLUME_SURGE_RATIO}x)"
        )
        return passed, note

    # ── Dimension 3: Spread Quality ───────────────────────────────────────────

    def _check_spread(self, candidate: CandidateSignal) -> tuple[bool, str]:
        """
        Re-check spread at validation time using the tick embedded in the signal.
        The tick was captured at signal detection, so it reflects conditions then.
        """
        spread_bps = candidate.tick_at_signal.spread_bps
        passed = spread_bps < self._max_spread_bps
        note = (
            f"spread_bps={spread_bps:.2f}, threshold={self._max_spread_bps} bps — "
            f"{'OK' if passed else 'FAIL'}"
        )
        return passed, note

    # ── Dimension 4: Catalyst Consistency ────────────────────────────────────

    def _check_catalyst(
        self, symbol: str, setup_type: SetupType
    ) -> tuple[bool, str]:
        """
        Check that the symbol's catalyst (if any) is consistent with the setup type.
        Event-driven catalysts (earnings, news, …) are compatible only with
        momentum/breakout-style setups. Non-event setups are always consistent
        when no catalyst is present.
        """
        universe = self._current_universe_fn()
        if universe is None:
            return True, "No universe snapshot available — skipping catalyst check"

        symbol_info = next(
            (s for s in universe.symbols if s.symbol == symbol), None
        )
        if symbol_info is None:
            return True, f"{symbol} not found in universe snapshot — skipping"

        catalyst = (symbol_info.catalyst or "").lower()

        if not catalyst:
            # No catalyst — any setup is fine
            return True, "No catalyst on record — all setups consistent"

        is_event_catalyst = any(kw in catalyst for kw in _EVENT_DRIVEN_CATALYSTS)

        if is_event_catalyst and setup_type not in _EVENT_COMPATIBLE_SETUPS:
            note = (
                f"Catalyst '{catalyst}' is event-driven but setup '{setup_type.value}' "
                f"is not in event-compatible set {[s.value for s in _EVENT_COMPATIBLE_SETUPS]}"
            )
            return False, note

        if not is_event_catalyst and setup_type == SetupType.CATALYST_MOVER:
            note = (
                f"setup_type is CATALYST_MOVER but catalyst '{catalyst}' "
                f"does not appear event-driven"
            )
            return False, note

        return True, f"Catalyst '{catalyst}' is consistent with setup '{setup_type.value}'"

    # ── Dimension 5: Regime Consistency ──────────────────────────────────────

    def _check_regime(self, setup_type: SetupType) -> tuple[bool, str]:
        """
        Confirm the setup_type is present in the current regime's allowed list.
        """
        regime = self._current_regime_fn()
        if regime is None:
            return False, "No regime assessment available — defaulting to FAIL"

        if not regime.label.is_tradeable:
            return False, f"Regime '{regime.label.value}' is a no-trade regime"

        if setup_type not in regime.allowed_setup_types:
            note = (
                f"setup_type '{setup_type.value}' not in "
                f"regime.allowed_setup_types for '{regime.label.value}'"
            )
            return False, note

        return True, f"'{setup_type.value}' is approved for regime '{regime.label.value}'"

    # ── LLM holistic check ────────────────────────────────────────────────────

    async def _llm_holistic_check(
        self,
        candidate: CandidateSignal,
        *,
        htf_aligned: bool,
        htf_note: str,
        volume_ok: bool,
        volume_note: str,
        spread_ok: bool,
        spread_note: str,
        catalyst_consistent: bool,
        catalyst_note: str,
        regime_consistent: bool,
        regime_note: str,
    ) -> dict:
        """
        Ask the LLM to make a holistic judgment given all five dimension results.
        The LLM may identify additional fail reasons but cannot override hard rule failures.
        Returns a dict with keys:
          - "overall_pass": bool
          - "llm_reasoning": str
          - "additional_fail_reasons": list[str]  (ValidationFailReason values)
        """
        system_prompt = (
            "You are the Signal Validation Agent for an autonomous equity day-trading system. "
            "You receive the results of five rule-based quality checks and make a holistic "
            "judgment on whether the signal should proceed to risk evaluation. "
            "Your job is to be a skeptic — look for subtle contradictions the rules may have missed. "
            "Respond with valid JSON only. No markdown, no prose outside the JSON object."
        )

        dimension_summary = {
            "1_htf_alignment": {"passed": htf_aligned, "note": htf_note},
            "2_volume_context": {"passed": volume_ok, "note": volume_note},
            "3_spread_quality": {"passed": spread_ok, "note": spread_note},
            "4_catalyst_consistency": {"passed": catalyst_consistent, "note": catalyst_note},
            "5_regime_consistency": {"passed": regime_consistent, "note": regime_note},
        }

        tick = candidate.tick_at_signal
        signal_summary = {
            "signal_id": candidate.signal_id,
            "symbol": candidate.symbol,
            "setup_type": candidate.setup_type.value,
            "direction": candidate.direction.value,
            "entry_price": candidate.entry_price,
            "initial_stop": candidate.initial_stop,
            "initial_target": candidate.initial_target,
            "risk_reward": round(candidate.risk_reward, 2),
            "regime": candidate.regime.value,
            "spread_bps": round(tick.spread_bps, 2),
            "misa_reasoning": candidate.reasoning,
        }

        user_message = f"""
Signal to evaluate:
{json.dumps(signal_summary, indent=2)}

Five-dimension rule check results:
{json.dumps(dimension_summary, indent=2)}

Valid additional_fail_reasons values (ValidationFailReason enum):
  "no_higher_timeframe_alignment"
  "insufficient_volume"
  "spread_too_wide"
  "catalyst_mismatch"
  "regime_contradiction"
  "edge_not_approved"
  "data_feed_dirty"

Return ONLY valid JSON in exactly this schema:
{{
  "overall_pass": true | false,
  "llm_reasoning": "<holistic analysis, max 150 words>",
  "additional_fail_reasons": ["<reason_value>", ...]
}}

Rules:
- overall_pass must be false if ANY dimension passed=false.
- additional_fail_reasons lists reasons not already caught by the rule checks.
- If all five dimensions passed and no additional issues exist, set overall_pass=true.
"""

        try:
            result = await self.llm_call_json(system_prompt, user_message)
            # Ensure required keys are present
            if not isinstance(result, dict):
                raise ValueError("LLM returned non-dict")
            result.setdefault("overall_pass", False)
            result.setdefault("llm_reasoning", "")
            result.setdefault("additional_fail_reasons", [])
            return result
        except Exception as exc:
            logger.warning("[SVA] LLM holistic check failed: %s — defaulting to rule-based only", exc)
            all_passed = all([htf_aligned, volume_ok, spread_ok, catalyst_consistent, regime_consistent])
            return {
                "overall_pass": all_passed,
                "llm_reasoning": f"LLM unavailable: {exc}",
                "additional_fail_reasons": [],
            }

    # ── Statistics accessor (for PSRA) ────────────────────────────────────────

    @property
    def session_stats(self) -> dict[str, int]:
        """Return a copy of the current session validation statistics."""
        return dict(self._stats)

    def reset_session_stats(self) -> None:
        """Reset statistics at the start of a new session."""
        for k in self._stats:
            self._stats[k] = 0
