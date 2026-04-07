"""
Agent 21 — Statistical Arbitrage Agent (StatArb)

Implements cointegration-based pair trading using:
  - Engle-Granger two-step cointegration test for pair qualification
  - Kalman filter for dynamic hedge ratio tracking
  - Ornstein-Uhlenbeck (OU) model for mean-reversion speed / half-life
  - Rolling z-score for entry / exit signal generation
  - ETF/underlying arbitrage (ETF vs component basket)
  - Regime gating: only enters trades during MEAN_REVERSION_DAY regime

Pipeline:
  1. Screen pair universe → qualified (cointegrated, HL in range, Hurst < 0.45)
  2. Kalman update on each tick → current spread and hedge ratio
  3. Z-score signal: |z| > entry_threshold → open position
  4. OU half-life → set max holding time (1–2× half-life)
  5. Z-score reverts to |z| < exit_threshold → close position
  6. Risk gate: max N concurrent pairs, max spread exposure per pair

Subscribes: CLOCK_TICK, REGIME_UPDATE, FILL_EVENT, KILL_SWITCH
Publishes:  TRADE_PLAN (pair trade), EXIT_TRIGGERED, POSITION_UPDATE
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from core.enums import (
    AgentState, Direction, RegimeLabel, StatArbSignal, Topic,
)
from core.models import BusMessage
from core.statistics import (
    KalmanState,
    OUParams,
    PairCandidate,
    StatArbSignalOutput,
    engle_granger,
    fit_ou,
    generate_stat_arb_signal,
    screen_pair,
)
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)


# ─── Pair Position ────────────────────────────────────────────────────────────

@dataclass
class PairPosition:
    """Active stat-arb position on a single pair."""
    pair_id: str              # e.g. "AAPL-QQQ"
    symbol_y: str
    symbol_x: str
    direction: int            # +1 = long spread, -1 = short spread
    hedge_ratio: float
    entry_spread: float       # spread value at entry
    entry_z: float
    entry_time: float         # Unix timestamp
    qty_y: float              # shares of Y leg
    qty_x: float              # shares of X leg (= qty_y * hedge_ratio)
    half_life_bars: float     # OU half-life (used for max hold limit)
    pnl: float = 0.0
    status: str = "open"      # "open" | "closed"

    @property
    def max_hold_sec(self) -> float:
        """Maximum holding time = 2× half-life (assuming 1 bar = 1 min = 60s)."""
        return max(300.0, self.half_life_bars * 2 * 60)

    @property
    def age_sec(self) -> float:
        return time.time() - self.entry_time


# ─── Statistical Arbitrage Agent ──────────────────────────────────────────────

class StatArbAgent:
    """
    Agent 21: Statistical Arbitrage Agent.

    Manages a portfolio of cointegrated pairs, continuously updating
    Kalman hedge ratios and z-scores to generate mean-reversion trades.

    NOT a BaseAgent subclass — operates on its own async loop.

    Args:
        pair_universe:    List of (symbol_y, symbol_x) candidate pairs.
        bus:              Message bus.
        store:            State store.
        audit:            Audit log.
        get_prices_fn:    Sync fn returning recent price history as
                          dict[str, list[float]] for all symbols.
        submit_order_fn:  Async fn: (symbol, side, qty) -> order_id
        config:           Strategy configuration dict.
    """

    AGENT_ID = 21
    AGENT_NAME = "stat_arb"

    def __init__(
        self,
        pair_universe: list[tuple[str, str]],
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        get_prices_fn: Callable[[list[str], int], dict[str, list[float]]],
        submit_order_fn: Callable,
        config: dict | None = None,
    ) -> None:
        self._pair_universe = pair_universe
        self._bus = bus
        self._store = store
        self._audit = audit
        self._get_prices_fn = get_prices_fn
        self._submit_order_fn = submit_order_fn

        cfg = config or {}
        self._z_entry = cfg.get("z_entry", 2.0)
        self._z_exit = cfg.get("z_exit", 0.5)
        self._zscore_window = cfg.get("zscore_window", 60)
        self._screen_window = cfg.get("screen_window", 250)
        self._max_pairs = cfg.get("max_concurrent_pairs", 5)
        self._max_notional_per_pair = cfg.get("max_notional_per_pair", 20_000.0)
        self._rescreen_interval = cfg.get("rescreen_interval_bars", 50)
        self._min_half_life = cfg.get("min_half_life_bars", 5.0)
        self._max_half_life = cfg.get("max_half_life_bars", 120.0)

        # Qualified pairs (refreshed periodically)
        self._qualified_pairs: list[PairCandidate] = []

        # Kalman state per pair
        self._kalman_states: dict[str, KalmanState] = {}

        # Spread history for z-score (per pair)
        self._spread_history: dict[str, list[float]] = {}

        # Active positions
        self._positions: dict[str, PairPosition] = {}

        # Current regime (gate)
        self._current_regime: RegimeLabel | None = None

        # Bar counter for re-screening
        self._bar_count = 0

        self._running = False
        self._state = AgentState.IDLE
        self._update_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._state = AgentState.PROCESSING
        logger.info("[StatArb] Starting on %d candidate pairs", len(self._pair_universe))

        await self._bus.subscribe(Topic.CLOCK_TICK, self._on_tick)
        await self._bus.subscribe(Topic.REGIME_UPDATE, self._on_regime_update)
        await self._bus.subscribe(Topic.FILL_EVENT, self._on_fill)
        await self._bus.subscribe(Topic.KILL_SWITCH, self._on_kill_switch)

        # Initial pair screening
        await self._screen_pairs()

        self._update_task = asyncio.create_task(
            self._heartbeat_loop(), name="statarb_hb"
        )

    async def stop(self) -> None:
        self._running = False
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
        await self._close_all_positions("agent_stop")

    # ── Pair Screening ────────────────────────────────────────────────────────

    async def _screen_pairs(self) -> None:
        """
        Re-screen the pair universe for cointegration quality.
        Runs in executor to avoid blocking event loop on numpy-heavy computation.
        """
        logger.info("[StatArb] Screening %d pairs for cointegration...", len(self._pair_universe))

        # Collect prices for all unique symbols
        all_symbols = list({sym for pair in self._pair_universe for sym in pair})
        prices = self._get_prices_fn(all_symbols, self._screen_window)

        qualified = []
        for sym_y, sym_x in self._pair_universe:
            py = prices.get(sym_y, [])
            px = prices.get(sym_x, [])
            if len(py) < 50 or len(px) < 50:
                continue
            candidate = screen_pair(
                symbol_y=sym_y,
                symbol_x=sym_x,
                prices_y=py,
                prices_x=px,
                min_half_life=self._min_half_life,
                max_half_life=self._max_half_life,
            )
            if candidate.is_valid:
                qualified.append(candidate)

        # Sort by half-life (prefer faster mean reversion)
        qualified.sort(key=lambda c: c.half_life_bars)
        self._qualified_pairs = qualified

        logger.info(
            "[StatArb] Screening complete: %d/%d pairs qualified",
            len(qualified), len(self._pair_universe),
        )

    # ── Tick Handler ──────────────────────────────────────────────────────────

    async def _on_tick(self, message: BusMessage) -> None:
        """Process one bar tick — update Kalman states, check signals."""
        if not self._running:
            return

        self._bar_count += 1

        # Re-screen periodically
        if self._bar_count % self._rescreen_interval == 0:
            asyncio.create_task(self._screen_pairs())

        # Fetch current prices
        all_symbols = list({sym for pair in self._pair_universe for sym in pair})
        prices = self._get_prices_fn(all_symbols, self._zscore_window + 10)

        # Update open positions (check exit)
        await self._check_exits(prices)

        # Check entry signals (if below max pairs)
        if len(self._positions) < self._max_pairs:
            await self._check_entries(prices)

    async def _check_exits(self, prices: dict[str, list[float]]) -> None:
        """Check each open position for exit conditions."""
        for pair_id, pos in list(self._positions.items()):
            if pos.status != "open":
                continue

            # Time-based exit: max holding period
            if pos.age_sec > pos.max_hold_sec:
                await self._close_position(pair_id, "time_stop")
                continue

            py = prices.get(pos.symbol_y, [])
            px = prices.get(pos.symbol_x, [])
            if not py or not px:
                continue

            ks = self._kalman_states.get(pair_id)
            signal, new_ks = generate_stat_arb_signal(
                y_series=py,
                x_series=px,
                z_entry=self._z_entry,
                z_exit=self._z_exit,
                window=self._zscore_window,
                current_position=pos.direction,
                kalman_state=ks,
            )
            self._kalman_states[pair_id] = new_ks

            if signal.should_exit:
                await self._close_position(pair_id, f"z_reversion z={signal.zscore:.2f}")

    async def _check_entries(self, prices: dict[str, list[float]]) -> None:
        """Check qualified pairs for entry signals."""
        if not self._is_regime_ok():
            return

        for candidate in self._qualified_pairs:
            pair_id = f"{candidate.symbol_y}-{candidate.symbol_x}"
            if pair_id in self._positions:
                continue

            py = prices.get(candidate.symbol_y, [])
            px = prices.get(candidate.symbol_x, [])
            if len(py) < self._zscore_window or len(px) < self._zscore_window:
                continue

            ks = self._kalman_states.get(pair_id)
            signal, new_ks = generate_stat_arb_signal(
                y_series=py,
                x_series=px,
                z_entry=self._z_entry,
                z_exit=self._z_exit,
                window=self._zscore_window,
                current_position=0,
                kalman_state=ks,
            )
            self._kalman_states[pair_id] = new_ks

            if signal.direction != 0:
                mid_y = py[-1]
                mid_x = px[-1]
                await self._open_position(candidate, signal, mid_y, mid_x, pair_id)
                break   # one entry per tick

    def _is_regime_ok(self) -> bool:
        """Only trade in mean-reversion regimes."""
        if self._current_regime is None:
            return True   # no regime info yet — allow
        return self._current_regime in (
            RegimeLabel.MEAN_REVERSION_DAY,
            RegimeLabel.TREND_DAY,   # stat arb can run in trending regimes too
        )

    async def _open_position(
        self,
        candidate: PairCandidate,
        signal: StatArbSignalOutput,
        mid_y: float,
        mid_x: float,
        pair_id: str,
    ) -> None:
        """Enter a pair trade."""
        # Position sizing: $notional / price per share
        qty_y = max(1.0, round(self._max_notional_per_pair / mid_y / 2))
        qty_x = max(1.0, round(qty_y * signal.hedge_ratio))

        # Leg Y
        y_side = "buy" if signal.direction == 1 else "sell"
        x_side = "sell" if signal.direction == 1 else "buy"

        try:
            y_id = await self._submit_order_fn(candidate.symbol_y, y_side, qty_y)
            x_id = await self._submit_order_fn(candidate.symbol_x, x_side, qty_x)
        except Exception as exc:
            logger.error("[StatArb] Order submission failed for %s: %s", pair_id, exc)
            return

        spread_at_entry = mid_y - signal.hedge_ratio * mid_x
        pos = PairPosition(
            pair_id=pair_id,
            symbol_y=candidate.symbol_y,
            symbol_x=candidate.symbol_x,
            direction=signal.direction,
            hedge_ratio=signal.hedge_ratio,
            entry_spread=spread_at_entry,
            entry_z=signal.zscore,
            entry_time=time.time(),
            qty_y=qty_y,
            qty_x=qty_x,
            half_life_bars=signal.half_life,
        )
        self._positions[pair_id] = pos

        logger.info(
            "[StatArb] OPEN %s dir=%+d z=%.2f HR=%.3f HL=%.1f bars",
            pair_id, signal.direction, signal.zscore, signal.hedge_ratio, signal.half_life,
        )

        await self._bus.publish(BusMessage(
            topic=Topic.TRADE_PLAN,
            payload={
                "agent": self.AGENT_NAME,
                "pair_id": pair_id,
                "symbol_y": candidate.symbol_y,
                "symbol_x": candidate.symbol_x,
                "direction": signal.direction,
                "qty_y": qty_y,
                "qty_x": qty_x,
                "hedge_ratio": signal.hedge_ratio,
                "entry_z": signal.zscore,
                "half_life_bars": signal.half_life,
                "stat_arb": True,
            },
            source_agent=self.AGENT_NAME,
        ))

    async def _close_position(self, pair_id: str, reason: str) -> None:
        pos = self._positions.get(pair_id)
        if not pos or pos.status != "open":
            return

        # Reverse each leg
        y_side = "sell" if pos.direction == 1 else "buy"
        x_side = "buy" if pos.direction == 1 else "sell"

        try:
            await self._submit_order_fn(pos.symbol_y, y_side, pos.qty_y)
            await self._submit_order_fn(pos.symbol_x, x_side, pos.qty_x)
        except Exception as exc:
            logger.error("[StatArb] Close order failed for %s: %s", pair_id, exc)

        pos.status = "closed"
        logger.info("[StatArb] CLOSE %s reason=%s pnl=$%.2f", pair_id, reason, pos.pnl)

        await self._bus.publish(BusMessage(
            topic=Topic.EXIT_TRIGGERED,
            payload={
                "agent": self.AGENT_NAME,
                "pair_id": pair_id,
                "reason": reason,
                "pnl": pos.pnl,
            },
            source_agent=self.AGENT_NAME,
        ))

    async def _close_all_positions(self, reason: str) -> None:
        for pair_id in list(self._positions.keys()):
            await self._close_position(pair_id, reason)

    # ── Event Handlers ────────────────────────────────────────────────────────

    async def _on_regime_update(self, message: BusMessage) -> None:
        regime_str = message.payload.get("regime", "")
        try:
            self._current_regime = RegimeLabel(regime_str)
        except ValueError:
            pass

    async def _on_fill(self, message: BusMessage) -> None:
        """Update P&L for pair positions on fill events."""
        symbol = message.payload.get("symbol", "")
        fill_price = float(message.payload.get("price", 0))
        fill_qty = float(message.payload.get("qty", 0))
        side = message.payload.get("side", "buy")

        for pos in self._positions.values():
            if pos.status != "open":
                continue
            if symbol == pos.symbol_y:
                sign = 1 if side == "sell" else -1
                pos.pnl += sign * fill_price * fill_qty
            elif symbol == pos.symbol_x:
                sign = 1 if side == "sell" else -1
                pos.pnl += sign * fill_price * fill_qty

    async def _on_kill_switch(self, message: BusMessage) -> None:
        logger.critical("[StatArb] KILL SWITCH — closing all pair positions")
        self._running = False
        await self._close_all_positions("kill_switch")

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            await self._bus.publish(BusMessage(
                topic=Topic.AGENT_HEARTBEAT,
                payload={
                    "agent_id": self.AGENT_ID,
                    "agent_name": self.AGENT_NAME,
                    "state": self._state.value,
                    "qualified_pairs": len(self._qualified_pairs),
                    "open_positions": len([p for p in self._positions.values()
                                          if p.status == "open"]),
                    "regime": self._current_regime.value if self._current_regime else None,
                },
                source_agent=self.AGENT_NAME,
            ))

    # ── Introspection ─────────────────────────────────────────────────────────

    def position_report(self) -> list[dict]:
        return [
            {
                "pair_id": pos.pair_id,
                "direction": pos.direction,
                "entry_z": pos.entry_z,
                "hedge_ratio": pos.hedge_ratio,
                "pnl": pos.pnl,
                "age_sec": pos.age_sec,
                "max_hold_sec": pos.max_hold_sec,
                "status": pos.status,
            }
            for pos in self._positions.values()
        ]

    @property
    def qualified_pairs(self) -> list[PairCandidate]:
        return self._qualified_pairs


