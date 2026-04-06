"""
Agent 11 — Micro-Monitoring Agent (MMA)

Watches every open position tick-by-tick across all exit dimensions.
Runs a tight 100ms monitoring loop for each position.

Safety contract:
  - Time stop is HARD: triggers unconditionally when now >= position.time_stop_at.
  - Price stop is always active: adverse excursion past stop_price → EXIT.
  - ATR expansion > 150% of 10-bar average → EXIT (VOLATILITY_SHOCK).
  - Spread doubling → EXIT (spread captured at entry).
  - Regime flip: does NOT auto-flatten; tightens stop to 50% of original risk
    and compresses hold time to 60 seconds from now.
  - Trailing stop: 1R profit → move stop to breakeven.  1.5R → trail at 0.5R.

Subscribes to: FILL_EVENT, REGIME_UPDATE, POSITION_UPDATE
Publishes to: EXIT_TRIGGERED, POSITION_UPDATE
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from agents.base import BaseAgent
from core.enums import Direction, ExitMode, RegimeLabel, Topic
from core.models import (
    AuditEvent,
    BarData,
    BusMessage,
    FillEvent,
    Position,
)
from infrastructure.audit_log import AuditLog
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore

logger = logging.getLogger(__name__)

# Monitoring loop interval
_MONITOR_INTERVAL_SEC = 0.1   # 100 ms

# ATR expansion factor from config key monitoring.atr_expansion_factor
_DEFAULT_ATR_EXPANSION = 1.5

# Trailing stop levels
_TRAIL_1R_MOVE_TO_BE = 1.0      # At 1R profit, move stop to breakeven
_TRAIL_1_5R_TRAIL = 1.5         # At 1.5R profit, trail at 0.5R profit

# Regime flip: tighten stop to this fraction of original risk
_REGIME_FLIP_STOP_FRACTION = 0.5
# Regime flip: compress hold time to this many seconds from now
_REGIME_FLIP_HOLD_COMPRESS_SEC = 60

# Number of bars used for ATR baseline
_ATR_WINDOW = 10


class MicroMonitoringAgent(BaseAgent):
    """
    Agent 11 — Micro-Monitoring Agent.

    Opens a monitoring slot for every filled order (FILL_EVENT),
    then watches that position every 100ms until exit.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        data_feed: Any,          # DataFeed — for current tick and bar history
        config: dict | None = None,
    ) -> None:
        super().__init__(
            agent_id="agent_11_micro_monitoring",
            agent_name="MicroMonitoringAgent",
            bus=bus,
            store=store,
            audit=audit,
            config=config,
        )
        self.data_feed = data_feed

        # ── Active positions keyed by position_id ──────────────────────────────
        self._monitors: dict[str, Position] = {}

        # Entry spread captured at position open (position_id → spread in price terms)
        self._entry_spreads: dict[str, float] = {}

        # position_id → order_id that opened it (for correlation)
        self._position_order_map: dict[str, str] = {}

        # Current regime
        self._current_regime: RegimeLabel = RegimeLabel.TREND_DAY

        # Per-symbol ATR history: symbol → list of True Range values (rolling 10)
        self._atr_history: dict[str, list[float]] = {}

        # Adverse excursion tracking for PSRA: position_id → max adverse excursion $
        self._adverse_excursion: dict[str, float] = {}

        # Positions that have been regime-flip tightened (position_id → True)
        self._regime_tightened: set[str] = set()

        # Background monitoring task
        self._monitor_task: asyncio.Task | None = None

        # Config-driven thresholds
        self._atr_expansion_factor: float = _DEFAULT_ATR_EXPANSION
        self._max_hold_time_sec: int = 300

    # ── Topic registration ─────────────────────────────────────────────────────

    @property
    def subscribed_topics(self) -> list[Topic]:
        return [Topic.FILL_EVENT, Topic.REGIME_UPDATE, Topic.POSITION_UPDATE]

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_startup(self) -> None:
        self._atr_expansion_factor = float(
            self.cfg("monitoring", "atr_expansion_factor", default=_DEFAULT_ATR_EXPANSION)
        )
        self._max_hold_time_sec = int(
            self.cfg("monitoring", "max_hold_time_sec", default=300)
        )
        self._monitor_task = asyncio.create_task(
            self._monitoring_loop(),
            name=f"{self.agent_id}-monitor-loop",
        )
        logger.info(
            "[%s] Started — ATR expansion factor=%.1fx max_hold=%ds interval=100ms",
            self.agent_name, self._atr_expansion_factor, self._max_hold_time_sec,
        )

    async def on_shutdown(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
        logger.info("[%s] Shutdown complete — %d positions were open at shutdown",
                    self.agent_name, len(self._monitors))

    # ── Main dispatch ──────────────────────────────────────────────────────────

    async def process(self, message: BusMessage) -> None:
        if message.topic == Topic.FILL_EVENT:
            await self._handle_fill_event(message)
        elif message.topic == Topic.REGIME_UPDATE:
            await self._handle_regime_update(message)
        elif message.topic == Topic.POSITION_UPDATE:
            await self._handle_position_update(message)

    # ── FILL_EVENT: open a position monitor ───────────────────────────────────

    async def _handle_fill_event(self, message: BusMessage) -> None:
        """
        A FILL_EVENT from Agent 8 means a new position is now open.
        We reconstruct a Position from state store (Agent 8 writes it there).
        """
        fill_order_id: str = message.payload.get("order_id", "")
        symbol: str = message.payload.get("symbol", "")
        fill_price: float = float(message.payload.get("price", 0.0))
        fill_qty: int = int(message.payload.get("qty", 0))

        if not symbol or fill_price <= 0 or fill_qty <= 0:
            return

        # Load the matching position from the state store
        # Agent 8 writes the position record after the fill.
        # We retry briefly in case there is a tiny write-ordering gap.
        position: Position | None = None
        for attempt in range(5):
            positions = await self.store.load_open_positions()
            for p in positions:
                if p.symbol == symbol and p.position_id not in self._monitors:
                    position = p
                    break
            if position is not None:
                break
            await asyncio.sleep(0.05)

        if position is None:
            logger.warning(
                "[%s] FILL_EVENT for symbol=%s but no matching Position in store "
                "(order_id=%s) — cannot monitor",
                self.agent_name, symbol, fill_order_id,
            )
            return

        # Capture entry spread from the live feed
        entry_spread: float = 0.0
        try:
            tick = await self.data_feed.get_latest_tick(symbol)
            if tick:
                entry_spread = tick.spread
        except Exception as exc:
            logger.debug("[%s] Could not fetch entry spread for %s: %s",
                         self.agent_name, symbol, exc)

        self._monitors[position.position_id] = position
        self._entry_spreads[position.position_id] = entry_spread
        self._position_order_map[position.position_id] = fill_order_id
        self._adverse_excursion[position.position_id] = 0.0

        logger.info(
            "[%s] Monitoring position_id=%s symbol=%s direction=%s "
            "entry=%.4f stop=%.4f target=%.4f time_stop_at=%s",
            self.agent_name, position.position_id, symbol,
            position.direction.value, position.entry_price,
            position.stop_price, position.target_price,
            position.time_stop_at.isoformat(),
        )

    # ── REGIME_UPDATE: track current regime ───────────────────────────────────

    async def _handle_regime_update(self, message: BusMessage) -> None:
        regime_str: str = message.payload.get("label", "")
        if not regime_str:
            return
        try:
            new_regime = RegimeLabel(regime_str)
        except ValueError:
            logger.warning("[%s] Unknown regime label: %s", self.agent_name, regime_str)
            return

        old_regime = self._current_regime
        self._current_regime = new_regime

        if old_regime != new_regime:
            logger.info(
                "[%s] Regime changed: %s → %s",
                self.agent_name, old_regime.value, new_regime.value,
            )
            # Per §5.2: regime flip escalates exit sensitivity for open positions
            if self._monitors:
                await self._apply_regime_flip_to_open_positions()

    # ── POSITION_UPDATE: external updates (e.g., from PSA) ───────────────────

    async def _handle_position_update(self, message: BusMessage) -> None:
        position_id: str = message.payload.get("position_id", "")
        closed: bool = message.payload.get("closed", False)

        if not position_id:
            return

        if closed:
            self._monitors.pop(position_id, None)
            self._entry_spreads.pop(position_id, None)
            self._position_order_map.pop(position_id, None)
            self._adverse_excursion.pop(position_id, None)
            self._regime_tightened.discard(position_id)
            logger.debug("[%s] Position closed externally: position_id=%s",
                         self.agent_name, position_id)
            return

        # Update our in-memory Position if the stop or target changed externally
        if position_id in self._monitors:
            try:
                raw = message.payload
                updated_pos = Position.model_validate(raw)
                self._monitors[position_id] = updated_pos
            except Exception as exc:
                logger.debug("[%s] Could not parse POSITION_UPDATE for %s: %s",
                             self.agent_name, position_id, exc)

    # ── Core monitoring loop ───────────────────────────────────────────────────

    async def _monitoring_loop(self) -> None:
        """
        Runs every 100ms.  For each monitored position, evaluates all exit conditions
        in order of priority, then updates trailing stops.
        """
        while self._running:
            try:
                await asyncio.sleep(_MONITOR_INTERVAL_SEC)
                if not self._monitors:
                    continue
                # Evaluate all positions concurrently for low latency
                tasks = [
                    self._evaluate_position(position_id, position)
                    for position_id, position in list(self._monitors.items())
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[%s] Monitoring loop error: %s", self.agent_name, exc)

    async def _evaluate_position(self, position_id: str, position: Position) -> None:
        """Evaluate all exit conditions for one position."""
        symbol = position.symbol

        # ── 1. Time stop (HARD) ────────────────────────────────────────────────
        now = datetime.utcnow()
        if now >= position.time_stop_at:
            logger.info(
                "[%s] TIME_STOP triggered: position_id=%s symbol=%s",
                self.agent_name, position_id, symbol,
            )
            await self._trigger_exit(
                position=position,
                exit_mode=ExitMode.TIME_STOP,
                reason=f"Time stop reached at {position.time_stop_at.isoformat()}",
            )
            return

        # ── Fetch current tick ─────────────────────────────────────────────────
        try:
            tick = await self.data_feed.get_latest_tick(symbol)
        except Exception as exc:
            logger.warning("[%s] Could not fetch tick for %s: %s",
                           self.agent_name, symbol, exc)
            return

        if tick is None:
            return

        current_price = tick.mid
        current_spread = tick.spread

        # Update position current price in memory
        position.current_price = current_price
        position.updated_at = now

        # ── 2. Price stop (adverse excursion past stop_price) ──────────────────
        effective_stop = position.trailing_stop if position.trailing_stop is not None \
            else position.stop_price

        stop_hit = (
            (position.direction == Direction.LONG and current_price <= effective_stop)
            or
            (position.direction == Direction.SHORT and current_price >= effective_stop)
        )
        if stop_hit:
            logger.info(
                "[%s] PRICE_STOP triggered: position_id=%s symbol=%s "
                "price=%.4f stop=%.4f",
                self.agent_name, position_id, symbol, current_price, effective_stop,
            )
            await self._trigger_exit(
                position=position,
                exit_mode=ExitMode.VOLATILITY_SHOCK,
                reason=(
                    f"Price {current_price:.4f} crossed stop {effective_stop:.4f}"
                ),
            )
            return

        # Track adverse excursion
        if position.direction == Direction.LONG:
            excursion = max(0.0, position.entry_price - current_price) * position.shares
        else:
            excursion = max(0.0, current_price - position.entry_price) * position.shares
        prev_excursion = self._adverse_excursion.get(position_id, 0.0)
        self._adverse_excursion[position_id] = max(prev_excursion, excursion)

        # ── 3. ATR expansion check ─────────────────────────────────────────────
        atr_exit = await self._check_atr_expansion(symbol)
        if atr_exit:
            logger.info(
                "[%s] ATR_EXPANSION exit: position_id=%s symbol=%s",
                self.agent_name, position_id, symbol,
            )
            await self._trigger_exit(
                position=position,
                exit_mode=ExitMode.VOLATILITY_SHOCK,
                reason="ATR expansion exceeded threshold",
            )
            return

        # ── 4. Spread doubling ─────────────────────────────────────────────────
        entry_spread = self._entry_spreads.get(position_id, 0.0)
        if entry_spread > 0 and current_spread > 2.0 * entry_spread:
            logger.info(
                "[%s] SPREAD_DOUBLING exit: position_id=%s symbol=%s "
                "entry_spread=%.5f current_spread=%.5f",
                self.agent_name, position_id, symbol,
                entry_spread, current_spread,
            )
            await self._trigger_exit(
                position=position,
                exit_mode=ExitMode.VOLATILITY_SHOCK,
                reason=(
                    f"Spread doubled: entry={entry_spread:.5f} "
                    f"current={current_spread:.5f}"
                ),
            )
            return

        # ── 5. Trailing stop update ────────────────────────────────────────────
        await self._update_trailing_stop(position_id, position, current_price)

    # ── ATR expansion ──────────────────────────────────────────────────────────

    async def _check_atr_expansion(self, symbol: str) -> bool:
        """
        Return True if current ATR > atr_expansion_factor * avg of last 10 bars.

        True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        ATR = mean of last 10 TRs
        """
        try:
            bars: list[BarData] = await self.data_feed.get_recent_bars(
                symbol, timeframe="1m", limit=_ATR_WINDOW + 1
            )
        except Exception as exc:
            logger.debug("[%s] Could not fetch bars for ATR check %s: %s",
                         self.agent_name, symbol, exc)
            return False

        if len(bars) < 2:
            return False

        # Compute True Range for each bar (need prev_close)
        true_ranges: list[float] = []
        for i in range(1, len(bars)):
            bar = bars[i]
            prev_close = bars[i - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            true_ranges.append(tr)

        if not true_ranges:
            return False

        # Use last bar's TR as "current ATR" and the mean of all computed TRs
        # as the baseline.  Both come from the same 10-bar window.
        current_tr = true_ranges[-1]
        avg_atr = sum(true_ranges) / len(true_ranges)

        # Update ATR history for PSRA / audit reference
        self._atr_history.setdefault(symbol, [])
        self._atr_history[symbol] = true_ranges  # keep latest window

        if avg_atr <= 0:
            return False

        expansion_ratio = current_tr / avg_atr
        if expansion_ratio > self._atr_expansion_factor:
            logger.debug(
                "[%s] ATR expansion %.2fx > threshold %.2fx for %s",
                self.agent_name, expansion_ratio, self._atr_expansion_factor, symbol,
            )
            return True

        return False

    # ── Trailing stop logic ────────────────────────────────────────────────────

    async def _update_trailing_stop(
        self, position_id: str, position: Position, current_price: float
    ) -> None:
        """
        Implement the 1R/1.5R trailing stop progression.

        - 1R of profit (price moved 1x risk in our favour): move stop to breakeven.
        - 1.5R of profit: trail stop at 0.5R profit from entry.

        Publishes POSITION_UPDATE if the stop changes.
        """
        risk_per_share = abs(position.entry_price - position.stop_price)
        if risk_per_share <= 0:
            return

        if position.direction == Direction.LONG:
            profit_per_share = current_price - position.entry_price
            be_stop = position.entry_price                          # breakeven
            trail_stop = position.entry_price + 0.5 * risk_per_share  # 0.5R lock-in
        else:
            profit_per_share = position.entry_price - current_price
            be_stop = position.entry_price
            trail_stop = position.entry_price - 0.5 * risk_per_share

        profit_in_r = profit_per_share / risk_per_share
        current_trailing = position.trailing_stop

        new_stop: float | None = None

        if profit_in_r >= _TRAIL_1_5R_TRAIL:
            # Trail at 0.5R profit
            if position.direction == Direction.LONG:
                candidate = trail_stop
                if current_trailing is None or candidate > current_trailing:
                    new_stop = candidate
            else:
                candidate = trail_stop
                if current_trailing is None or candidate < current_trailing:
                    new_stop = candidate

        elif profit_in_r >= _TRAIL_1R_MOVE_TO_BE:
            # Move to breakeven
            if position.direction == Direction.LONG:
                candidate = be_stop
                if current_trailing is None or candidate > current_trailing:
                    new_stop = candidate
            else:
                candidate = be_stop
                if current_trailing is None or candidate < current_trailing:
                    new_stop = candidate

        if new_stop is not None:
            position.trailing_stop = new_stop
            position.updated_at = datetime.utcnow()

            logger.info(
                "[%s] TRAILING_STOP updated: position_id=%s symbol=%s "
                "new_trailing=%.4f profit_in_r=%.2fR",
                self.agent_name, position_id, position.symbol,
                new_stop, profit_in_r,
            )
            # Persist and broadcast
            await self.store.upsert_position(position)
            await self.publish(
                topic=Topic.POSITION_UPDATE,
                payload={
                    **position.model_dump(mode="json"),
                    "trailing_stop_updated": True,
                },
            )

    # ── Regime flip handling ───────────────────────────────────────────────────

    async def _apply_regime_flip_to_open_positions(self) -> None:
        """
        Per §5.2: on regime change, do NOT auto-flatten.
        Instead, tighten each open position's stop to 50% of original risk
        and compress its time stop to now + 60 seconds.
        """
        now = datetime.utcnow()
        compressed_stop_at = now + timedelta(seconds=_REGIME_FLIP_HOLD_COMPRESS_SEC)

        for position_id, position in list(self._monitors.items()):
            if position_id in self._regime_tightened:
                continue  # Already tightened — don't tighten further

            original_risk = abs(position.entry_price - position.stop_price)
            tightened_risk = original_risk * _REGIME_FLIP_STOP_FRACTION

            if position.direction == Direction.LONG:
                new_stop = position.entry_price - tightened_risk
                # Only move stop upward (never widen)
                if new_stop <= position.stop_price:
                    new_stop = position.stop_price
            else:
                new_stop = position.entry_price + tightened_risk
                if new_stop >= position.stop_price:
                    new_stop = position.stop_price

            position.stop_price = new_stop
            position.time_stop_at = min(position.time_stop_at, compressed_stop_at)
            position.updated_at = now

            self._regime_tightened.add(position_id)

            logger.info(
                "[%s] REGIME_FLIP tightened position_id=%s symbol=%s "
                "new_stop=%.4f new_time_stop=%s regime=%s",
                self.agent_name, position_id, position.symbol,
                new_stop, position.time_stop_at.isoformat(),
                self._current_regime.value,
            )

            await self.store.upsert_position(position)
            await self.publish(
                topic=Topic.POSITION_UPDATE,
                payload={
                    **position.model_dump(mode="json"),
                    "regime_tightened": True,
                    "new_regime": self._current_regime.value,
                },
            )
            await self.audit.record(AuditEvent(
                event_type="REGIME_FLIP_STOP_TIGHTENED",
                agent=self.agent_id,
                symbol=position.symbol,
                details={
                    "position_id": position_id,
                    "new_stop": new_stop,
                    "original_risk": original_risk,
                    "tightened_risk": tightened_risk,
                    "new_regime": self._current_regime.value,
                    "new_time_stop_at": position.time_stop_at.isoformat(),
                },
            ))

    # ── Exit trigger ───────────────────────────────────────────────────────────

    async def _trigger_exit(
        self,
        position: Position,
        exit_mode: ExitMode,
        reason: str,
    ) -> None:
        position_id = position.position_id
        symbol = position.symbol

        # Remove from monitor set immediately to prevent double-trigger
        self._monitors.pop(position_id, None)

        current_price = position.current_price

        logger.info(
            "[%s] EXIT_TRIGGERED: position_id=%s symbol=%s mode=%s "
            "price=%.4f reason=%s",
            self.agent_name, position_id, symbol,
            exit_mode.value, current_price, reason,
        )

        # Publish EXIT_TRIGGERED for Agent 12 / EA to act on
        await self.publish(
            topic=Topic.EXIT_TRIGGERED,
            payload={
                "position_id": position_id,
                "symbol": symbol,
                "exit_mode": exit_mode.value,
                "current_price": current_price,
                "reason": reason,
                "direction": position.direction.value,
                "shares": position.shares,
                "entry_price": position.entry_price,
                "stop_price": position.stop_price,
                "target_price": position.target_price,
                "trailing_stop": position.trailing_stop,
                "adverse_excursion": self._adverse_excursion.pop(position_id, 0.0),
                "triggered_at": datetime.utcnow().isoformat(),
            },
        )

        # Immutable audit record
        await self.audit.record(AuditEvent(
            event_type="EXIT_TRIGGERED",
            agent=self.agent_id,
            symbol=symbol,
            details={
                "position_id": position_id,
                "exit_mode": exit_mode.value,
                "current_price": current_price,
                "reason": reason,
            },
            immutable=True,
        ))

        # Clean up auxiliary state
        self._entry_spreads.pop(position_id, None)
        self._position_order_map.pop(position_id, None)
        self._regime_tightened.discard(position_id)
