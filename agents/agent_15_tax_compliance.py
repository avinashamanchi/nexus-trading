"""
Agent 15 — Tax & Compliance Agent

Maintains complete immutable audit trail and tracks regulatory/tax exposure
in real time. Monitors wash-sale exposure, PDT status, and audit log
completeness.

Audit logs are append-only and cannot be modified by any agent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from agents.base import BaseAgent
from core.constants import (
    FINRA_TAF_RATE,
    PDT_MAX_DAY_TRADES_PER_WINDOW,
    PDT_MINIMUM_EQUITY,
    SEC_FEE_RATE,
    WASH_SALE_DAYS,
)
from core.enums import Topic
from core.models import (
    AuditEvent,
    BusMessage,
    ClosedTrade,
    WashSaleFlag,
)
from infrastructure.audit_log import wash_sale_flag_event

logger = logging.getLogger(__name__)


class TaxComplianceAgent(BaseAgent):
    """
    Agent 15: Tax & Compliance Agent.

    Safety contract:
    - Wash-sale flags surfaced to TERA before re-entry
    - Audit logs immutable — never modified
    - PDT rule monitored; compliance flags escalated to HGL
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # symbol → most recent closed trade with loss (for wash sale tracking)
        self._recent_losses: dict[str, ClosedTrade] = {}
        # Rolling 5-day day-trade count
        self._day_trade_timestamps: list[datetime] = []
        self._total_fees_today: float = 0.0

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [
            Topic.TRADE_CLOSED,
            Topic.ORDER_SUBMITTED,
            Topic.FILL_EVENT,
            Topic.DAILY_PNL,
        ]

    async def process(self, message: BusMessage) -> None:
        topic = message.topic

        if topic == Topic.TRADE_CLOSED:
            await self._handle_trade_closed(message)

        elif topic == Topic.ORDER_SUBMITTED:
            # Record all order submissions in audit log
            payload = message.payload
            await self.audit.record(AuditEvent(
                event_type="ORDER_SUBMITTED",
                agent=self.agent_id,
                symbol=payload.get("symbol"),
                details=payload,
            ))

        elif topic == Topic.FILL_EVENT:
            await self._handle_fill(message)

        elif topic == Topic.DAILY_PNL:
            # Audit daily P/L snapshots
            payload = message.payload
            await self.audit.record(AuditEvent(
                event_type="DAILY_PNL_SNAPSHOT",
                agent=self.agent_id,
                details=payload,
            ))

    # ── Trade closed ──────────────────────────────────────────────────────────

    async def _handle_trade_closed(self, message: BusMessage) -> None:
        try:
            trade = ClosedTrade.model_validate(message.payload)
        except Exception as exc:
            logger.error("[TCA] Could not parse ClosedTrade: %s", exc)
            return

        # Record in audit log (immutable)
        await self.audit.record(AuditEvent(
            event_type="TRADE_CLOSED",
            agent=self.agent_id,
            symbol=trade.symbol,
            details=trade.model_dump(mode="json"),
        ))

        # Compute and record regulatory fees
        fees = self._compute_fees(trade)
        self._total_fees_today += fees["total"]
        await self.audit.record(AuditEvent(
            event_type="REGULATORY_FEES",
            agent=self.agent_id,
            symbol=trade.symbol,
            details={**fees, "trade_id": trade.trade_id},
        ))

        # Wash sale check: if this is a loss
        if not trade.is_winner:
            await self._flag_wash_sale(trade)

        # PDT tracking
        self._day_trade_timestamps.append(trade.closed_at)
        await self._check_pdt_exposure()

        # Check if we're re-entering a washed-out symbol (alert TERA)
        await self._check_re_entry_wash_sale(trade.symbol)

    async def _handle_fill(self, message: BusMessage) -> None:
        payload = message.payload
        await self.audit.record(AuditEvent(
            event_type="ORDER_FILLED",
            agent=self.agent_id,
            symbol=payload.get("symbol"),
            details=payload,
        ))

    # ── Wash sale ─────────────────────────────────────────────────────────────

    async def _flag_wash_sale(self, trade: ClosedTrade) -> None:
        """Flag a losing trade for wash-sale monitoring (30-day window)."""
        wash_end = trade.closed_at + timedelta(days=WASH_SALE_DAYS)
        flag = WashSaleFlag(
            symbol=trade.symbol,
            last_exit_at=trade.closed_at,
            last_exit_price=trade.exit_price,
            net_loss=trade.net_pnl,
            wash_sale_window_end=wash_end,
        )
        await self.store.upsert_wash_sale(flag)
        self._recent_losses[trade.symbol] = trade

        await self.audit.record(wash_sale_flag_event(trade.symbol, trade.net_pnl))
        logger.info(
            "[TCA] Wash-sale flag set: %s net_loss=%.2f window_end=%s",
            trade.symbol, trade.net_pnl, wash_end.date(),
        )

    async def _check_re_entry_wash_sale(self, symbol: str) -> None:
        """Alert if the system is about to re-enter a wash-sale flagged symbol."""
        flag = await self.store.get_wash_sale_flag(symbol)
        if flag:
            logger.warning(
                "[TCA] WASH SALE RISK: Re-entry in %s within 30-day window "
                "(loss=%.2f, window ends %s)",
                symbol, flag.net_loss, flag.wash_sale_window_end.date(),
            )
            await self.publish(
                Topic.PSA_ALERT,
                payload={
                    "alert_type": "wash_sale_risk",
                    "symbol": symbol,
                    "net_loss": flag.net_loss,
                    "window_end": flag.wash_sale_window_end.isoformat(),
                    "reason": f"Re-entry in {symbol} within wash-sale window",
                },
            )

    # ── PDT ───────────────────────────────────────────────────────────────────

    async def _check_pdt_exposure(self) -> None:
        """Monitor rolling 5-day day-trade count and alert before limit."""
        cutoff = datetime.utcnow() - timedelta(days=5)
        self._day_trade_timestamps = [
            ts for ts in self._day_trade_timestamps if ts >= cutoff
        ]
        count = len(self._day_trade_timestamps)

        if count >= PDT_MAX_DAY_TRADES_PER_WINDOW:
            logger.warning(
                "[TCA] PDT LIMIT: %d day trades used in rolling 5-day window",
                count,
            )
            await self.audit.record(AuditEvent(
                event_type="PDT_LIMIT_WARNING",
                agent=self.agent_id,
                details={
                    "day_trades_used": count,
                    "limit": PDT_MAX_DAY_TRADES_PER_WINDOW,
                    "window_days": 5,
                },
            ))
            await self.publish(
                Topic.PSA_ALERT,
                payload={
                    "alert_type": "pdt_limit",
                    "day_trades_used": count,
                    "reason": f"PDT limit reached: {count} day trades in 5-day window",
                },
            )

    # ── Fee computation ───────────────────────────────────────────────────────

    def _compute_fees(self, trade: ClosedTrade) -> dict[str, float]:
        """Compute SEC fee and FINRA TAF for a completed trade."""
        sale_proceeds = trade.exit_price * trade.shares
        sec_fee = SEC_FEE_RATE * sale_proceeds
        finra_taf = min(FINRA_TAF_RATE * trade.shares, 8.30)
        total = sec_fee + finra_taf
        return {
            "sec_fee": round(sec_fee, 4),
            "finra_taf": round(finra_taf, 4),
            "total": round(total, 4),
            "sale_proceeds": round(sale_proceeds, 2),
        }

    # ── Audit log completeness ────────────────────────────────────────────────

    async def verify_audit_completeness(self, session_date: str) -> bool:
        """
        Verify that all closed trades for the session have corresponding
        audit entries. Called by PSRA at session end.
        """
        trades = await self.store.load_closed_trades(session_date)
        audit_events = await self.audit.query(
            event_type="TRADE_CLOSED",
            since=datetime.strptime(session_date, "%Y-%m-%d"),
        )
        audit_trade_ids = {e["details_json"] for e in audit_events}

        all_present = True
        for trade in trades:
            # Simplified check — in production, parse details_json
            if not any(trade.trade_id in e.get("details_json", "") for e in audit_events):
                logger.error(
                    "[TCA] AUDIT GAP: Trade %s not found in audit log", trade.trade_id
                )
                all_present = False

        if all_present:
            logger.info(
                "[TCA] Audit completeness verified: all %d trades logged", len(trades)
            )
        return all_present

    @property
    def total_fees_today(self) -> float:
        return self._total_fees_today
