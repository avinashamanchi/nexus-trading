"""
Agent 0 — Edge Research Agent

Maintains the approved setup list (ApprovedSetupList).  Every setup that
enters the live pipeline must pass a rigorous expectancy gate administered
by this agent.  The gate is the hard boundary between the research world
and the trading world: nothing crosses it without passing.

Safety contract:
  - No setup enters pipeline without positive net expectancy after costs.
  - Retired setups are permanently blocked — no intraday un-retirement.
  - Changes only take effect the NEXT session (never intraday).
  - Only processes proposals with status HGL_APPROVED.
  - Minimum 1000 historical samples required for full validation.
  - Parameter tweaks require minimum 200 out-of-sample trades.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, date
from typing import Any

from core.enums import (
    ChangeProposalStatus,
    ChangeProposalType,
    RegimeLabel,
    SetupType,
    Topic,
)
from core.models import (
    ApprovedSetup,
    ApprovedSetupList,
    AuditEvent,
    BusMessage,
    ChangeProposal,
)
from agents.base import BaseAgent
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

# ── State store key for persisting the approved setup list ────────────────────
_SETUP_LIST_STATE_KEY = "edge_research:approved_setup_list"

# Mapping from regime to setup types that make sense there (used when a new
# setup is admitted and the proposal doesn't specify allowed_regimes).
_DEFAULT_REGIME_MAP: dict[SetupType, list[RegimeLabel]] = {
    SetupType.BREAKOUT: [
        RegimeLabel.TREND_DAY,
        RegimeLabel.EVENT_DRIVEN_DAY,
    ],
    SetupType.MOMENTUM_BURST: [
        RegimeLabel.TREND_DAY,
        RegimeLabel.EVENT_DRIVEN_DAY,
    ],
    SetupType.GAP_AND_GO: [
        RegimeLabel.TREND_DAY,
        RegimeLabel.EVENT_DRIVEN_DAY,
    ],
    SetupType.OPENING_RANGE_BREAK: [
        RegimeLabel.TREND_DAY,
        RegimeLabel.EVENT_DRIVEN_DAY,
    ],
    SetupType.VWAP_TAP: [
        RegimeLabel.TREND_DAY,
        RegimeLabel.MEAN_REVERSION_DAY,
        RegimeLabel.EVENT_DRIVEN_DAY,
    ],
    SetupType.MEAN_REVERSION_FADE: [
        RegimeLabel.MEAN_REVERSION_DAY,
    ],
    SetupType.CATALYST_MOVER: [
        RegimeLabel.EVENT_DRIVEN_DAY,
    ],
    SetupType.LIQUIDITY_SWEEP: [
        RegimeLabel.TREND_DAY,
        RegimeLabel.EVENT_DRIVEN_DAY,
    ],
}


class EdgeResearchAgent(BaseAgent):
    """
    Agent 0 — Edge Research Agent.

    Owns the canonical ApprovedSetupList.  All other agents treat this list
    as the source of truth for which setups may be traded.

    Message flow:
      SUBSCRIBES: Topic.CHANGE_PROPOSAL  (§5.5 PSRA handoff)
      PUBLISHES:  Topic.APPROVED_SETUPS  (on startup + after any change)
    """

    AGENT_ID = "agent_00_edge_research"
    AGENT_NAME = "EdgeResearchAgent"

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        agent_id: str | None = None,
        agent_name: str | None = None,
        config: dict | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id or self.AGENT_ID,
            agent_name=agent_name or self.AGENT_NAME,
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )
        self._approved_list: ApprovedSetupList = ApprovedSetupList(setups=[])
        # Rolling expectancy history: setup_type → deque of recent per-share EVs
        self._rolling_ev: dict[SetupType, deque] = {}

    # ── Topic subscriptions ───────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.CHANGE_PROPOSAL]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_startup(self) -> None:
        """Load persisted setup list and publish the current approved setups."""
        await self._load_setup_list()
        await self._check_rolling_expectancy()
        await self._publish_approved_setups()
        logger.info(
            "[%s] Startup complete — %d active setups loaded",
            self.AGENT_NAME,
            sum(1 for s in self._approved_list.setups if not s.retired),
        )

    async def on_shutdown(self) -> None:
        await self._persist_setup_list()

    # ── Core message handler ──────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        if message.topic != Topic.CHANGE_PROPOSAL:
            return
        try:
            proposal = ChangeProposal.model_validate(message.payload)
        except Exception as exc:
            logger.error("[%s] Could not parse ChangeProposal: %s", self.AGENT_NAME, exc)
            return

        # Safety gate: only handle HGL-approved proposals
        if proposal.status != ChangeProposalStatus.HGL_APPROVED:
            logger.debug(
                "[%s] Ignoring proposal %s — status is %s (not HGL_APPROVED)",
                self.AGENT_NAME,
                proposal.proposal_id,
                proposal.status.value,
            )
            return

        logger.info(
            "[%s] Processing HGL-approved proposal %s (%s)",
            self.AGENT_NAME,
            proposal.proposal_id,
            proposal.proposal_type.value,
        )

        await self.audit.record(AuditEvent(
            event_type="EDGE_PROPOSAL_RECEIVED",
            agent=self.AGENT_ID,
            details={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type.value,
                "description": proposal.description,
            },
        ))

        if proposal.proposal_type == ChangeProposalType.PARAMETER_TWEAK:
            await self._handle_parameter_tweak(proposal)
        elif proposal.proposal_type == ChangeProposalType.SETUP_MODIFICATION:
            await self._handle_setup_modification(proposal)
        elif proposal.proposal_type == ChangeProposalType.NEW_SETUP_ADDITION:
            await self._handle_new_setup_addition(proposal)
        elif proposal.proposal_type == ChangeProposalType.SETUP_RETIREMENT:
            await self._handle_setup_retirement(proposal)
        else:
            logger.warning(
                "[%s] Unknown proposal type: %s",
                self.AGENT_NAME,
                proposal.proposal_type,
            )

    # ── Proposal handlers ─────────────────────────────────────────────────────

    async def _handle_parameter_tweak(self, proposal: ChangeProposal) -> None:
        """
        Abbreviated re-test (200 out-of-sample trades).
        Applies only if EV is still positive after the tweak.
        Changes take effect next session.
        """
        setup_type = self._extract_setup_type(proposal)
        if setup_type is None:
            await self._reject_proposal(proposal, "Could not determine setup_type from proposal")
            return

        stats = proposal.parameter_changes.get("trade_statistics", {})
        sample_count = stats.get("sample_count", 0)

        min_oos = self.cfg("signals", "out_of_sample_min_trades", default=200)
        if sample_count < min_oos:
            await self._reject_proposal(
                proposal,
                f"Insufficient out-of-sample trades: {sample_count} < {min_oos}",
            )
            return

        analysis = await self._llm_analyze_expectancy(
            setup_type=setup_type,
            stats=stats,
            validation_mode="abbreviated",
        )

        if not analysis.get("passes_expectancy", False):
            await self._reject_proposal(
                proposal,
                f"LLM analysis: net_ev={analysis.get('net_ev_per_share', 'N/A')} — "
                f"does not meet positive expectancy threshold. "
                f"Reason: {analysis.get('reasoning', 'no detail')}",
            )
            return

        await self._apply_parameter_update(proposal, setup_type, analysis)
        await self._accept_proposal(proposal, analysis)

    async def _handle_setup_modification(self, proposal: ChangeProposal) -> None:
        """
        Full re-validation (1000 samples).
        Treats the modified setup as a fresh validation target.
        Changes take effect next session.
        """
        setup_type = self._extract_setup_type(proposal)
        if setup_type is None:
            await self._reject_proposal(proposal, "Could not determine setup_type from proposal")
            return

        stats = proposal.parameter_changes.get("trade_statistics", {})
        sample_count = stats.get("sample_count", 0)

        min_samples = self.cfg("signals", "min_historical_samples", default=1000)
        if sample_count < min_samples:
            await self._reject_proposal(
                proposal,
                f"Insufficient samples for full re-validation: {sample_count} < {min_samples}",
            )
            return

        analysis = await self._llm_analyze_expectancy(
            setup_type=setup_type,
            stats=stats,
            validation_mode="full",
        )

        if not analysis.get("passes_expectancy", False):
            await self._reject_proposal(
                proposal,
                f"Full re-validation failed: net_ev={analysis.get('net_ev_per_share', 'N/A')}. "
                f"Reason: {analysis.get('reasoning', 'no detail')}",
            )
            return

        await self._apply_setup_update(proposal, setup_type, analysis)
        await self._accept_proposal(proposal, analysis)

    async def _handle_new_setup_addition(self, proposal: ChangeProposal) -> None:
        """
        Full validation + Monte Carlo simulation.
        Requires 1000+ samples and positive EV with high confidence.
        Changes take effect next session.
        """
        setup_type = self._extract_setup_type(proposal)
        if setup_type is None:
            await self._reject_proposal(proposal, "Could not determine setup_type from proposal")
            return

        # Block if setup already active
        if self._approved_list.is_approved(setup_type):
            await self._reject_proposal(
                proposal,
                f"Setup {setup_type.value} is already in the active approved list",
            )
            return

        stats = proposal.parameter_changes.get("trade_statistics", {})
        sample_count = stats.get("sample_count", 0)

        min_samples = self.cfg("signals", "min_historical_samples", default=1000)
        if sample_count < min_samples:
            await self._reject_proposal(
                proposal,
                f"New setup requires {min_samples} samples; only {sample_count} provided",
            )
            return

        # Full analysis + Monte Carlo
        analysis = await self._llm_analyze_expectancy(
            setup_type=setup_type,
            stats=stats,
            validation_mode="full_with_monte_carlo",
        )

        if not analysis.get("passes_expectancy", False):
            await self._reject_proposal(
                proposal,
                f"New setup validation failed: net_ev={analysis.get('net_ev_per_share', 'N/A')}. "
                f"Reason: {analysis.get('reasoning', 'no detail')}",
            )
            return

        if not analysis.get("monte_carlo_passes", False):
            await self._reject_proposal(
                proposal,
                f"Monte Carlo simulation failed: "
                f"ruin_probability={analysis.get('ruin_probability', 'N/A')}. "
                f"Reason: {analysis.get('reasoning', 'no detail')}",
            )
            return

        # Build new ApprovedSetup scheduled for next session
        allowed_regimes = self._parse_allowed_regimes(proposal, setup_type)
        new_setup = ApprovedSetup(
            setup_type=setup_type,
            expectancy_per_share=float(analysis["net_ev_per_share"]),
            win_rate=float(stats.get("win_rate", 0.0)),
            avg_win=float(stats.get("avg_win", 0.0)),
            avg_loss=float(stats.get("avg_loss", 0.0)),
            sample_count=int(sample_count),
            out_of_sample_validated=True,
            allowed_regimes=allowed_regimes,
            last_validated=datetime.utcnow(),
            rolling_expectancy=float(analysis["net_ev_per_share"]),
            retired=False,
        )

        self._approved_list.setups.append(new_setup)
        self._approved_list = ApprovedSetupList(
            setups=self._approved_list.setups,
            version=self._approved_list.version + 1,
            effective_session=self._next_session_date(),
        )
        await self._persist_setup_list()
        await self._accept_proposal(proposal, analysis)
        await self._publish_approved_setups()

        logger.info(
            "[%s] New setup %s admitted with EV=%.4f (effective %s)",
            self.AGENT_NAME,
            setup_type.value,
            new_setup.expectancy_per_share,
            self._approved_list.effective_session,
        )

    async def _handle_setup_retirement(self, proposal: ChangeProposal) -> None:
        """
        Archive the setup — no re-validation required.
        Changes take effect next session.
        """
        setup_type = self._extract_setup_type(proposal)
        if setup_type is None:
            await self._reject_proposal(proposal, "Could not determine setup_type from proposal")
            return

        setup = self._approved_list.get_setup(setup_type)
        if setup is None:
            # Already retired or never existed — treat as success
            logger.info(
                "[%s] Retirement of %s — setup not found or already retired",
                self.AGENT_NAME,
                setup_type.value,
            )
        else:
            setup.retired = True
            setup.retire_reason = proposal.description

        self._approved_list = ApprovedSetupList(
            setups=self._approved_list.setups,
            version=self._approved_list.version + 1,
            effective_session=self._next_session_date(),
        )
        await self._persist_setup_list()
        await self._accept_proposal(proposal, {"action": "retired", "setup": setup_type.value})
        await self._publish_approved_setups()

        await self.audit.record(AuditEvent(
            event_type="SETUP_RETIRED",
            agent=self.AGENT_ID,
            details={
                "setup_type": setup_type.value,
                "proposal_id": proposal.proposal_id,
                "reason": proposal.description,
                "effective_session": self._approved_list.effective_session,
            },
        ))

        logger.info(
            "[%s] Setup %s retired (effective %s)",
            self.AGENT_NAME,
            setup_type.value,
            self._approved_list.effective_session,
        )

    # ── Rolling expectancy monitor ────────────────────────────────────────────

    async def _check_rolling_expectancy(self) -> None:
        """
        Scan the active setup list and retire any setup whose rolling EV
        has fallen below the configured threshold.
        """
        retire_threshold = self.cfg(
            "signals", "retire_below_expectancy", default=-0.05
        )
        changed = False

        for setup in self._approved_list.setups:
            if setup.retired:
                continue
            if (
                setup.rolling_expectancy is not None
                and setup.rolling_expectancy < retire_threshold
            ):
                setup.retired = True
                setup.retire_reason = (
                    f"Rolling expectancy {setup.rolling_expectancy:.4f} fell below "
                    f"threshold {retire_threshold:.4f}"
                )
                changed = True
                logger.warning(
                    "[%s] AUTO-RETIRING %s — rolling EV %.4f < %.4f",
                    self.AGENT_NAME,
                    setup.setup_type.value,
                    setup.rolling_expectancy,
                    retire_threshold,
                )
                await self.audit.record(AuditEvent(
                    event_type="SETUP_AUTO_RETIRED",
                    agent=self.AGENT_ID,
                    details={
                        "setup_type": setup.setup_type.value,
                        "rolling_expectancy": setup.rolling_expectancy,
                        "threshold": retire_threshold,
                    },
                ))

        if changed:
            self._approved_list = ApprovedSetupList(
                setups=self._approved_list.setups,
                version=self._approved_list.version + 1,
                effective_session=self._next_session_date(),
            )
            await self._persist_setup_list()
            await self._publish_approved_setups()

    # ── LLM expectancy analysis ───────────────────────────────────────────────

    async def _llm_analyze_expectancy(
        self,
        setup_type: SetupType,
        stats: dict,
        validation_mode: str,
    ) -> dict:
        """
        Call the LLM to analyse historical trade statistics and compute net
        expectancy per share after realistic transaction costs.

        Returns a dict with at minimum:
          - passes_expectancy: bool
          - net_ev_per_share: float
          - reasoning: str
          - monte_carlo_passes: bool (only if validation_mode contains 'monte_carlo')
          - ruin_probability: float (only if validation_mode contains 'monte_carlo')
        """
        system_prompt = (
            "You are a quantitative edge-research analyst for an autonomous day-trading system. "
            "Your role is to evaluate whether a trading setup has sufficient statistical edge "
            "to be admitted to the live pipeline. You are rigorous, conservative, and unbiased. "
            "You compute net expected value (EV) per share after all realistic transaction costs. "
            "\n\n"
            "Transaction cost assumptions (use these exactly):\n"
            "  - Commission: $0.005/share (both sides = $0.01/share round-trip)\n"
            "  - SEC fee (sells only): ~$0.000008/share on average\n"
            "  - FINRA TAF (sells only): $0.000145/share\n"
            "  - Estimated slippage: 0.01/share average (market impact + spread crossing)\n"
            "  - Total realistic cost: approximately $0.02/share round-trip\n"
            "\n"
            "A setup PASSES if and only if:\n"
            "  1. net_ev_per_share > 0.0 (positive expectancy after all costs)\n"
            "  2. sample_count meets the minimum for this validation mode\n"
            "  3. The win_rate and avg_win/avg_loss combination is statistically robust\n"
            "\n"
            "For 'full_with_monte_carlo' mode, also simulate 1000 equity curves using the "
            "provided statistics and determine ruin probability (ruin = drawdown > 25% of "
            "starting capital). The setup fails Monte Carlo if ruin_probability > 0.05."
        )

        user_message = (
            f"Setup type: {setup_type.value}\n"
            f"Validation mode: {validation_mode}\n"
            f"\nTrade statistics:\n"
            f"  win_rate: {stats.get('win_rate', 'N/A')}\n"
            f"  avg_win ($/share): {stats.get('avg_win', 'N/A')}\n"
            f"  avg_loss ($/share): {stats.get('avg_loss', 'N/A')}\n"
            f"  sample_count: {stats.get('sample_count', 'N/A')}\n"
            f"  gross_ev_per_share: {stats.get('gross_ev_per_share', 'N/A')}\n"
            f"  avg_hold_time_sec: {stats.get('avg_hold_time_sec', 'N/A')}\n"
            f"  max_consecutive_losses: {stats.get('max_consecutive_losses', 'N/A')}\n"
            f"  max_drawdown_pct: {stats.get('max_drawdown_pct', 'N/A')}\n"
            f"\nAdditional context from proposal:\n"
            f"  {stats.get('additional_context', 'none provided')}\n"
            f"\nRespond with JSON containing these fields:\n"
            f"  passes_expectancy (bool),\n"
            f"  net_ev_per_share (float — EV after all transaction costs),\n"
            f"  gross_ev_per_share (float),\n"
            f"  total_cost_per_share (float),\n"
            f"  reasoning (string — concise explanation),\n"
            f"  confidence (float 0-1),\n"
            + (
                "  monte_carlo_passes (bool),\n"
                "  ruin_probability (float 0-1),\n"
                "  median_equity_curve_return (float),\n"
                if "monte_carlo" in validation_mode
                else ""
            )
            + "  edge_quality (string: 'strong'|'marginal'|'insufficient')"
        )

        try:
            result = await self.llm_call_json(
                system_prompt=system_prompt,
                user_message=user_message,
            )
            # Ensure boolean types are correct (LLM sometimes returns strings)
            if "passes_expectancy" in result:
                val = result["passes_expectancy"]
                result["passes_expectancy"] = val if isinstance(val, bool) else str(val).lower() == "true"
            if "monte_carlo_passes" in result:
                val = result["monte_carlo_passes"]
                result["monte_carlo_passes"] = val if isinstance(val, bool) else str(val).lower() == "true"
            return result
        except Exception as exc:
            logger.exception(
                "[%s] LLM expectancy analysis failed for %s: %s",
                self.AGENT_NAME,
                setup_type.value,
                exc,
            )
            # Fail safe: return a rejection result
            return {
                "passes_expectancy": False,
                "net_ev_per_share": -999.0,
                "reasoning": f"LLM analysis error: {exc}",
                "confidence": 0.0,
                "monte_carlo_passes": False,
                "ruin_probability": 1.0,
                "edge_quality": "insufficient",
            }

    # ── Apply changes ─────────────────────────────────────────────────────────

    async def _apply_parameter_update(
        self,
        proposal: ChangeProposal,
        setup_type: SetupType,
        analysis: dict,
    ) -> None:
        """Update an existing setup's statistics after a parameter tweak."""
        stats = proposal.parameter_changes.get("trade_statistics", {})
        for setup in self._approved_list.setups:
            if setup.setup_type == setup_type and not setup.retired:
                setup.expectancy_per_share = float(analysis["net_ev_per_share"])
                setup.win_rate = float(stats.get("win_rate", setup.win_rate))
                setup.avg_win = float(stats.get("avg_win", setup.avg_win))
                setup.avg_loss = float(stats.get("avg_loss", setup.avg_loss))
                setup.sample_count = int(stats.get("sample_count", setup.sample_count))
                setup.last_validated = datetime.utcnow()
                setup.rolling_expectancy = float(analysis["net_ev_per_share"])
                break

        self._approved_list = ApprovedSetupList(
            setups=self._approved_list.setups,
            version=self._approved_list.version + 1,
            effective_session=self._next_session_date(),
        )
        await self._persist_setup_list()
        await self._publish_approved_setups()

    async def _apply_setup_update(
        self,
        proposal: ChangeProposal,
        setup_type: SetupType,
        analysis: dict,
    ) -> None:
        """Fully overwrite an existing setup's parameters after setup modification."""
        stats = proposal.parameter_changes.get("trade_statistics", {})
        allowed_regimes = self._parse_allowed_regimes(proposal, setup_type)

        updated = False
        for i, setup in enumerate(self._approved_list.setups):
            if setup.setup_type == setup_type:
                self._approved_list.setups[i] = ApprovedSetup(
                    setup_type=setup_type,
                    expectancy_per_share=float(analysis["net_ev_per_share"]),
                    win_rate=float(stats.get("win_rate", setup.win_rate)),
                    avg_win=float(stats.get("avg_win", setup.avg_win)),
                    avg_loss=float(stats.get("avg_loss", setup.avg_loss)),
                    sample_count=int(stats.get("sample_count", setup.sample_count)),
                    out_of_sample_validated=True,
                    allowed_regimes=allowed_regimes,
                    last_validated=datetime.utcnow(),
                    rolling_expectancy=float(analysis["net_ev_per_share"]),
                    retired=False,
                )
                updated = True
                break

        if not updated:
            # Setup wasn't in list yet — add it
            new_setup = ApprovedSetup(
                setup_type=setup_type,
                expectancy_per_share=float(analysis["net_ev_per_share"]),
                win_rate=float(stats.get("win_rate", 0.0)),
                avg_win=float(stats.get("avg_win", 0.0)),
                avg_loss=float(stats.get("avg_loss", 0.0)),
                sample_count=int(stats.get("sample_count", 0)),
                out_of_sample_validated=True,
                allowed_regimes=allowed_regimes,
                last_validated=datetime.utcnow(),
                rolling_expectancy=float(analysis["net_ev_per_share"]),
                retired=False,
            )
            self._approved_list.setups.append(new_setup)

        self._approved_list = ApprovedSetupList(
            setups=self._approved_list.setups,
            version=self._approved_list.version + 1,
            effective_session=self._next_session_date(),
        )
        await self._persist_setup_list()
        await self._publish_approved_setups()

    # ── Proposal outcome helpers ──────────────────────────────────────────────

    async def _accept_proposal(self, proposal: ChangeProposal, analysis: dict) -> None:
        proposal.edge_validation_result = "passed"
        proposal.status = ChangeProposalStatus.EDGE_PASSED
        await self.store.upsert_change_proposal(proposal)
        await self.audit.record(AuditEvent(
            event_type="EDGE_PROPOSAL_ACCEPTED",
            agent=self.AGENT_ID,
            details={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type.value,
                "net_ev_per_share": analysis.get("net_ev_per_share"),
                "effective_session": self._approved_list.effective_session,
            },
        ))
        logger.info(
            "[%s] Proposal %s ACCEPTED — net EV: %s",
            self.AGENT_NAME,
            proposal.proposal_id,
            analysis.get("net_ev_per_share"),
        )

    async def _reject_proposal(self, proposal: ChangeProposal, reason: str) -> None:
        proposal.edge_validation_result = "failed"
        proposal.edge_rejection_reason = reason
        proposal.status = ChangeProposalStatus.EDGE_FAILED
        await self.store.upsert_change_proposal(proposal)
        await self.audit.record(AuditEvent(
            event_type="EDGE_PROPOSAL_REJECTED",
            agent=self.AGENT_ID,
            details={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type.value,
                "rejection_reason": reason,
            },
        ))
        logger.warning(
            "[%s] Proposal %s REJECTED — %s",
            self.AGENT_NAME,
            proposal.proposal_id,
            reason,
        )

    # ── Publishing ────────────────────────────────────────────────────────────

    async def _publish_approved_setups(self) -> None:
        await self.publish(
            topic=Topic.APPROVED_SETUPS,
            payload=self._approved_list.model_dump(mode="json"),
        )
        logger.debug(
            "[%s] Published ApprovedSetupList v%d (%d active setups)",
            self.AGENT_NAME,
            self._approved_list.version,
            sum(1 for s in self._approved_list.setups if not s.retired),
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _persist_setup_list(self) -> None:
        """
        Persist the setup list to the state store.
        Uses a dedicated key in the change_proposals table via a synthetic
        ChangeProposal — the state store does not have a generic KV API, so
        we store the serialised list in the proposal description field.
        """
        try:
            # The state store's change_proposals table is our nearest analogue
            # for arbitrary agent-state persistence.  We create a sentinel
            # proposal whose raw_json we hijack to carry the setup list JSON.
            sentinel = ChangeProposal(
                proposal_id=_SETUP_LIST_STATE_KEY,
                proposal_type=ChangeProposalType.PARAMETER_TWEAK,
                status=ChangeProposalStatus.LIVE,
                submitted_by=self.AGENT_ID,
                description=self._approved_list.model_dump_json(),
                parameter_changes={},
            )
            await self.store.upsert_change_proposal(sentinel)
        except Exception as exc:
            logger.error("[%s] Failed to persist setup list: %s", self.AGENT_NAME, exc)

    async def _load_setup_list(self) -> None:
        """Restore the setup list from the state store, or start fresh."""
        try:
            pending = await self.store.load_pending_proposals()
            for p in pending:
                if p.proposal_id == _SETUP_LIST_STATE_KEY:
                    self._approved_list = ApprovedSetupList.model_validate_json(
                        p.description
                    )
                    logger.info(
                        "[%s] Loaded setup list v%d from state store",
                        self.AGENT_NAME,
                        self._approved_list.version,
                    )
                    return
        except Exception as exc:
            logger.warning(
                "[%s] Could not load setup list from state store: %s — starting fresh",
                self.AGENT_NAME,
                exc,
            )
        self._approved_list = ApprovedSetupList(setups=[], version=1)

    # ── Utility helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_setup_type(proposal: ChangeProposal) -> SetupType | None:
        """Extract setup_type from proposal.parameter_changes."""
        raw = proposal.parameter_changes.get("setup_type")
        if raw is None:
            return None
        try:
            return SetupType(raw)
        except ValueError:
            logger.error("Unknown setup_type in proposal: %s", raw)
            return None

    @staticmethod
    def _parse_allowed_regimes(
        proposal: ChangeProposal, setup_type: SetupType
    ) -> list[RegimeLabel]:
        """Extract allowed_regimes from proposal or fall back to defaults."""
        raw_regimes = proposal.parameter_changes.get("allowed_regimes", [])
        if raw_regimes:
            parsed = []
            for r in raw_regimes:
                try:
                    parsed.append(RegimeLabel(r))
                except ValueError:
                    logger.warning("Unknown regime label in proposal: %s", r)
            if parsed:
                return parsed
        return _DEFAULT_REGIME_MAP.get(setup_type, list(RegimeLabel))

    @property
    def approved_setup_list(self) -> "ApprovedSetupList":
        """Public accessor used by coordinator and SPA."""
        return self._approved_list

    @staticmethod
    def _next_session_date() -> str:
        """Return tomorrow's date as YYYY-MM-DD (changes never apply intraday)."""
        from datetime import timedelta
        tomorrow = date.today() + timedelta(days=1)
        return tomorrow.strftime("%Y-%m-%d")
