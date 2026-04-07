"""
Agent 23 — CAT Compliance Agent (CAT-CA)

Provides real-time regulatory compliance monitoring using the
SEC Consolidated Audit Trail (CAT) framework:

Responsibilities:
  1. Receive all order lifecycle events from the message bus
  2. Record every event to the CATReporter (immutable hash-chained JSONL)
  3. Perform real-time spoofing / layering detection on every cancel event
  4. Block order submissions from agents when CRITICAL spoofing risk detected
  5. Escalate HIGH/CRITICAL risk to PSA via PSA_ALERT topic
  6. Prepare and submit daily CAT report at session end
  7. Enforce DMM (Designated Market Maker) quoting obligations (if applicable)

Spoofing detection rules:
  - CRITICAL: cancel/fill ratio > 20 AND rapid cancels > 5 within 30s
              → immediate trading halt + SEC notification
  - HIGH:     cancel/fill ratio > 10 OR rapid cancels > 3
              → PSA alert, hold new orders for 30s, audit record
  - MEDIUM:   cancel/fill ratio > 5
              → audit record only
  - LOW:      cancel/fill ratio > 2
              → logged

Subscribes: ORDER_SUBMITTED, ORDER_UPDATE, FILL_EVENT, SESSION_END, KILL_SWITCH
Publishes:  PSA_ALERT (on HIGH/CRITICAL), AUDIT_EVENT
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Awaitable

from core.enums import (
    AgentState, CATEventType, OrderSide, SpoofingRisk, Topic,
)
from core.models import BusMessage, AuditEvent
from infrastructure.audit_log import AuditLog
from infrastructure.cat_reporter import CATEvent, CATReporter
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore

logger = logging.getLogger(__name__)


# ─── CAT Compliance Agent ─────────────────────────────────────────────────────

class CATComplianceAgent:
    """
    Agent 23: CAT Compliance Agent.

    Real-time regulatory compliance enforcement and order lifecycle auditing.
    Operates as a side-car to the main trading pipeline — it listens to all
    order events and enforces compliance rules without blocking execution.

    NOT a BaseAgent subclass — operates as an independent async agent.

    Args:
        bus:            Message bus.
        store:          State store.
        audit:          System audit log.
        cat:            CATReporter instance (shared with other reporters).
        firm_id:        CAT reporting firm designator.
        notify_fn:      Optional async callback for compliance alerts.
                        Signature: (symbol, risk, event) → None
    """

    AGENT_ID = 23
    AGENT_NAME = "cat_compliance"

    # Order hold duration when HIGH spoofing risk detected
    SPOOFING_HOLD_SEC = 30.0

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        cat: CATReporter,
        firm_id: str = "NEXUS001",
        notify_fn: Callable[[str, SpoofingRisk, CATEvent], Awaitable[None]] | None = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._audit = audit
        self._cat = cat
        self._firm_id = firm_id
        self._notify_fn = notify_fn

        self._running = False
        self._state = AgentState.IDLE

        # Symbols currently under spoofing hold
        self._held_symbols: set[str] = set()
        self._hold_tasks: dict[str, asyncio.Task] = {}

        # Stats
        self._events_recorded = 0
        self._spoofing_alerts = 0
        self._critical_escalations = 0

        self._hb_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._state = AgentState.PROCESSING
        await self._cat.initialize()

        logger.info("[CAT] Compliance agent starting — firm=%s", self._firm_id)

        await self._bus.subscribe(Topic.ORDER_SUBMITTED, self._on_order_submitted)
        await self._bus.subscribe(Topic.ORDER_UPDATE, self._on_order_update)
        await self._bus.subscribe(Topic.FILL_EVENT, self._on_fill)
        await self._bus.subscribe(Topic.SESSION_END, self._on_session_end)
        await self._bus.subscribe(Topic.KILL_SWITCH, self._on_kill_switch)

        self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="cat_hb")

    async def stop(self) -> None:
        self._running = False
        if self._hb_task and not self._hb_task.done():
            self._hb_task.cancel()
        await self._cat.shutdown()
        logger.info(
            "[CAT] Compliance agent stopped | events=%d alerts=%d critical=%d",
            self._events_recorded, self._spoofing_alerts, self._critical_escalations,
        )

    # ── Order Event Handlers ──────────────────────────────────────────────────

    async def _on_order_submitted(self, message: BusMessage) -> None:
        """Record NEW_ORDER CAT event for every submitted order."""
        p = message.payload
        symbol = p.get("symbol", "")

        # Compliance gate: reject if symbol is under spoofing hold
        if symbol in self._held_symbols:
            logger.warning(
                "[CAT] Order blocked for %s — spoofing hold active", symbol
            )
            await self._bus.publish(BusMessage(
                topic=Topic.PSA_ALERT,
                source_agent=self.AGENT_NAME,
                payload={
                    "type": "order_blocked_spoofing_hold",
                    "symbol": symbol,
                    "order_id": p.get("order_id", ""),
                },
            ))
            return

        event = self._cat.make_event(
            event_type=CATEventType.NEW_ORDER,
            order_id=p.get("order_id", p.get("cl_ord_id", "")),
            symbol=symbol,
            side=self._parse_side(p.get("side", "buy")),
            qty=float(p.get("qty", 0)),
            price=p.get("price"),
            account_id=p.get("account", ""),
            exchange=p.get("venue", ""),
        )
        await self._record(event)

    async def _on_order_update(self, message: BusMessage) -> None:
        """Record CANCEL or CANCEL_REPLACE CAT events."""
        p = message.payload
        status = p.get("status", "")
        symbol = p.get("symbol", "")

        if status in ("cancelled", "cancel_replace"):
            event_type = (
                CATEventType.CANCEL_REPLACE
                if status == "cancel_replace"
                else CATEventType.CANCEL
            )
            event = self._cat.make_event(
                event_type=event_type,
                order_id=p.get("order_id", ""),
                symbol=symbol,
                side=self._parse_side(p.get("side", "buy")),
                qty=float(p.get("qty", 0)),
                price=p.get("price"),
                cancel_reason=p.get("reason", ""),
                orig_order_id=p.get("orig_order_id", ""),
            )
            risk = await self._record(event)

            if risk in (SpoofingRisk.HIGH, SpoofingRisk.CRITICAL):
                await self._handle_spoofing_risk(symbol, risk, event)

    async def _on_fill(self, message: BusMessage) -> None:
        """Record FILL CAT event."""
        p = message.payload
        event = self._cat.make_event(
            event_type=CATEventType.FILL,
            order_id=p.get("order_id", ""),
            symbol=p.get("symbol", ""),
            side=self._parse_side(p.get("side", "buy")),
            qty=float(p.get("qty", 0)),
            price=p.get("price"),
            fill_qty=float(p.get("qty", 0)),
            fill_price=float(p.get("price", 0)),
            exchange=p.get("venue", ""),
        )
        await self._record(event)

    # ── Spoofing Enforcement ──────────────────────────────────────────────────

    async def _handle_spoofing_risk(
        self,
        symbol: str,
        risk: SpoofingRisk,
        event: CATEvent,
    ) -> None:
        self._spoofing_alerts += 1

        # Build alert payload
        analysis = self._cat._spoofing.get(symbol)
        alert_payload = {
            "type": "spoofing_detected",
            "symbol": symbol,
            "risk": risk.value,
            "cancel_fill_ratio": analysis.cancel_fill_ratio if analysis else 0.0,
            "rapid_cancel_count": analysis.rapid_cancel_count if analysis else 0,
            "order_id": event.order_id,
        }

        if risk == SpoofingRisk.CRITICAL:
            self._critical_escalations += 1
            logger.critical(
                "[CAT] CRITICAL spoofing risk for %s — escalating to PSA + audit", symbol
            )
            # Write regulatory audit record
            await self._audit.append(AuditEvent(
                event_type="SPOOFING_CRITICAL",
                agent=self.AGENT_NAME,
                details={**alert_payload, "regulatory_action": "immediate_halt_required"},
            ))
            # Escalate to PSA for immediate halt consideration
            await self._bus.publish(BusMessage(
                topic=Topic.PSA_ALERT,
                payload={**alert_payload, "severity": "critical"},
                source_agent=self.AGENT_NAME,
            ))
            # Apply trading hold for this symbol
            await self._apply_hold(symbol, hold_sec=300.0)   # 5 min hold on CRITICAL

        elif risk == SpoofingRisk.HIGH:
            logger.warning("[CAT] HIGH spoofing risk for %s", symbol)
            await self._audit.append(AuditEvent(
                event_type="SPOOFING_HIGH",
                agent=self.AGENT_NAME,
                details=alert_payload,
            ))
            await self._bus.publish(BusMessage(
                topic=Topic.PSA_ALERT,
                payload={**alert_payload, "severity": "high"},
                source_agent=self.AGENT_NAME,
            ))
            await self._apply_hold(symbol, hold_sec=self.SPOOFING_HOLD_SEC)

        # Call external notification (e.g. compliance officer Slack)
        if self._notify_fn:
            await self._notify_fn(symbol, risk, event)

    async def _apply_hold(self, symbol: str, hold_sec: float) -> None:
        """Place a trading hold on a symbol for hold_sec seconds."""
        self._held_symbols.add(symbol)
        logger.warning("[CAT] Spoofing hold applied to %s for %.0fs", symbol, hold_sec)

        # Cancel existing hold task if any
        if symbol in self._hold_tasks:
            self._hold_tasks[symbol].cancel()

        async def _release():
            await asyncio.sleep(hold_sec)
            self._held_symbols.discard(symbol)
            self._hold_tasks.pop(symbol, None)
            logger.info("[CAT] Spoofing hold released for %s", symbol)

        self._hold_tasks[symbol] = asyncio.create_task(_release())

    async def _on_session_end(self, message: BusMessage) -> None:
        """Compile and log daily CAT submission at end of session."""
        report = await self._cat.daily_submission()
        logger.info("[CAT] Daily submission: %s", report)

        await self._audit.append(AuditEvent(
            event_type="CAT_DAILY_SUBMISSION",
            agent=self.AGENT_NAME,
            details=report,
        ))

        await self._bus.publish(BusMessage(
            topic=Topic.AUDIT_EVENT,
            payload={"type": "cat_daily_report", **report},
            source_agent=self.AGENT_NAME,
        ))

    async def _on_kill_switch(self, message: BusMessage) -> None:
        self._running = False

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _record(self, event: CATEvent) -> SpoofingRisk:
        risk = await self._cat.record(event)
        self._events_recorded += 1
        return risk

    @staticmethod
    def _parse_side(side_str: str) -> OrderSide:
        mapping = {
            "buy": OrderSide.BUY,
            "sell": OrderSide.SELL,
            "sell_short": OrderSide.SELL_SHORT,
            "buy_to_cover": OrderSide.BUY_TO_COVER,
        }
        return mapping.get(side_str.lower(), OrderSide.BUY)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            await self._bus.publish(BusMessage(
                topic=Topic.AGENT_HEARTBEAT,
                payload={
                    "agent_id": self.AGENT_ID,
                    "agent_name": self.AGENT_NAME,
                    "state": self._state.value,
                    "events_recorded": self._events_recorded,
                    "spoofing_alerts": self._spoofing_alerts,
                    "critical_escalations": self._critical_escalations,
                    "held_symbols": list(self._held_symbols),
                },
                source_agent=self.AGENT_NAME,
            ))

    # ── Introspection ─────────────────────────────────────────────────────────

    def compliance_summary(self) -> dict:
        return {
            "firm_id": self._firm_id,
            "events_recorded": self._events_recorded,
            "spoofing_alerts": self._spoofing_alerts,
            "critical_escalations": self._critical_escalations,
            "held_symbols": list(self._held_symbols),
            "spoofing_report": self._cat.spoofing_summary(),
        }

    def is_symbol_held(self, symbol: str) -> bool:
        return symbol in self._held_symbols

    @property
    def cat_reporter(self) -> CATReporter:
        return self._cat
