"""
Agent 6 — Trade Eligibility & Risk Agent (TERA)

The main risk enforcement gate. Every validated signal must pass through TERA
before a trade plan is created. TERA enforces ten hard rules and maintains
stateful risk counters across the session.

Ten enforced rules (all checked for every incoming ValidatedSignal):
  1.  Daily loss cap          — daily_pnl <= -(daily_max_loss_pct * equity) → REJECT
  2.  Drawdown velocity       — sum of losses in last 180s > $300 → 5-min pause
  3.  Max concurrent trades   — open_positions >= max_concurrent_trades → REJECT
  4.  Symbol concentration    — existing position in symbol > max_symbol_concentration_pct → REJECT
  5.  Sector concentration    — same-sector count >= max_sector_concentration → REJECT
  6.  Correlation bucket      — same-theme count >= max_theme_concentration → REJECT
  7.  Consecutive losses      — consecutive_losses >= 3 → 300s cooldown
  8.  Anomaly hold            — anomaly_hold == True → REJECT
  9.  PDT limit               — pdt_trades_used >= 3 and equity < $25,000 → REJECT
  10. Wash sale               — wash_sale_flag present on symbol → REJECT

State machine:
  - TRADE_CLOSED  → update daily_pnl, consecutive_losses, loss_timestamps
  - PSA_ALERT     → if anomaly alert → set anomaly_hold = True
  - DAILY_PNL     → update daily_pnl snapshot from portfolio supervisor

Safety contract:
  Hard stop on daily loss. Velocity pause enforced. Anomaly hold blocks all
  new trades until explicitly cleared. PDT monitored. Wash sale flagged.

Subscribed topics : VALIDATED_SIGNAL, TRADE_CLOSED, PSA_ALERT, DAILY_PNL
Publishes         : RISK_DECISION
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

from core.constants import (
    AGENT_IDS,
    PDT_MINIMUM_EQUITY,
    PDT_MAX_DAY_TRADES_PER_WINDOW,
)
from core.enums import (
    RiskDecision as RiskDecisionEnum,
    RiskRejectReason,
    Topic,
    ValidationResult,
)
from core.models import (
    AuditEvent,
    BusMessage,
    ClosedTrade,
    Position,
    UniverseSnapshot,
    ValidatedSignal,
    WashSaleFlag,
)
from agents.base import BaseAgent
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NOTE: core/models.py defines CandidateSignal and ValidatedSignal but does
# NOT define a RiskDecision model.  We define it here as a local Pydantic
# model matching the pipeline contract.
# ---------------------------------------------------------------------------
from datetime import datetime as _dt
from pydantic import BaseModel as _BaseModel, Field as _Field
import uuid as _uuid


def _uid() -> str:
    return str(_uuid.uuid4())


def _now() -> _dt:
    return _dt.utcnow()


class RiskDecisionPayload(_BaseModel):
    """Published to Topic.RISK_DECISION."""
    decision_id: str = _Field(default_factory=_uid)
    signal_id: str
    symbol: str
    decision: RiskDecisionEnum
    reject_reason: RiskRejectReason | None = None
    reject_detail: str | None = None
    decided_at: _dt = _Field(default_factory=_now)


class TERAAgent(BaseAgent):
    """
    Agent 6 — Trade Eligibility & Risk Agent (TERA).

    Stateful risk gate that evaluates every validated (PASS) signal against
    ten hard risk rules and publishes an approval or rejection decision.
    """

    # ── Constructor ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        account_equity_fn: Callable[[], float],
        get_wash_sale_fn: Callable[[str], WashSaleFlag | None],
        open_positions_fn: Callable[[], list[Position]],
        current_universe_fn: Callable[[], UniverseSnapshot | None] | None = None,
        config: dict | None = None,
    ) -> None:
        super().__init__(
            agent_id=AGENT_IDS[6],
            agent_name="TERAAgent",
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )
        self._account_equity_fn = account_equity_fn
        self._get_wash_sale_fn = get_wash_sale_fn
        self._open_positions_fn = open_positions_fn
        self._current_universe_fn: Callable[[], UniverseSnapshot | None] = (
            current_universe_fn if current_universe_fn is not None else lambda: None
        )

        # ── Session-level risk state ──────────────────────────────────────────
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._anomaly_hold: bool = False
        self._pdt_trades_used: int = 0

        # Timestamps of loss events within the velocity window
        self._loss_timestamps: deque[tuple[datetime, float]] = deque()

        # Cooldown tracking
        self._drawdown_velocity_pause_until: datetime | None = None
        self._consecutive_loss_cooldown_until: datetime | None = None

        # ── Config shortcuts ──────────────────────────────────────────────────
        self._daily_max_loss_pct: float = float(
            self.cfg("capital", "daily_max_loss_pct", default=0.02)
        )
        self._drawdown_velocity_loss: float = float(
            self.cfg("risk", "drawdown_velocity_loss", default=300.0)
        )
        self._drawdown_velocity_window_sec: float = float(
            self.cfg("risk", "drawdown_velocity_window_sec", default=180)
        )
        self._drawdown_velocity_pause_sec: float = 300.0  # 5-min mandatory pause
        self._max_concurrent_trades: int = int(
            self.cfg("risk", "max_concurrent_trades", default=5)
        )
        self._max_symbol_concentration_pct: float = float(
            self.cfg("risk", "max_symbol_concentration_pct", default=0.30)
        )
        self._max_sector_concentration: int = int(
            self.cfg("risk", "max_sector_concentration", default=3)
        )
        self._max_theme_concentration: int = int(
            self.cfg("risk", "max_theme_concentration", default=3)
        )
        self._consecutive_loss_cooldown_trades: int = int(
            self.cfg("risk", "consecutive_loss_cooldown_trades", default=3)
        )
        self._consecutive_loss_cooldown_sec: float = float(
            self.cfg("risk", "consecutive_loss_cooldown_sec", default=300)
        )

    # ── Topic subscriptions ───────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [
            Topic.VALIDATED_SIGNAL,
            Topic.TRADE_CLOSED,
            Topic.PSA_ALERT,
            Topic.DAILY_PNL,
        ]

    # ── Main message handler ──────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        topic = message.topic

        if topic == Topic.VALIDATED_SIGNAL:
            await self._handle_validated_signal(message)

        elif topic == Topic.TRADE_CLOSED:
            await self._handle_trade_closed(message)

        elif topic == Topic.PSA_ALERT:
            await self._handle_psa_alert(message)

        elif topic == Topic.DAILY_PNL:
            await self._handle_daily_pnl(message)

    # ── VALIDATED_SIGNAL handler ──────────────────────────────────────────────

    async def _handle_validated_signal(self, message: BusMessage) -> None:
        try:
            validated = ValidatedSignal(**message.payload)
        except Exception as exc:
            logger.error("[TERA] Failed to parse ValidatedSignal: %s", exc)
            return

        # Only evaluate signals that passed upstream validation
        if validated.result != ValidationResult.PASS:
            logger.debug(
                "[TERA] Skipping failed signal %s (result=%s)",
                validated.signal_id, validated.result.value,
            )
            return

        candidate = validated.candidate
        symbol = candidate.symbol
        equity = self._account_equity_fn()

        # Evaluate all ten rules in order of severity
        decision, reject_reason, reject_detail = self._evaluate_all_rules(
            symbol=symbol,
            equity=equity,
        )

        payload = RiskDecisionPayload(
            signal_id=validated.signal_id,
            symbol=symbol,
            decision=decision,
            reject_reason=reject_reason,
            reject_detail=reject_detail,
        )

        await self.publish(
            Topic.RISK_DECISION,
            payload.model_dump(mode="json"),
            correlation_id=validated.signal_id,
        )

        await self.audit.record(AuditEvent(
            event_type="RISK_DECISION",
            agent=self.agent_id,
            symbol=symbol,
            details={
                "decision_id": payload.decision_id,
                "signal_id": validated.signal_id,
                "decision": decision.value,
                "reject_reason": reject_reason.value if reject_reason else None,
                "reject_detail": reject_detail,
                "daily_pnl": self._daily_pnl,
                "consecutive_losses": self._consecutive_losses,
                "anomaly_hold": self._anomaly_hold,
                "pdt_trades_used": self._pdt_trades_used,
                "account_equity": equity,
            },
        ))

        if decision == RiskDecisionEnum.APPROVED:
            logger.info("[TERA] APPROVED signal_id=%s symbol=%s", validated.signal_id, symbol)
        else:
            logger.info(
                "[TERA] REJECTED signal_id=%s symbol=%s reason=%s detail=%s",
                validated.signal_id, symbol,
                reject_reason.value if reject_reason else "unknown",
                reject_detail,
            )

    # ── TRADE_CLOSED handler ──────────────────────────────────────────────────

    async def _handle_trade_closed(self, message: BusMessage) -> None:
        """
        Update daily_pnl, consecutive_losses, and loss_timestamps when a
        trade closes.
        """
        try:
            trade = ClosedTrade(**message.payload)
        except Exception as exc:
            logger.error("[TERA] Failed to parse ClosedTrade: %s", exc)
            return

        self._daily_pnl += trade.net_pnl

        if trade.net_pnl < 0:
            self._consecutive_losses += 1
            # Record loss for velocity check
            self._loss_timestamps.append((datetime.utcnow(), abs(trade.net_pnl)))
            logger.info(
                "[TERA] Loss recorded: net_pnl=%.2f consecutive_losses=%d",
                trade.net_pnl, self._consecutive_losses,
            )
        else:
            # Winning trade resets the consecutive loss counter
            self._consecutive_losses = 0
            logger.info(
                "[TERA] Win recorded: net_pnl=%.2f consecutive_losses reset to 0",
                trade.net_pnl,
            )

        # Prune stale loss timestamps outside the velocity window
        self._prune_loss_timestamps()

    # ── PSA_ALERT handler ─────────────────────────────────────────────────────

    async def _handle_psa_alert(self, message: BusMessage) -> None:
        """
        If the Portfolio Supervisor signals an anomaly, engage anomaly hold.
        The hold must be explicitly cleared by a supervisor / HGL action.
        """
        alert_type = message.payload.get("alert_type", "")
        if "anomaly" in alert_type.lower():
            if not self._anomaly_hold:
                self._anomaly_hold = True
                logger.warning(
                    "[TERA] ANOMALY HOLD engaged — alert_type='%s'. "
                    "All new trades blocked until hold is cleared.",
                    alert_type,
                )
                await self.audit.record(AuditEvent(
                    event_type="ANOMALY_HOLD_ENGAGED",
                    agent=self.agent_id,
                    details={
                        "alert_type": alert_type,
                        "payload": message.payload,
                    },
                ))

    # ── DAILY_PNL handler ─────────────────────────────────────────────────────

    async def _handle_daily_pnl(self, message: BusMessage) -> None:
        """Sync internal daily_pnl from the Portfolio Supervisor's snapshot."""
        pnl = message.payload.get("daily_pnl")
        if pnl is not None:
            self._daily_pnl = float(pnl)
            logger.debug("[TERA] daily_pnl synced: %.2f", self._daily_pnl)

    # ── Core rule evaluation ──────────────────────────────────────────────────

    def _evaluate_all_rules(
        self,
        symbol: str,
        equity: float,
    ) -> tuple[RiskDecisionEnum, RiskRejectReason | None, str | None]:
        """
        Evaluate all ten risk rules in priority order.
        Returns (decision, reject_reason, reject_detail) on the first failure,
        or (APPROVED, None, None) if all rules pass.
        """
        now = datetime.utcnow()

        # ── Rule 1: Daily loss cap ────────────────────────────────────────────
        daily_max_loss = equity * self._daily_max_loss_pct
        if self._daily_pnl <= -daily_max_loss:
            detail = (
                f"daily_pnl={self._daily_pnl:.2f} <= "
                f"-{daily_max_loss:.2f} ({self._daily_max_loss_pct*100:.1f}% of equity)"
            )
            return RiskDecisionEnum.REJECTED, RiskRejectReason.DAILY_LOSS_CAP, detail

        # ── Rule 2: Drawdown velocity ─────────────────────────────────────────
        if self._drawdown_velocity_pause_until and now < self._drawdown_velocity_pause_until:
            remaining = (self._drawdown_velocity_pause_until - now).seconds
            detail = f"Drawdown velocity pause active for {remaining}s more"
            return RiskDecisionEnum.REJECTED, RiskRejectReason.DRAWDOWN_VELOCITY, detail

        self._prune_loss_timestamps()
        velocity_loss = sum(amt for _, amt in self._loss_timestamps)
        if velocity_loss > self._drawdown_velocity_loss:
            self._drawdown_velocity_pause_until = now + timedelta(
                seconds=self._drawdown_velocity_pause_sec
            )
            detail = (
                f"${velocity_loss:.2f} in losses in last "
                f"{self._drawdown_velocity_window_sec:.0f}s exceeds "
                f"${self._drawdown_velocity_loss:.0f} threshold — "
                f"{self._drawdown_velocity_pause_sec:.0f}s pause engaged"
            )
            logger.warning("[TERA] %s", detail)
            return RiskDecisionEnum.REJECTED, RiskRejectReason.DRAWDOWN_VELOCITY, detail

        # ── Rule 3: Max concurrent trades ─────────────────────────────────────
        open_positions = self._open_positions_fn()
        if len(open_positions) >= self._max_concurrent_trades:
            detail = (
                f"open_positions={len(open_positions)} >= "
                f"max_concurrent_trades={self._max_concurrent_trades}"
            )
            return RiskDecisionEnum.REJECTED, RiskRejectReason.MAX_CONCURRENT_TRADES, detail

        # ── Rule 4: Symbol concentration ──────────────────────────────────────
        symbol_exposure = sum(
            p.entry_price * p.shares
            for p in open_positions
            if p.symbol == symbol
        )
        if equity > 0 and (symbol_exposure / equity) > self._max_symbol_concentration_pct:
            detail = (
                f"Existing exposure in {symbol}: "
                f"${symbol_exposure:.2f} ({symbol_exposure/equity*100:.1f}% of equity) "
                f"> {self._max_symbol_concentration_pct*100:.0f}% limit"
            )
            return RiskDecisionEnum.REJECTED, RiskRejectReason.SYMBOL_CONCENTRATION, detail

        # ── Rule 5: Sector concentration ──────────────────────────────────────
        # Requires universe data to know each position's sector.
        # We store sector on Position via plan metadata — looked up from state store.
        sector = self._get_symbol_sector(symbol)
        if sector:
            same_sector_count = self._count_sector_positions(open_positions, sector)
            if same_sector_count >= self._max_sector_concentration:
                detail = (
                    f"Sector '{sector}' already has {same_sector_count} positions "
                    f"(limit={self._max_sector_concentration})"
                )
                return RiskDecisionEnum.REJECTED, RiskRejectReason.SECTOR_CONCENTRATION, detail

        # ── Rule 6: Correlation bucket (theme concentration) ──────────────────
        theme = self._get_symbol_theme(symbol)
        if theme:
            same_theme_count = self._count_theme_positions(open_positions, theme)
            if same_theme_count >= self._max_theme_concentration:
                detail = (
                    f"Theme '{theme}' already has {same_theme_count} positions "
                    f"(limit={self._max_theme_concentration})"
                )
                return RiskDecisionEnum.REJECTED, RiskRejectReason.CORRELATION_BUCKET, detail

        # ── Rule 7: Consecutive losses cooldown ───────────────────────────────
        if self._consecutive_loss_cooldown_until and now < self._consecutive_loss_cooldown_until:
            remaining = (self._consecutive_loss_cooldown_until - now).seconds
            detail = f"Consecutive loss cooldown active for {remaining}s more"
            return RiskDecisionEnum.REJECTED, RiskRejectReason.CONSECUTIVE_LOSSES, detail

        if self._consecutive_losses >= self._consecutive_loss_cooldown_trades:
            self._consecutive_loss_cooldown_until = now + timedelta(
                seconds=self._consecutive_loss_cooldown_sec
            )
            detail = (
                f"consecutive_losses={self._consecutive_losses} >= "
                f"{self._consecutive_loss_cooldown_trades} — "
                f"{self._consecutive_loss_cooldown_sec:.0f}s cooldown engaged"
            )
            logger.warning("[TERA] %s", detail)
            return RiskDecisionEnum.REJECTED, RiskRejectReason.CONSECUTIVE_LOSSES, detail

        # ── Rule 8: Anomaly hold ──────────────────────────────────────────────
        if self._anomaly_hold:
            detail = "Anomaly hold is active — all new trades blocked"
            return RiskDecisionEnum.REJECTED, RiskRejectReason.ANOMALY_HOLD, detail

        # ── Rule 9: PDT limit ─────────────────────────────────────────────────
        if equity < PDT_MINIMUM_EQUITY:
            if self._pdt_trades_used >= PDT_MAX_DAY_TRADES_PER_WINDOW:
                detail = (
                    f"PDT limit reached: {self._pdt_trades_used}/"
                    f"{PDT_MAX_DAY_TRADES_PER_WINDOW} day trades used and "
                    f"account equity ${equity:,.2f} < PDT minimum ${PDT_MINIMUM_EQUITY:,.2f}"
                )
                return RiskDecisionEnum.REJECTED, RiskRejectReason.PDT_LIMIT, detail

        # ── Rule 10: Wash sale ────────────────────────────────────────────────
        wash_flag = self._get_wash_sale_fn(symbol)
        if wash_flag is not None:
            detail = (
                f"Wash sale flag on {symbol}: last exit at {wash_flag.last_exit_at.isoformat()}, "
                f"net_loss={wash_flag.net_loss:.2f}, "
                f"window_end={wash_flag.wash_sale_window_end.isoformat()}"
            )
            return RiskDecisionEnum.REJECTED, RiskRejectReason.WASH_SALE_FLAG, detail

        # ── All rules passed ──────────────────────────────────────────────────
        return RiskDecisionEnum.APPROVED, None, None

    # ── Helper: prune stale loss timestamps ───────────────────────────────────

    def _prune_loss_timestamps(self) -> None:
        """Remove loss events outside the velocity tracking window."""
        cutoff = datetime.utcnow() - timedelta(seconds=self._drawdown_velocity_window_sec)
        while self._loss_timestamps and self._loss_timestamps[0][0] < cutoff:
            self._loss_timestamps.popleft()

    # ── Helpers: sector / theme lookup ───────────────────────────────────────

    def _get_symbol_sector(self, symbol: str) -> str | None:
        """
        Look up sector for a symbol from the current UniverseSnapshot.
        Returns None if universe is unavailable — sector check is then skipped.
        """
        universe = self._current_universe_fn()
        if universe is None:
            return None
        info = next((s for s in universe.symbols if s.symbol == symbol), None)
        return info.sector if info else None

    def _get_symbol_theme(self, symbol: str) -> str | None:
        """
        Look up correlation theme for a symbol from the current UniverseSnapshot.
        Theme is stored in the catalyst field or as a separate attribute when the
        pipeline coordinator enriches universe snapshots with theme tags.
        Returns None if not available — theme check is then skipped.
        """
        universe = self._current_universe_fn()
        if universe is None:
            return None
        info = next((s for s in universe.symbols if s.symbol == symbol), None)
        if info is None:
            return None
        # Theme is modelled as a prefix of the catalyst field (e.g. "theme:ai_hardware")
        # or falls back to the sector as a coarse bucket.
        catalyst = info.catalyst or ""
        if catalyst.startswith("theme:"):
            return catalyst[len("theme:"):]
        return info.sector  # coarse fallback: treat sector as theme bucket

    def _count_sector_positions(
        self, positions: list[Position], sector: str
    ) -> int:
        """
        Count how many open positions are in the given sector.
        Uses state store for sector metadata on each position's symbol.
        """
        count = 0
        for pos in positions:
            pos_sector = self._get_symbol_sector(pos.symbol)
            if pos_sector and pos_sector.lower() == sector.lower():
                count += 1
        return count

    def _count_theme_positions(
        self, positions: list[Position], theme: str
    ) -> int:
        """Count how many open positions share the given correlation theme."""
        count = 0
        for pos in positions:
            pos_theme = self._get_symbol_theme(pos.symbol)
            if pos_theme and pos_theme.lower() == theme.lower():
                count += 1
        return count

    # ── State management — public API for testing / supervisors ──────────────

    def clear_anomaly_hold(self) -> None:
        """Clear the anomaly hold. Should only be called by authorised supervisor."""
        self._anomaly_hold = False
        logger.info("[TERA] Anomaly hold cleared")

    def increment_pdt_counter(self) -> None:
        """Called by execution agent when a day trade is completed."""
        self._pdt_trades_used += 1
        logger.info("[TERA] PDT trade count incremented to %d", self._pdt_trades_used)

    def reset_for_new_session(self) -> None:
        """
        Reset all session-level state at the start of a new trading day.
        PDT counter is NOT reset here — that is managed on the rolling 5-day window.
        """
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._anomaly_hold = False
        self._loss_timestamps.clear()
        self._drawdown_velocity_pause_until = None
        self._consecutive_loss_cooldown_until = None
        logger.info("[TERA] Session state reset for new trading day")

    @property
    def risk_state(self) -> dict:
        """Expose current risk state snapshot for monitoring / heartbeat."""
        return {
            "daily_pnl": self._daily_pnl,
            "consecutive_losses": self._consecutive_losses,
            "anomaly_hold": self._anomaly_hold,
            "pdt_trades_used": self._pdt_trades_used,
            "velocity_losses_count": len(self._loss_timestamps),
            "velocity_loss_total": sum(amt for _, amt in self._loss_timestamps),
            "drawdown_pause_active": (
                self._drawdown_velocity_pause_until is not None
                and datetime.utcnow() < self._drawdown_velocity_pause_until
            ),
            "cooldown_active": (
                self._consecutive_loss_cooldown_until is not None
                and datetime.utcnow() < self._consecutive_loss_cooldown_until
            ),
        }
