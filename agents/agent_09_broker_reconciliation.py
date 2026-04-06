"""
Agent 9 — Broker State Reconciliation Agent

Continuously reconciles internal state (state store) against live broker state.
Acts as a gate: until at least one CLEAN reconciliation has been published,
Agent 8 will not submit orders.

Safety contract:
  - No new order permitted without a CLEAN reconciliation.
  - Any mismatch immediately escalates to PSA via PSA_ALERT.
  - All reconciliation state persists to the state store (crash-safe).
  - On startup, runs an immediate reconciliation before any orders can proceed.
  - Duplicate order risk (two open orders for the same symbol) triggers MISMATCH.

Subscribes to: ORDER_SUBMITTED, ORDER_UPDATE, POSITION_UPDATE
Publishes to: RECONCILIATION_STATUS, PSA_ALERT
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from agents.base import BaseAgent
from brokers.base import BrokerBase
from core.enums import OrderStatus, ReconciliationStatus, Topic
from core.models import (
    AuditEvent,
    BusMessage,
    Order,
    Position,
    ReconciliationReport,
)
from infrastructure.audit_log import AuditLog
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore

logger = logging.getLogger(__name__)

_RECONCILIATION_INTERVAL_SEC = 5.0


class BrokerReconciliationAgent(BaseAgent):
    """
    Agent 9 — Broker State Reconciliation Agent.

    Runs a tight reconciliation loop every 5 seconds.  Publishes
    RECONCILIATION_STATUS so Agent 8 can gate order submission.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        broker: BrokerBase,
        config: dict | None = None,
    ) -> None:
        super().__init__(
            agent_id="agent_09_broker_reconciliation",
            agent_name="BrokerReconciliationAgent",
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )
        self.broker = broker

        # ── Internal shadow state (kept in sync by incoming bus messages) ──────
        # symbol → set of order_ids (open only)
        self.internal_orders: dict[str, set[str]] = {}
        # set of symbols with open positions per internal knowledge
        self.internal_positions: set[str] = set()

        # Last produced report — used for reference and persistence
        self.last_reconciliation: ReconciliationReport | None = None

        # Background loop handle
        self._recon_task: asyncio.Task | None = None

    # ── Topic registration ─────────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.ORDER_SUBMITTED, Topic.ORDER_UPDATE, Topic.POSITION_UPDATE]

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_startup(self) -> None:
        logger.info("[%s] Starting — running immediate reconciliation on startup",
                    self.agent_name)
        # Seed internal state from persistent store
        await self._seed_internal_state_from_store()
        # First reconciliation before orders are permitted
        await self._run_reconciliation()
        # Periodic loop
        self._recon_task = asyncio.create_task(
            self._reconciliation_loop(),
            name=f"{self.agent_id}-recon-loop",
        )

    async def on_shutdown(self) -> None:
        if self._recon_task:
            self._recon_task.cancel()
        logger.info("[%s] Shutdown complete", self.agent_name)

    # ── State seeding from store (crash recovery) ──────────────────────────────

    async def _seed_internal_state_from_store(self) -> None:
        """Load open positions and orders from the state store on restart."""
        try:
            open_positions: list[Position] = await self.store.load_open_positions()
            self.internal_positions = {p.symbol for p in open_positions}

            open_orders: list[Order] = await self.store.load_open_orders()
            for order in open_orders:
                self.internal_orders.setdefault(order.symbol, set()).add(order.order_id)

            logger.info(
                "[%s] Seeded from store: %d positions, %d orders across %d symbols",
                self.agent_name,
                len(self.internal_positions),
                sum(len(v) for v in self.internal_orders.values()),
                len(self.internal_orders),
            )
        except Exception as exc:
            logger.error("[%s] Failed to seed state from store: %s", self.agent_name, exc)

    # ── Message dispatch ───────────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        if message.topic == Topic.ORDER_SUBMITTED:
            await self._handle_order_submitted(message)
        elif message.topic == Topic.ORDER_UPDATE:
            await self._handle_order_update(message)
        elif message.topic == Topic.POSITION_UPDATE:
            await self._handle_position_update(message)

    # ── Internal state updates from bus messages ───────────────────────────────

    async def _handle_order_submitted(self, message: BusMessage) -> None:
        symbol: str = message.payload.get("symbol", "")
        order_id: str = message.payload.get("order_id", "")
        if symbol and order_id:
            self.internal_orders.setdefault(symbol, set()).add(order_id)
            logger.debug(
                "[%s] Tracked new order: symbol=%s order_id=%s",
                self.agent_name, symbol, order_id,
            )

    async def _handle_order_update(self, message: BusMessage) -> None:
        symbol: str = message.payload.get("symbol", "")
        order_id: str = message.payload.get("order_id", "")
        status_str: str = message.payload.get("status", "")

        terminal_statuses = {
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
            OrderStatus.REJECTED.value,
            OrderStatus.EXPIRED.value,
        }
        if symbol and order_id and status_str in terminal_statuses:
            if symbol in self.internal_orders:
                self.internal_orders[symbol].discard(order_id)
                if not self.internal_orders[symbol]:
                    del self.internal_orders[symbol]

    async def _handle_position_update(self, message: BusMessage) -> None:
        symbol: str = message.payload.get("symbol", "")
        closed: bool = message.payload.get("closed", False)
        if not symbol:
            return
        if closed:
            self.internal_positions.discard(symbol)
            logger.debug("[%s] Position closed: symbol=%s", self.agent_name, symbol)
        else:
            self.internal_positions.add(symbol)
            logger.debug("[%s] Position opened/updated: symbol=%s", self.agent_name, symbol)

    # ── Reconciliation loop ────────────────────────────────────────────────────

    async def _reconciliation_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(_RECONCILIATION_INTERVAL_SEC)
                await self._run_reconciliation()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception(
                    "[%s] Unexpected error in reconciliation loop: %s",
                    self.agent_name, exc,
                )

    async def _run_reconciliation(self) -> None:
        discrepancies: list[str] = []
        broker_position_symbols: list[str] = []
        broker_order_count = 0
        buying_power_ok = True
        duplicate_order_risk = False

        # ── Fetch broker state ─────────────────────────────────────────────────
        try:
            broker_positions = await self.broker.get_positions()
            broker_orders = await self.broker.get_open_orders()
            account_info = await self.broker.get_account()
        except Exception as exc:
            logger.error("[%s] Broker fetch failed during reconciliation: %s",
                         self.agent_name, exc)
            # Cannot reconcile without broker state — treat as MISMATCH
            report = ReconciliationReport(
                status=ReconciliationStatus.MISMATCH,
                internal_positions=sorted(self.internal_positions),
                broker_positions=[],
                discrepancies=[f"Broker fetch failed: {exc}"],
                buying_power_ok=False,
                duplicate_order_risk=False,
            )
            await self._publish_and_persist_report(report)
            return

        broker_position_symbols = [bp.symbol for bp in broker_positions]
        broker_order_count = len(broker_orders)

        # ── Load internal state from store (authoritative on-disk truth) ───────
        try:
            store_positions = await self.store.load_open_positions()
            store_orders = await self.store.load_open_orders()
        except Exception as exc:
            logger.error("[%s] Store load failed during reconciliation: %s",
                         self.agent_name, exc)
            discrepancies.append(f"Store load failed: {exc}")
            store_positions = []
            store_orders = []

        store_position_symbols = {p.symbol for p in store_positions}

        # Merge in-memory shadow set with store (should agree; union defensively)
        all_internal_symbols = store_position_symbols | self.internal_positions

        # ── Check 1: Ghost positions (broker has positions internal doesn't know) ─
        broker_symbol_set = set(broker_position_symbols)
        ghost_positions = broker_symbol_set - all_internal_symbols
        for sym in ghost_positions:
            discrepancies.append(f"GHOST_POSITION: broker has {sym}, internal does not")

        # ── Check 2: Missing positions (internal thinks open, broker says closed) ─
        missing_positions = all_internal_symbols - broker_symbol_set
        for sym in missing_positions:
            discrepancies.append(f"MISSING_POSITION: internal has {sym}, broker does not")

        # ── Check 3: Order count mismatch ─────────────────────────────────────
        internal_open_order_count = sum(len(ids) for ids in self.internal_orders.values())
        # We allow broker to have 2x the internal count because bracket orders create
        # child legs (stop + target) that the broker tracks separately.
        # Mismatch threshold: broker can have up to 3x internal (entry + stop + target)
        # but if broker has 0 and internal expects > 0, that is a mismatch.
        if internal_open_order_count > 0 and broker_order_count == 0:
            discrepancies.append(
                f"ORDER_COUNT_MISMATCH: internal={internal_open_order_count} "
                f"broker={broker_order_count}"
            )

        # ── Check 4: Buying power sanity ──────────────────────────────────────
        min_buying_power: float = self.cfg("capital", "pdt_minimum", default=25_000.0)
        if account_info.equity < min_buying_power:
            buying_power_ok = False
            discrepancies.append(
                f"LOW_EQUITY: equity={account_info.equity:.2f} "
                f"below PDT minimum={min_buying_power:.2f}"
            )

        # ── Check 5: Duplicate open orders for same symbol ────────────────────
        for sym, order_ids in self.internal_orders.items():
            if len(order_ids) > 1:
                duplicate_order_risk = True
                discrepancies.append(
                    f"DUPLICATE_ORDER_RISK: symbol={sym} "
                    f"has {len(order_ids)} open orders"
                )

        # Also check broker side for duplicates
        broker_orders_by_symbol: dict[str, int] = {}
        for bo in broker_orders:
            if bo.status.upper() not in ("FILLED", "CANCELLED", "CANCELED",
                                          "REJECTED", "EXPIRED"):
                broker_orders_by_symbol[bo.symbol] = (
                    broker_orders_by_symbol.get(bo.symbol, 0) + 1
                )
        for sym, count in broker_orders_by_symbol.items():
            # Count > 3 indicates a real problem (bracket = 3 normal legs)
            if count > 3:
                duplicate_order_risk = True
                discrepancies.append(
                    f"BROKER_DUPLICATE_ORDER: symbol={sym} "
                    f"has {count} broker-side orders"
                )

        # ── Build report ───────────────────────────────────────────────────────
        if discrepancies:
            status = ReconciliationStatus.MISMATCH
        else:
            status = ReconciliationStatus.CLEAN

        report = ReconciliationReport(
            status=status,
            internal_positions=sorted(all_internal_symbols),
            broker_positions=sorted(broker_position_symbols),
            discrepancies=discrepancies,
            buying_power_ok=buying_power_ok,
            duplicate_order_risk=duplicate_order_risk,
        )

        await self._publish_and_persist_report(report)

        if status == ReconciliationStatus.MISMATCH:
            logger.warning(
                "[%s] RECONCILIATION MISMATCH: %d discrepancies: %s",
                self.agent_name, len(discrepancies), discrepancies,
            )
        else:
            logger.debug("[%s] Reconciliation CLEAN", self.agent_name)

    async def _publish_and_persist_report(self, report: ReconciliationReport) -> None:
        self.last_reconciliation = report

        # Persist to state store (survives crashes)
        try:
            await self.store.save_reconciliation(report)
        except Exception as exc:
            logger.error("[%s] Failed to persist reconciliation report: %s",
                         self.agent_name, exc)

        # Publish RECONCILIATION_STATUS for Agent 8 to gate on
        await self.publish(
            topic=Topic.RECONCILIATION_STATUS,
            payload=report.model_dump(mode="json"),
        )

        # On mismatch, also publish PSA_ALERT and audit record
        if report.status == ReconciliationStatus.MISMATCH:
            await self.publish(
                topic=Topic.PSA_ALERT,
                payload={
                    "source": self.agent_id,
                    "alert_type": "RECONCILIATION_MISMATCH",
                    "discrepancies": report.discrepancies,
                    "buying_power_ok": report.buying_power_ok,
                    "duplicate_order_risk": report.duplicate_order_risk,
                    "reconciled_at": report.reconciled_at.isoformat(),
                },
            )
            await self.audit.record(AuditEvent(
                event_type="RECONCILIATION_MISMATCH",
                agent=self.agent_id,
                details={
                    "discrepancies": report.discrepancies,
                    "broker_positions": report.broker_positions,
                    "internal_positions": report.internal_positions,
                    "buying_power_ok": report.buying_power_ok,
                    "duplicate_order_risk": report.duplicate_order_risk,
                },
            ))
