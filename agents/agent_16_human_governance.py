"""
Agent 16 — Human Governance Layer (HGL)

Requires explicit human approval for all material changes to strategy,
capital allocation, or system configuration.

This agent provides the CLI/UI interface for human operators to:
- Review and approve/reject PSRA change proposals
- Authorize system restarts after halts
- Sign off on shadow mode pass criteria before shadow runs
- Log all approved modifications with timestamps and rationale

This is the only agent that accepts human input. All approvals are
logged to the immutable audit trail.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from agents.base import BaseAgent
from core.enums import ChangeProposalStatus, ChangeProposalType, Topic
from core.models import AuditEvent, BusMessage, ChangeProposal
from infrastructure.audit_log import governance_approval_event

logger = logging.getLogger(__name__)


class HumanGovernanceAgent(BaseAgent):
    """
    Agent 16: Human Governance Layer (HGL).

    This agent bridges human operators and the automated system.
    - Receives change proposals from PSRA (via message bus)
    - Presents them to human operators for approval
    - Forwards approved proposals to Edge Research Agent for re-validation
    - Maintains the change log (immutable audit trail)

    In production, this connects to a web dashboard or CLI.
    For now, it supports programmatic approval via approve_proposal() and
    an interactive CLI review loop.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._pending_proposals: dict[str, ChangeProposal] = {}
        self._approved_changes: list[dict] = []

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [
            Topic.CHANGE_PROPOSAL,
            Topic.HALT_COMMAND,   # Log all halts for HGL visibility
        ]

    async def process(self, message: BusMessage) -> None:
        if message.topic == Topic.CHANGE_PROPOSAL:
            await self._handle_proposal(message)
        elif message.topic == Topic.HALT_COMMAND:
            await self._handle_halt_notification(message)

    # ── Proposal handling ─────────────────────────────────────────────────────

    async def _handle_proposal(self, message: BusMessage) -> None:
        try:
            proposal = ChangeProposal.model_validate(message.payload)
        except Exception as exc:
            logger.error("[HGL] Could not parse ChangeProposal: %s", exc)
            return

        if proposal.status != ChangeProposalStatus.AWAITING_HGL:
            return

        self._pending_proposals[proposal.proposal_id] = proposal
        logger.info(
            "[HGL] New proposal pending review: %s — %s",
            proposal.proposal_id[:8], proposal.description,
        )

        # In production: trigger UI notification / webhook
        # For now: log to audit
        await self.audit.record(AuditEvent(
            event_type="GOVERNANCE_PROPOSAL_RECEIVED",
            agent=self.agent_id,
            details={
                "proposal_id": proposal.proposal_id,
                "type": proposal.proposal_type.value,
                "description": proposal.description,
            },
        ))

    async def _handle_halt_notification(self, message: BusMessage) -> None:
        reason = message.payload.get("reason", "unknown")
        logger.critical("[HGL] SYSTEM HALT — operator action required: %s", reason)
        await self.audit.record(AuditEvent(
            event_type="GOVERNANCE_HALT_NOTIFIED",
            agent=self.agent_id,
            details={"reason": reason, "timestamp": datetime.utcnow().isoformat()},
        ))

    # ── Human approval interface ───────────────────────────────────────────────

    async def approve_proposal(
        self,
        proposal_id: str,
        approver: str,
        rationale: str = "",
    ) -> bool:
        """
        Programmatic approval — called by human operator via CLI/API.
        Moves proposal to HGL_APPROVED and forwards to Edge Research Agent.
        """
        proposal = self._pending_proposals.get(proposal_id)
        if not proposal:
            # Try loading from state store
            proposals = await self.store.load_pending_proposals()
            for p in proposals:
                if p.proposal_id == proposal_id:
                    proposal = p
                    break

        if not proposal:
            logger.error("[HGL] Proposal %s not found", proposal_id)
            return False

        # Update status
        proposal.status = ChangeProposalStatus.HGL_APPROVED
        proposal.hgl_approved_by = approver
        proposal.hgl_approved_at = datetime.utcnow()
        proposal.updated_at = datetime.utcnow()

        await self.store.upsert_change_proposal(proposal)
        self._pending_proposals.pop(proposal_id, None)

        # Record in audit log (immutable)
        await self.audit.record(governance_approval_event(
            approver=approver,
            proposal_id=proposal_id,
            proposal_type=proposal.proposal_type.value,
        ))
        if rationale:
            await self.audit.record(AuditEvent(
                event_type="GOVERNANCE_APPROVAL_RATIONALE",
                agent=self.agent_id,
                details={
                    "proposal_id": proposal_id,
                    "approver": approver,
                    "rationale": rationale,
                },
            ))

        # Forward to Edge Research Agent for re-validation
        await self.publish(
            Topic.CHANGE_PROPOSAL,
            payload=proposal.model_dump(mode="json"),
        )

        self._approved_changes.append({
            "proposal_id": proposal_id,
            "type": proposal.proposal_type.value,
            "approver": approver,
            "approved_at": datetime.utcnow().isoformat(),
            "rationale": rationale,
        })

        logger.info(
            "[HGL] Proposal %s APPROVED by %s → forwarded to Edge Research",
            proposal_id[:8], approver,
        )
        return True

    async def reject_proposal(
        self,
        proposal_id: str,
        approver: str,
        rejection_reason: str,
    ) -> bool:
        """Reject a change proposal."""
        proposal = self._pending_proposals.get(proposal_id)
        if not proposal:
            logger.error("[HGL] Proposal %s not found", proposal_id)
            return False

        proposal.status = ChangeProposalStatus.HGL_REJECTED
        proposal.hgl_rejection_reason = rejection_reason
        proposal.updated_at = datetime.utcnow()

        await self.store.upsert_change_proposal(proposal)
        self._pending_proposals.pop(proposal_id, None)

        await self.audit.record(AuditEvent(
            event_type="GOVERNANCE_PROPOSAL_REJECTED",
            agent=self.agent_id,
            details={
                "proposal_id": proposal_id,
                "approver": approver,
                "reason": rejection_reason,
            },
        ))

        logger.info(
            "[HGL] Proposal %s REJECTED by %s: %s",
            proposal_id[:8], approver, rejection_reason,
        )
        return True

    async def authorize_system_restart(self, approver: str, reason: str) -> None:
        """
        HGL authorizes system restart after a PSA halt.
        Sends resume command to PSA via the message bus.
        """
        logger.warning("[HGL] System restart authorized by %s: %s", approver, reason)
        await self.audit.record(AuditEvent(
            event_type="GOVERNANCE_RESTART_AUTHORIZED",
            agent=self.agent_id,
            details={
                "approver": approver,
                "reason": reason,
                "timestamp": datetime.utcnow().isoformat(),
            },
        ))
        await self.publish(
            Topic.SYSTEM_STATE,
            payload={
                "command": "resume",
                "approved_by": approver,
                "reason": reason,
            },
        )

    async def sign_off_shadow_criteria(
        self,
        approver: str,
        criteria: dict,
    ) -> None:
        """
        HGL signs off on shadow mode pass criteria (§8.2) BEFORE the shadow run.
        Criteria are locked at this point — cannot be retroactively adjusted.
        """
        await self.audit.record(AuditEvent(
            event_type="SHADOW_CRITERIA_SIGNED_OFF",
            agent=self.agent_id,
            details={
                "approver": approver,
                "criteria": criteria,
                "signed_at": datetime.utcnow().isoformat(),
            },
        ))
        logger.info("[HGL] Shadow mode criteria signed off by %s", approver)

    # ── Pending proposals overview ─────────────────────────────────────────────

    async def get_pending_proposals(self) -> list[ChangeProposal]:
        """Return all proposals awaiting HGL review."""
        from_memory = list(self._pending_proposals.values())
        from_store = await self.store.load_pending_proposals()
        seen = {p.proposal_id for p in from_memory}
        all_proposals = from_memory + [p for p in from_store if p.proposal_id not in seen]
        return [p for p in all_proposals if p.status == ChangeProposalStatus.AWAITING_HGL]

    async def interactive_review_loop(self) -> None:
        """
        CLI interactive loop for operator review of pending proposals.
        Run this in the terminal when reviewing proposals.
        """
        while True:
            proposals = await self.get_pending_proposals()
            if not proposals:
                print("[HGL] No proposals pending review.")
                await asyncio.sleep(60)
                continue

            print(f"\n[HGL] {len(proposals)} proposal(s) pending review:")
            for i, p in enumerate(proposals):
                print(f"  [{i+1}] {p.proposal_id[:8]} | {p.proposal_type.value}")
                print(f"       {p.description}")
                print(f"       Changes: {json.dumps(p.parameter_changes, indent=2)}")
                print()

            try:
                action = input("Enter proposal # to act on (or 'skip'): ").strip()
                if action.lower() == "skip":
                    await asyncio.sleep(60)
                    continue

                idx = int(action) - 1
                proposal = proposals[idx]

                decision = input(f"Approve (a) or Reject (r) proposal {proposal.proposal_id[:8]}? ").strip().lower()
                approver = input("Your name: ").strip()
                rationale = input("Rationale: ").strip()

                if decision == "a":
                    await self.approve_proposal(proposal.proposal_id, approver, rationale)
                elif decision == "r":
                    await self.reject_proposal(proposal.proposal_id, approver, rationale)
                else:
                    print("Unknown action — skipping")

            except (ValueError, IndexError):
                print("Invalid selection")
            except KeyboardInterrupt:
                print("\n[HGL] Exiting review loop")
                break
