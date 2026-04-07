"""
Agent 8 — Execution Agent (EA)

Places orders safely and efficiently; only after confirming broker state.

Safety contract:
  - No position without stop. Bracket order guarantees this structurally.
  - No market order entries. Market orders used for emergency flatten only.
  - No duplicate submissions. Checked against pending_orders per symbol.
  - No execution if reconciliation dirty. reconciliation_clean gate enforced.
  - Price chase limit enforced (max_price_chase_bps from plan.entry_price).
  - Every order and fill is immutably recorded to the audit log.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from agents.base import BaseAgent
from brokers.base import BrokerBase, AccountInfo
from core.enums import (
    Direction,
    ExitMode,
    OrderSide,
    OrderStatus,
    OrderType,
    ReconciliationStatus,
    TimeInForce,
    Topic,
)
from core.exceptions import (
    OrderSubmissionError,
    PriceChaseLimitError,
    PositionWithoutStopError,
    ReconciliationMismatchError,
)
from core.models import (
    AuditEvent,
    BusMessage,
    FillEvent,
    Order,
    TradePlan,
)
from infrastructure.audit_log import AuditLog, order_submitted_event, order_filled_event
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore

logger = logging.getLogger(__name__)

# How often (seconds) to poll open orders for status updates.
_ORDER_POLL_INTERVAL_SEC = 2.0


class ExecutionAgent(BaseAgent):
    """
    Agent 8 — Execution Agent.

    Subscribes to TRADE_PLAN and RECONCILIATION_STATUS.
    Publishes ORDER_SUBMITTED, ORDER_UPDATE, FILL_EVENT.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        broker: BrokerBase,
        data_feed: Any,          # DataFeed — for re-fetching latest tick
        config: dict | None = None,
        sor: "Any | None" = None,  # ToxicitySOR | None — optional toxicity-aware SOR
    ) -> None:
        super().__init__(
            agent_id="agent_08_execution",
            agent_name="ExecutionAgent",
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )
        self.broker = broker
        self.data_feed = data_feed
        self._sor = sor

        # ── State ──────────────────────────────────────────────────────────────
        # Gate: remains False until at least one CLEAN reconciliation received.
        self.reconciliation_clean: bool = False

        # symbol → Order (only the entry leg tracked here; bracket ties stop/target)
        self.pending_orders: dict[str, Order] = {}

        # order_id → asyncio.Task (timeout watchdog per order)
        self._timeout_tasks: dict[str, asyncio.Task] = {}

        # Background polling task handle
        self._poll_task: asyncio.Task | None = None

        # Execution quality flag received from Agent 10
        self._quality_degraded: bool = False

    # ── Topic registration ─────────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.TRADE_PLAN, Topic.RECONCILIATION_STATUS, Topic.EXECUTION_QUALITY]

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_startup(self) -> None:
        logger.info("[%s] Starting — reconciliation_clean=False until first CLEAN report",
                    self.agent_name)
        self._poll_task = asyncio.create_task(
            self._order_status_poll_loop(),
            name=f"{self.agent_id}-order-poll",
        )

    async def on_shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        for task in self._timeout_tasks.values():
            task.cancel()
        logger.info("[%s] Shutdown complete", self.agent_name)

    # ── Main dispatch ──────────────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        if message.topic == Topic.RECONCILIATION_STATUS:
            await self._handle_reconciliation_status(message)
        elif message.topic == Topic.TRADE_PLAN:
            await self._handle_trade_plan(message)
        elif message.topic == Topic.EXECUTION_QUALITY:
            await self._handle_execution_quality(message)

    # ── Reconciliation gate ────────────────────────────────────────────────────

    async def _handle_reconciliation_status(self, message: BusMessage) -> None:
        status_str = message.payload.get("status")
        if status_str == ReconciliationStatus.CLEAN.value:
            if not self.reconciliation_clean:
                logger.info("[%s] First CLEAN reconciliation received — gate open",
                            self.agent_name)
            self.reconciliation_clean = True
        elif status_str == ReconciliationStatus.MISMATCH.value:
            if self.reconciliation_clean:
                logger.warning(
                    "[%s] Reconciliation MISMATCH received — gate CLOSED until next CLEAN",
                    self.agent_name,
                )
            self.reconciliation_clean = False
            discrepancies = message.payload.get("discrepancies", [])
            await self.audit.record(AuditEvent(
                event_type="RECON_MISMATCH_GATE_CLOSED",
                agent=self.agent_id,
                details={"discrepancies": discrepancies},
            ))

    # ── Execution quality feedback ─────────────────────────────────────────────

    async def _handle_execution_quality(self, message: BusMessage) -> None:
        degraded = message.payload.get("quality_degraded", False)
        if degraded and not self._quality_degraded:
            avg_slip = message.payload.get("avg_slippage_bps", 0)
            logger.warning(
                "[%s] Execution quality degraded — avg_slippage=%.1f bps. "
                "SPA handles tolerance adjustment.",
                self.agent_name, avg_slip,
            )
        self._quality_degraded = degraded

    # ── Trade plan handling ────────────────────────────────────────────────────

    async def _handle_trade_plan(self, message: BusMessage) -> None:
        try:
            plan = TradePlan.model_validate(message.payload)
        except Exception as exc:
            logger.error("[%s] Could not parse TradePlan: %s", self.agent_name, exc)
            return

        symbol = plan.symbol

        # ── Safety gate 1: Reconciliation ─────────────────────────────────────
        if not self.reconciliation_clean:
            logger.warning(
                "[%s] BLOCKED plan_id=%s symbol=%s: awaiting reconciliation",
                self.agent_name, plan.plan_id, symbol,
            )
            await self.audit.record(AuditEvent(
                event_type="ORDER_BLOCKED_RECON",
                agent=self.agent_id,
                symbol=symbol,
                details={"plan_id": plan.plan_id, "reason": "awaiting reconciliation"},
            ))
            return

        # ── Safety gate 2: Duplicate check ────────────────────────────────────
        if symbol in self.pending_orders:
            logger.warning(
                "[%s] SKIPPED plan_id=%s symbol=%s: duplicate pending order exists",
                self.agent_name, plan.plan_id, symbol,
            )
            return

        # ── Safety gate 3: Broker account / buying power ──────────────────────
        try:
            account: AccountInfo = await self.broker.get_account()
        except Exception as exc:
            logger.error(
                "[%s] Could not fetch account for plan_id=%s: %s",
                self.agent_name, plan.plan_id, exc,
            )
            return

        order_value = plan.entry_price * plan.shares
        if account.buying_power < order_value:
            logger.warning(
                "[%s] BLOCKED plan_id=%s symbol=%s: insufficient buying power "
                "(need=%.2f available=%.2f)",
                self.agent_name, plan.plan_id, symbol, order_value, account.buying_power,
            )
            await self.audit.record(AuditEvent(
                event_type="ORDER_BLOCKED_BUYING_POWER",
                agent=self.agent_id,
                symbol=symbol,
                details={
                    "plan_id": plan.plan_id,
                    "required": order_value,
                    "available": account.buying_power,
                },
            ))
            return

        # ── Safety gate 4: Spread tolerance check ─────────────────────────────
        max_spread_bps: float = self.cfg("universe", "max_spread_bps", default=20.0)
        try:
            tick = await self.data_feed.get_latest_tick(symbol)
            if tick and tick.spread_bps > max_spread_bps:
                logger.warning(
                    "[%s] BLOCKED plan_id=%s symbol=%s: spread %.1f bps > max %.1f bps",
                    self.agent_name, plan.plan_id, symbol,
                    tick.spread_bps, max_spread_bps,
                )
                return
        except Exception as exc:
            logger.warning(
                "[%s] Could not verify spread for %s: %s — proceeding with caution",
                self.agent_name, symbol, exc,
            )
            tick = None

        # ── Safety gate 5: Price chase check ──────────────────────────────────
        max_chase_bps: float = self.cfg("execution", "max_price_chase_bps", default=10.0)
        if tick is not None:
            current_mid = tick.mid
            chase_bps = abs(current_mid - plan.entry_price) / plan.entry_price * 10_000
            if chase_bps > max_chase_bps:
                logger.warning(
                    "[%s] BLOCKED plan_id=%s symbol=%s: price chased %.1f bps (max %.1f)",
                    self.agent_name, plan.plan_id, symbol, chase_bps, max_chase_bps,
                )
                await self.audit.record(AuditEvent(
                    event_type="ORDER_BLOCKED_PRICE_CHASE",
                    agent=self.agent_id,
                    symbol=symbol,
                    details={
                        "plan_id": plan.plan_id,
                        "chase_bps": round(chase_bps, 2),
                        "limit_bps": max_chase_bps,
                    },
                ))
                return

        # ── Safety gate 6: Stop price must be set (structural invariant) ───────
        if plan.stop_price <= 0:
            raise PositionWithoutStopError(symbol)

        # ── Submit bracket order ───────────────────────────────────────────────
        side = OrderSide.BUY if plan.direction == Direction.LONG else OrderSide.SELL_SHORT
        order_timeout_sec: int = self.cfg("execution", "order_timeout_sec", default=30)

        order = Order(
            plan_id=plan.plan_id,
            symbol=symbol,
            side=side,
            order_type=OrderType.BRACKET,
            qty=plan.shares,
            limit_price=plan.entry_price,
            stop_price=plan.stop_price,
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.PENDING,
            submitted_at=datetime.utcnow(),
        )

        if self._sor is not None:
            decision = self._sor.route(symbol, side.value, float(plan.shares), is_aggressive=True)
            logger.info(
                "[EA] SOR venue=%s toxicity=%.3f reason=%s",
                decision.venue.value, decision.toxicity_score, decision.reason,
            )

        try:
            broker_order = await self.broker.submit_bracket_order(
                symbol=symbol,
                side=side,
                qty=plan.shares,
                limit_price=plan.entry_price,
                stop_loss_price=plan.stop_price,
                take_profit_price=plan.target_price,
                time_in_force=TimeInForce.DAY,
                client_order_id=order.order_id,
            )
        except Exception as exc:
            logger.error(
                "[%s] Bracket order submission failed for %s: %s",
                self.agent_name, symbol, exc,
            )
            await self.audit.record(AuditEvent(
                event_type="ORDER_SUBMISSION_FAILED",
                agent=self.agent_id,
                symbol=symbol,
                details={"plan_id": plan.plan_id, "error": str(exc)},
            ))
            raise OrderSubmissionError(symbol, str(exc)) from exc

        # Stamp broker IDs back onto the internal order
        order.broker_order_id = broker_order.broker_order_id
        order.status = OrderStatus.SUBMITTED

        # Persist to state store
        await self.store.upsert_order(order)

        # Track in-memory
        self.pending_orders[symbol] = order

        # Immutable audit record
        await self.audit.record(order_submitted_event(
            agent=self.agent_id,
            order_id=order.order_id,
            symbol=symbol,
            details={
                "plan_id": plan.plan_id,
                "broker_order_id": broker_order.broker_order_id,
                "side": side.value,
                "qty": plan.shares,
                "limit_price": plan.entry_price,
                "stop_price": plan.stop_price,
                "target_price": plan.target_price,
            },
        ))

        # Publish ORDER_SUBMITTED
        await self.publish(
            topic=Topic.ORDER_SUBMITTED,
            payload={
                **order.model_dump(mode="json"),
                "plan_id": plan.plan_id,
                "entry_price": plan.entry_price,
                "stop_price": plan.stop_price,
                "target_price": plan.target_price,
                "submitted_at": order.submitted_at.isoformat()
                    if order.submitted_at else None,
            },
            correlation_id=plan.signal_id,
        )

        logger.info(
            "[%s] Bracket order submitted: plan_id=%s symbol=%s qty=%d "
            "entry=%.4f stop=%.4f target=%.4f broker_id=%s",
            self.agent_name, plan.plan_id, symbol, plan.shares,
            plan.entry_price, plan.stop_price, plan.target_price,
            broker_order.broker_order_id,
        )

        # Start timeout watchdog
        timeout_task = asyncio.create_task(
            self._order_timeout_watchdog(
                order=order,
                timeout_sec=order_timeout_sec,
            ),
            name=f"{self.agent_id}-timeout-{order.order_id}",
        )
        self._timeout_tasks[order.order_id] = timeout_task

    # ── Order status polling loop ──────────────────────────────────────────────

    async def _order_status_poll_loop(self) -> None:
        """
        Every _ORDER_POLL_INTERVAL_SEC seconds, poll open orders from the broker,
        publish ORDER_UPDATE events, and emit FILL_EVENT for filled orders.
        """
        while self._running:
            try:
                await asyncio.sleep(_ORDER_POLL_INTERVAL_SEC)
                await self._poll_open_orders()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[%s] Order poll loop error: %s", self.agent_name, exc)

    async def _poll_open_orders(self) -> None:
        if not self.pending_orders:
            return

        try:
            broker_orders = await self.broker.get_open_orders()
        except Exception as exc:
            logger.warning("[%s] Could not fetch open orders: %s", self.agent_name, exc)
            return

        # Index broker orders by client_order_id for fast lookup
        broker_by_client: dict[str, Any] = {
            bo.client_order_id: bo for bo in broker_orders
        }

        symbols_to_remove: list[str] = []

        for symbol, order in list(self.pending_orders.items()):
            if not order.broker_order_id:
                continue

            # Fetch latest state for this specific order
            try:
                broker_order = await self.broker.get_order(order.broker_order_id)
            except Exception as exc:
                logger.warning(
                    "[%s] Could not fetch order %s: %s",
                    self.agent_name, order.broker_order_id, exc,
                )
                continue

            if broker_order is None:
                continue

            prev_status = order.status
            new_status_str = broker_order.status.upper()

            # Map broker status string to internal OrderStatus
            status_map = {
                "PENDING": OrderStatus.PENDING,
                "SUBMITTED": OrderStatus.SUBMITTED,
                "ACCEPTED": OrderStatus.SUBMITTED,
                "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
                "FILLED": OrderStatus.FILLED,
                "CANCELLED": OrderStatus.CANCELLED,
                "CANCELED": OrderStatus.CANCELLED,
                "REJECTED": OrderStatus.REJECTED,
                "EXPIRED": OrderStatus.EXPIRED,
            }
            new_status = status_map.get(new_status_str, order.status)

            if new_status == prev_status and broker_order.filled_qty == order.filled_qty:
                continue  # No change — skip publishing

            # Update internal order
            order.status = new_status
            order.filled_qty = broker_order.filled_qty
            if broker_order.avg_fill_price is not None:
                order.avg_fill_price = broker_order.avg_fill_price
            if broker_order.filled_at is not None:
                order.filled_at = broker_order.filled_at

            # Persist updated state
            await self.store.upsert_order(order)

            # Publish ORDER_UPDATE
            await self.publish(
                topic=Topic.ORDER_UPDATE,
                payload=order.model_dump(mode="json"),
            )

            logger.info(
                "[%s] ORDER_UPDATE symbol=%s order_id=%s status=%s filled=%d/%d",
                self.agent_name, symbol, order.order_id,
                new_status.value, order.filled_qty, order.qty,
            )

            # Handle fill
            if new_status == OrderStatus.FILLED and prev_status != OrderStatus.FILLED:
                await self._handle_fill(order)
                symbols_to_remove.append(symbol)
                # Cancel the timeout task if still running
                if order.order_id in self._timeout_tasks:
                    self._timeout_tasks[order.order_id].cancel()
                    del self._timeout_tasks[order.order_id]

            # Handle terminal non-fill states
            elif new_status in (OrderStatus.CANCELLED, OrderStatus.REJECTED,
                                OrderStatus.EXPIRED):
                symbols_to_remove.append(symbol)
                if order.order_id in self._timeout_tasks:
                    self._timeout_tasks[order.order_id].cancel()
                    del self._timeout_tasks[order.order_id]
                await self.audit.record(AuditEvent(
                    event_type=f"ORDER_{new_status.value.upper()}",
                    agent=self.agent_id,
                    symbol=symbol,
                    details={
                        "order_id": order.order_id,
                        "broker_order_id": order.broker_order_id,
                    },
                ))

        for symbol in symbols_to_remove:
            self.pending_orders.pop(symbol, None)

    async def _handle_fill(self, order: Order) -> None:
        """Emit a FILL_EVENT for Agent 10 latency measurement."""
        fill = FillEvent(
            order_id=order.order_id,
            symbol=order.symbol,
            qty=order.filled_qty,
            price=order.avg_fill_price or 0.0,
            side=order.side,
            filled_at=order.filled_at or datetime.utcnow(),
        )
        # Audit
        await self.audit.record(order_filled_event(
            agent=self.agent_id,
            order_id=order.order_id,
            symbol=order.symbol,
            fill_price=fill.price,
            qty=fill.qty,
        ))
        # Publish fill event (Agent 10 measures latency, Agent 11 opens position monitor)
        await self.publish(
            topic=Topic.FILL_EVENT,
            payload=fill.model_dump(mode="json"),
        )
        logger.info(
            "[%s] FILL_EVENT symbol=%s order_id=%s price=%.4f qty=%d",
            self.agent_name, order.symbol, order.order_id, fill.price, fill.qty,
        )

    # ── Order timeout watchdog ─────────────────────────────────────────────────

    async def _order_timeout_watchdog(self, order: Order, timeout_sec: int) -> None:
        """Cancel the bracket order if it is not filled within timeout_sec."""
        await asyncio.sleep(timeout_sec)

        # Check if still in our pending set (i.e., not yet filled or cancelled)
        if order.symbol not in self.pending_orders:
            return
        if self.pending_orders.get(order.symbol) is not order:
            return

        # Re-check broker status before cancelling
        try:
            broker_order = await self.broker.get_order(order.broker_order_id or "")
            if broker_order and broker_order.status.upper() in ("FILLED", "CANCELLED",
                                                                 "CANCELED", "REJECTED"):
                return  # Already terminal — let the poll loop clean it up
        except Exception:
            pass  # Can't reach broker; proceed with cancel attempt

        logger.warning(
            "[%s] ORDER TIMEOUT: order_id=%s symbol=%s — cancelling after %ds",
            self.agent_name, order.order_id, order.symbol, timeout_sec,
        )
        try:
            if order.broker_order_id:
                cancelled = await self.broker.cancel_order(order.broker_order_id)
                logger.info(
                    "[%s] Cancel result for order_id=%s: %s",
                    self.agent_name, order.order_id, cancelled,
                )
        except Exception as exc:
            logger.error(
                "[%s] Failed to cancel timed-out order %s: %s",
                self.agent_name, order.order_id, exc,
            )

        # Update internal state
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = datetime.utcnow()
        await self.store.upsert_order(order)
        self.pending_orders.pop(order.symbol, None)

        await self.audit.record(AuditEvent(
            event_type="ORDER_TIMEOUT_CANCELLED",
            agent=self.agent_id,
            symbol=order.symbol,
            details={
                "order_id": order.order_id,
                "timeout_sec": timeout_sec,
            },
        ))
        await self.publish(
            topic=Topic.ORDER_UPDATE,
            payload=order.model_dump(mode="json"),
        )
