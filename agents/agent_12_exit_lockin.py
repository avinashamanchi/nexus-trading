"""
Agent 12 — Exit & Lock-In Agent (ELA)

Closes positions safely using one of 6 named exit modes.
Receives exit triggers from MMA or PSA and executes the close.
Enforces: no re-entry window, no target extension without removing risk first,
regime-flip sensitivity escalation.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from agents.base import BaseAgent
from core.enums import ExitMode, OrderSide, Topic
from core.exceptions import BrokerAPIError
from core.models import AuditEvent, BusMessage, ClosedTrade, Order, Position
from brokers.base import BrokerBase
from infrastructure.audit_log import trade_closed_event
from infrastructure.state_store import StateStore

logger = logging.getLogger(__name__)


class ExitLockInAgent(BaseAgent):
    """
    Agent 12: Exit & Lock-In Agent (ELA).

    Executes position closes via 6 named exit modes:
      1. PROFIT_TARGET    — planned level reached
      2. TIME_STOP        — maximum holding time expired
      3. MOMENTUM_FAILURE — directional edge gone
      4. VOLATILITY_SHOCK — adverse excursion velocity exceeded
      5. REGIME_FLIP      — escalated sensitivity, exit on risk trigger
      6. PORTFOLIO_RISK_BREACH — PSA-level override
    """

    def __init__(self, broker: BrokerBase, **kwargs) -> None:
        super().__init__(**kwargs)
        self.broker = broker
        # symbol → datetime: when no-re-entry window expires
        self._no_reentry_until: dict[str, datetime] = {}
        # position_id → Position (active positions this agent tracks)
        self._active_positions: dict[str, Position] = {}
        # order_id → submitted Order
        self._pending_exits: dict[str, str] = {}  # order_id → position_id

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [
            Topic.EXIT_TRIGGERED,
            Topic.FILL_EVENT,
            Topic.POSITION_UPDATE,
            Topic.HALT_COMMAND,
        ]

    async def process(self, message: BusMessage) -> None:
        topic = message.topic

        if topic == Topic.EXIT_TRIGGERED:
            await self._handle_exit_trigger(message)

        elif topic == Topic.POSITION_UPDATE:
            await self._handle_position_update(message)

        elif topic == Topic.FILL_EVENT:
            await self._handle_fill(message)

        elif topic == Topic.HALT_COMMAND:
            await self._emergency_flatten_all(message)

    # ── Exit trigger ──────────────────────────────────────────────────────────

    async def _handle_exit_trigger(self, message: BusMessage) -> None:
        payload = message.payload
        position_id = payload.get("position_id")
        exit_mode_str = payload.get("exit_mode", "")
        symbol = payload.get("symbol", "")
        current_price = payload.get("current_price", 0.0)

        try:
            exit_mode = ExitMode(exit_mode_str)
        except ValueError:
            logger.error("Unknown exit_mode: %s", exit_mode_str)
            return

        position = self._active_positions.get(position_id)
        if not position:
            logger.warning("EXIT_TRIGGERED for unknown position %s", position_id)
            return

        logger.info(
            "[ELA] Exit triggered: %s %s mode=%s price=%.2f",
            symbol, position_id[:8], exit_mode.value, current_price,
        )

        # Regime flip: escalate sensitivity but don't auto-flatten
        if exit_mode == ExitMode.REGIME_FLIP:
            await self._handle_regime_flip_exit(position, current_price)
            return

        # All other modes: execute close
        await self._execute_exit(position, exit_mode, current_price, message.correlation_id)

    async def _handle_regime_flip_exit(self, position: Position, current_price: float) -> None:
        """
        §5.2: Regime flip is risk escalation, not auto-flatten.
        Tighten stop to 50% of original risk, compress hold time to 60s.
        """
        orig_risk = abs(position.entry_price - position.stop_price)
        new_stop_distance = orig_risk * 0.5

        from core.enums import Direction
        if position.direction == Direction.LONG:
            new_stop = current_price - new_stop_distance
        else:
            new_stop = current_price + new_stop_distance

        new_time_stop = datetime.utcnow() + timedelta(seconds=60)
        updated = position.model_copy(update={
            "stop_price": round(new_stop, 2),
            "time_stop_at": new_time_stop,
            "updated_at": datetime.utcnow(),
        })
        self._active_positions[position.position_id] = updated
        await self.store.upsert_position(updated)

        await self.publish(
            Topic.POSITION_UPDATE,
            payload={
                "position_id": position.position_id,
                "symbol": position.symbol,
                "stop_price": round(new_stop, 2),
                "time_stop_at": new_time_stop.isoformat(),
                "reason": "regime_flip_sensitivity_escalation",
            },
        )
        logger.info(
            "[ELA] Regime flip: tightened stop to %.2f, new time stop in 60s for %s",
            round(new_stop, 2), position.symbol,
        )

    async def _execute_exit(
        self,
        position: Position,
        exit_mode: ExitMode,
        current_price: float,
        correlation_id: str | None,
    ) -> None:
        """Execute position close via market order (emergency) or limit close."""
        from core.enums import Direction

        symbol = position.symbol
        qty = position.shares

        # Determine close side
        if position.direction == Direction.LONG:
            close_side = OrderSide.SELL
        else:
            close_side = OrderSide.BUY_TO_COVER

        try:
            # Use broker close_position for simplicity and atomicity
            broker_order = await self.broker.close_position(symbol)

            if broker_order:
                fill_price = broker_order.avg_fill_price or current_price
                gross_pnl = (
                    (fill_price - position.entry_price) * qty
                    if position.direction == Direction.LONG
                    else (position.entry_price - fill_price) * qty
                )
                commission = 0.0
                slippage = abs(fill_price - current_price) * qty
                net_pnl = gross_pnl - commission - slippage

                hold_sec = (datetime.utcnow() - position.opened_at).total_seconds()

                closed_trade = ClosedTrade(
                    plan_id=position.plan_id,
                    symbol=symbol,
                    setup_type=payload_setup_type(position),
                    regime=_get_regime_label(position),
                    direction=position.direction,
                    shares=qty,
                    entry_price=position.entry_price,
                    exit_price=fill_price,
                    stop_price=position.stop_price,
                    target_price=position.target_price,
                    exit_mode=exit_mode,
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    commission=commission,
                    slippage=slippage,
                    hold_time_sec=hold_sec,
                    opened_at=position.opened_at,
                )

                session_date = datetime.utcnow().strftime("%Y-%m-%d")
                await self.store.save_closed_trade(closed_trade, session_date)
                await self.store.delete_position(position.position_id)

                # Publish trade closed event
                await self.publish(
                    Topic.TRADE_CLOSED,
                    payload=closed_trade.model_dump(mode="json"),
                    correlation_id=correlation_id,
                )

                # Audit
                await self.audit.record(trade_closed_event(
                    agent=self.agent_id,
                    trade_id=closed_trade.trade_id,
                    symbol=symbol,
                    net_pnl=net_pnl,
                    exit_mode=exit_mode.value,
                ))

                # Remove from active positions
                self._active_positions.pop(position.position_id, None)

                # Set no-re-entry window for stop-outs
                if exit_mode in (ExitMode.VOLATILITY_SHOCK, ExitMode.TIME_STOP):
                    no_reentry_sec = self.cfg("risk", "no_reentry_after_stop_sec", default=180)
                    self._no_reentry_until[symbol] = (
                        datetime.utcnow() + timedelta(seconds=no_reentry_sec)
                    )
                    logger.info(
                        "[ELA] No-re-entry window set for %s (%ds)", symbol, no_reentry_sec
                    )

                logger.info(
                    "[ELA] Position closed: %s mode=%s pnl=%.2f hold=%.0fs",
                    symbol, exit_mode.value, net_pnl, hold_sec,
                )
            else:
                logger.error("[ELA] close_position(%s) returned None", symbol)

        except BrokerAPIError as exc:
            logger.error("[ELA] Broker error closing %s: %s", symbol, exc)
        except Exception as exc:
            logger.exception("[ELA] Unexpected error closing %s: %s", symbol, exc)

    # ── Position tracking ─────────────────────────────────────────────────────

    async def _handle_position_update(self, message: BusMessage) -> None:
        payload = message.payload
        position_id = payload.get("position_id")
        if position_id and position_id in self._active_positions:
            pos = self._active_positions[position_id]
            updates = {}
            if "stop_price" in payload:
                updates["stop_price"] = payload["stop_price"]
            if "trailing_stop" in payload:
                updates["trailing_stop"] = payload["trailing_stop"]
            if updates:
                self._active_positions[position_id] = pos.model_copy(update=updates)

    async def _handle_fill(self, message: BusMessage) -> None:
        """When a fill event arrives for an entry, register the position."""
        payload = message.payload
        # Fill events from EA include a position snapshot
        if "position" in payload:
            try:
                pos = Position.model_validate(payload["position"])
                self._active_positions[pos.position_id] = pos
                logger.info(
                    "[ELA] Tracking new position: %s %s",
                    pos.symbol, pos.position_id[:8],
                )
            except Exception as exc:
                logger.warning("[ELA] Could not parse position from FILL_EVENT: %s", exc)

    # ── Emergency flatten ─────────────────────────────────────────────────────

    async def _emergency_flatten_all(self, message: BusMessage) -> None:
        """PSA kill switch: flatten all positions immediately."""
        logger.critical("[ELA] EMERGENCY FLATTEN ALL — PSA kill switch activated")
        try:
            await self.broker.cancel_all_orders()
            broker_orders = await self.broker.close_all_positions()
            logger.warning(
                "[ELA] Emergency flatten: %d positions closed", len(broker_orders)
            )
            # Clear all active position tracking
            self._active_positions.clear()
        except Exception as exc:
            logger.exception("[ELA] Emergency flatten failed: %s", exc)

    # ── Re-entry gate ─────────────────────────────────────────────────────────

    def is_reentry_allowed(self, symbol: str) -> bool:
        expiry = self._no_reentry_until.get(symbol)
        if not expiry:
            return True
        if datetime.utcnow() >= expiry:
            del self._no_reentry_until[symbol]
            return True
        return False


# ─── Helpers ──────────────────────────────────────────────────────────────────

def payload_setup_type(position: Position):
    """Extract setup type — stored in plan metadata (use a default if unavailable)."""
    from core.enums import SetupType
    return SetupType.BREAKOUT  # placeholder; real impl reads from TradePlan


def _get_regime_label(position: Position):
    from core.enums import RegimeLabel
    return RegimeLabel.TREND_DAY  # placeholder; real impl reads from TradePlan
