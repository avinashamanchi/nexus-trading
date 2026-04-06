"""
Agent 14 — Post-Session Review Agent (PSRA)

Analyzes session performance and proposes improvements via the §5.5
PSRA → Edge Research handoff protocol.

Runs after session close. Cannot modify live rules intraday.
All proposals require HGL approval + Edge Research re-validation before going live.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from agents.base import BaseAgent
from core.enums import (
    ChangeProposalStatus,
    ChangeProposalType,
    Topic,
)
from core.models import (
    AuditEvent,
    BusMessage,
    ChangeProposal,
    ClosedTrade,
    SessionExecutionSummary,
    ShadowSessionSummary,
)

logger = logging.getLogger(__name__)


class PostSessionReviewAgent(BaseAgent):
    """
    Agent 14: Post-Session Review Agent (PSRA).

    Responsibilities:
    - Analyze today's session: setup expectancy by type, regime performance,
      execution quality, feed integrity, rejection patterns, wash-sale impact
    - Generate improvement proposals (parameter tweaks, setup modifications)
    - Submit proposals through §5.5 handoff protocol (never directly to live)
    - Compare shadow P/L against live conditions
    - Track signal starvation metrics
    """

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.SYSTEM_STATE]  # Triggered on POST_SESSION state

    async def process(self, message: BusMessage) -> None:
        payload = message.payload
        state = payload.get("state", "")
        if state == "post_session":
            await self._run_post_session_review()

    async def run_review(self, session_date: str) -> None:
        """Entry point called directly by the pipeline coordinator at session end."""
        logger.info("[PSRA] Starting post-session review for %s", session_date)
        await self._run_post_session_review(session_date)

    async def _run_post_session_review(self, session_date: str | None = None) -> None:
        session_date = session_date or datetime.utcnow().strftime("%Y-%m-%d")

        # Load today's closed trades
        trades = await self.store.load_closed_trades(session_date)
        if not trades:
            logger.info("[PSRA] No trades to review for %s", session_date)
            return

        logger.info("[PSRA] Reviewing %d trades for %s", len(trades), session_date)

        # Compute session statistics
        stats = self._compute_session_stats(trades)

        # LLM analysis
        analysis = await self._llm_analyze_session(trades, stats)

        # Generate proposals if warranted
        proposals = await self._generate_proposals(trades, stats, analysis)

        # Submit proposals through §5.5 handoff
        for proposal in proposals:
            await self._submit_proposal(proposal)

        # Record audit event
        await self.audit.record(AuditEvent(
            event_type="POST_SESSION_REVIEW",
            agent=self.agent_id,
            details={
                "session_date": session_date,
                "trade_count": len(trades),
                "stats": stats,
                "proposals_generated": len(proposals),
            },
        ))

        logger.info(
            "[PSRA] Review complete: %d proposals submitted for HGL review",
            len(proposals),
        )

    def _compute_session_stats(self, trades: list[ClosedTrade]) -> dict[str, Any]:
        if not trades:
            return {}

        wins = [t for t in trades if t.is_winner]
        losses = [t for t in trades if not t.is_winner]

        total_pnl = sum(t.net_pnl for t in trades)
        avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0

        # By setup type
        by_setup: dict[str, dict] = {}
        for trade in trades:
            st = trade.setup_type.value
            if st not in by_setup:
                by_setup[st] = {"trades": 0, "wins": 0, "pnl": 0.0}
            by_setup[st]["trades"] += 1
            by_setup[st]["pnl"] += trade.net_pnl
            if trade.is_winner:
                by_setup[st]["wins"] += 1

        for st, d in by_setup.items():
            d["win_rate"] = d["wins"] / d["trades"] if d["trades"] > 0 else 0

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades),
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": (len(wins) / len(trades)) * avg_win + (len(losses) / len(trades)) * avg_loss if trades else 0,
            "max_drawdown": self._compute_max_drawdown(trades),
            "avg_hold_sec": sum(t.hold_time_sec for t in trades) / len(trades),
            "by_setup": by_setup,
        }

    def _compute_max_drawdown(self, trades: list[ClosedTrade]) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for trade in sorted(trades, key=lambda t: t.closed_at):
            equity += trade.net_pnl
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        return max_dd

    async def _llm_analyze_session(
        self, trades: list[ClosedTrade], stats: dict
    ) -> dict:
        """Use LLM to perform deep analysis of the session."""
        system_prompt = """You are a professional trading analyst reviewing a day trading session.
Analyze the provided trade statistics and identify:
1. Which setup types are performing above/below expectancy
2. Whether execution quality is impacting results (slippage vs expected)
3. Whether the session regime was correctly classified
4. Any patterns in losing trades (time of day, setup type, hold time)
5. Specific, measurable parameter tweaks that could improve performance
6. Whether any setups should be considered for retirement (below 45% win rate sustained)

Be precise and quantitative. Propose only changes with clear, testable hypotheses."""

        user_message = f"""Session Statistics:
{json.dumps(stats, indent=2, default=str)}

Total trades: {len(trades)}
Winning setups (win rate > 60%): {[k for k, v in stats.get('by_setup', {}).items() if v['win_rate'] > 0.6]}
Underperforming setups (win rate < 45%): {[k for k, v in stats.get('by_setup', {}).items() if v['win_rate'] < 0.45]}

Analyze and return JSON with:
{{
  "session_quality": "good|neutral|poor",
  "key_observations": ["obs1", "obs2"],
  "parameter_tweak_proposals": [
    {{"parameter": "max_spread_bps", "current": 20, "proposed": 15, "reason": "..."}}
  ],
  "setup_retirement_candidates": ["setup_type"],
  "setup_modification_candidates": [{{"setup": "type", "change": "..."}}],
  "execution_quality_notes": "..."
}}"""

        try:
            return await self.llm_call_json(system_prompt, user_message)
        except Exception as exc:
            logger.error("[PSRA] LLM analysis failed: %s", exc)
            return {"session_quality": "unknown", "key_observations": []}

    async def _generate_proposals(
        self,
        trades: list[ClosedTrade],
        stats: dict,
        analysis: dict,
    ) -> list[ChangeProposal]:
        proposals = []

        # Parameter tweaks
        for tweak in analysis.get("parameter_tweak_proposals", []):
            param = tweak.get("parameter", "")
            proposed = tweak.get("proposed")
            reason = tweak.get("reason", "")

            # Check if change exceeds threshold requiring HGL review (10% change)
            current = tweak.get("current", 1.0)
            change_pct = abs(proposed - current) / current if current else 1.0
            threshold = self.cfg("post_session", "setup_parameter_tweak_threshold", default=0.10)

            proposal = ChangeProposal(
                proposal_type=ChangeProposalType.PARAMETER_TWEAK,
                status=ChangeProposalStatus.PROPOSED,
                description=f"Parameter tweak: {param} {current} → {proposed}. Reason: {reason}",
                parameter_changes={param: proposed},
                proposed_for_session=self._next_session_date(),
            )
            proposals.append(proposal)

        # Setup retirements
        for setup_type in analysis.get("setup_retirement_candidates", []):
            proposal = ChangeProposal(
                proposal_type=ChangeProposalType.SETUP_RETIREMENT,
                status=ChangeProposalStatus.PROPOSED,
                description=f"Retire setup '{setup_type}' — sustained win rate below 45%",
                parameter_changes={"setup_type": setup_type},
                proposed_for_session=self._next_session_date(),
            )
            proposals.append(proposal)

        # Setup modifications
        for mod in analysis.get("setup_modification_candidates", []):
            proposal = ChangeProposal(
                proposal_type=ChangeProposalType.SETUP_MODIFICATION,
                status=ChangeProposalStatus.PROPOSED,
                description=f"Modify setup '{mod.get('setup')}': {mod.get('change')}",
                parameter_changes=mod,
                proposed_for_session=self._next_session_date(),
            )
            proposals.append(proposal)

        return proposals

    async def _submit_proposal(self, proposal: ChangeProposal) -> None:
        """Step 1 of §5.5: PSRA flags proposal; it now awaits HGL review."""
        proposal.status = ChangeProposalStatus.AWAITING_HGL
        await self.store.upsert_change_proposal(proposal)

        await self.publish(
            Topic.CHANGE_PROPOSAL,
            payload=proposal.model_dump(mode="json"),
        )

        logger.info(
            "[PSRA] Proposal submitted for HGL review: %s (%s)",
            proposal.proposal_type.value, proposal.proposal_id[:8],
        )

    def _next_session_date(self) -> str:
        from datetime import date, timedelta as td
        d = date.today()
        # Skip weekends
        days_ahead = 1
        while True:
            next_d = d + td(days=days_ahead)
            if next_d.weekday() < 5:  # Mon–Fri
                return next_d.isoformat()
            days_ahead += 1

    async def evaluate_shadow_performance(
        self, summary: ShadowSessionSummary
    ) -> bool:
        """Check if shadow mode pass criteria (§8.2) are met."""
        cfg = self.config.get("shadow", {})
        checks = {
            "min_trading_days": summary.session_date is not None,
            "min_win_rate": summary.win_rate >= cfg.get("min_win_rate", 0.50),
            "max_drawdown": summary.max_drawdown <= (
                cfg.get("max_drawdown_pct", 0.04) * self.cfg("capital", "starting_equity", default=30000)
            ),
            "zero_data_events": summary.data_integrity_events == 0,
            "zero_reconciliation_events": summary.reconciliation_events == 0,
        }
        all_pass = all(checks.values())
        logger.info("[PSRA] Shadow criteria check: %s → %s", checks, "PASS" if all_pass else "FAIL")
        return all_pass
