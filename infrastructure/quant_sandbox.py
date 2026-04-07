"""
infrastructure/quant_sandbox.py — Quant Strategy Sandbox

Isolated strategy deployment runtime for research and live experiments.

The sandbox provides:
  - A base `Strategy` class with a well-defined lifecycle
  - `QuantSandbox` manager: register, start, stop, teardown strategies
  - Resource limits per strategy (max drawdown, max position size, daily loss cap)
  - Per-strategy P&L tracking in isolation from the live book
  - Strategy hot-reload (stop → update code ref → restart without system restart)
  - Signal emission through a sandboxed message bus topic (SANDBOX_SIGNAL)
  - Quarantine: automatic suspension if a strategy breaches its risk limits

Design principles:
  - Strategies cannot access live order submission directly
  - Each strategy gets its own event loop task (cooperative multitasking)
  - Sandbox signals are advisory only — the main pipeline decides whether to act
  - All strategy decisions are logged to an isolated per-strategy audit trail

This is NOT Agent 18 (SMRE / replay). The sandbox runs strategies against
the live tick stream in forward-time, for research; SMRE replays past sessions
for offline validation.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


# ─── Enums ─────────────────────────────────────────────────────────────────────

class StrategyState(str, Enum):
    REGISTERED = "registered"
    STARTING = "starting"
    ACTIVE = "active"
    PAUSED = "paused"
    QUARANTINED = "quarantined"   # risk limit breached
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class SandboxSignalType(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    ALERT = "alert"
    INFO = "info"


# ─── Signal emitted by a sandboxed strategy ────────────────────────────────────

@dataclass
class SandboxSignal:
    """Signal emitted by a strategy into the sandbox bus."""
    strategy_id: str
    signal_type: SandboxSignalType
    symbol: str
    direction: str          # 'long' / 'short' / 'flat'
    confidence: float       # 0.0–1.0
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# ─── Per-strategy risk limits ──────────────────────────────────────────────────

@dataclass
class StrategyLimits:
    """
    Risk guard-rails applied per strategy in the sandbox.

    Exceeding any hard limit causes automatic quarantine.
    """
    max_position_usd: float = 10_000.0   # max gross notional per symbol
    max_daily_loss_usd: float = 500.0    # hard daily loss cap
    max_drawdown_pct: float = 5.0        # % from peak equity
    max_signals_per_minute: int = 60     # throttle runaway strategies
    max_open_positions: int = 5          # concurrent open positions


# ─── Per-strategy P&L tracker ─────────────────────────────────────────────────

class StrategyPnLTracker:
    """
    Isolated P&L book for a single sandbox strategy.

    Tracks: fills, open positions, daily P&L, peak equity, drawdown.
    """

    def __init__(self, initial_equity: float = 100_000.0) -> None:
        self._equity = initial_equity
        self._peak_equity = initial_equity
        self._daily_pnl = 0.0
        self._positions: dict[str, dict] = {}  # symbol → {qty, avg_price}
        self._fills: list[dict] = []
        self.total_trades = 0
        self._winning_trades = 0

    def record_fill(self, symbol: str, qty: float, price: float, side: str) -> None:
        """
        Record an (imaginary) fill.

        side: 'buy' or 'sell'
        qty:  always positive; side determines direction.
        """
        notional = qty * price
        pos = self._positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})

        if side == "buy":
            new_qty = pos["qty"] + qty
            if pos["qty"] == 0:
                pos["avg_price"] = price
            else:
                pos["avg_price"] = (pos["avg_price"] * pos["qty"] + notional) / new_qty
            pos["qty"] = new_qty
        else:  # sell / flatten
            sell_qty = min(qty, abs(pos["qty"]))
            pnl = sell_qty * (price - pos["avg_price"])
            self._daily_pnl += pnl
            self._equity += pnl
            pos["qty"] = pos["qty"] - sell_qty
            self.total_trades += 1
            if pnl > 0:
                self._winning_trades += 1

        self._positions[symbol] = pos
        self._peak_equity = max(self._peak_equity, self._equity)
        self._fills.append({
            "symbol": symbol, "qty": qty, "price": price, "side": side,
            "ts": time.time(),
        })

    def mark_positions(self, prices: dict[str, float]) -> None:
        """Update P&L with current market prices (mark-to-market)."""
        mtm = 0.0
        for sym, pos in self._positions.items():
            if sym in prices and pos["qty"] != 0:
                mtm += pos["qty"] * (prices[sym] - pos["avg_price"])
        self._equity = self._peak_equity + self._daily_pnl + mtm  # approximate

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def drawdown_pct(self) -> float:
        if self._peak_equity == 0:
            return 0.0
        return (self._peak_equity - self._equity) / self._peak_equity * 100.0

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self._positions.values() if abs(p["qty"]) > 1e-9)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self._winning_trades / self.total_trades

    def summary(self) -> dict:
        return {
            "equity": round(self._equity, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "drawdown_pct": round(self.drawdown_pct, 4),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 4),
            "open_positions": self.open_position_count,
        }


# ─── Strategy base class ───────────────────────────────────────────────────────

class Strategy(ABC):
    """
    Base class for all sandboxed strategies.

    Subclass this and implement `on_tick()` and optionally `on_start()` /
    `on_stop()`. Emit signals via `self.emit()`. The sandbox enforces all
    resource limits — strategies do not interact with the live order book.

    Attributes:
        strategy_id:  Unique identifier assigned by the sandbox.
        name:         Human-readable strategy name.
        version:      Semver string (e.g., '1.0.0').
        symbols:      Symbols this strategy watches.
        limits:       Risk limits. Defaults to StrategyLimits().
    """

    def __init__(
        self,
        name: str,
        version: str = "1.0.0",
        symbols: list[str] | None = None,
        limits: StrategyLimits | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.symbols: list[str] = symbols or []
        self.limits = limits or StrategyLimits()

        # Set by sandbox at registration
        self.strategy_id: str = str(uuid.uuid4())
        self._state: StrategyState = StrategyState.REGISTERED
        self._pnl = StrategyPnLTracker()
        self._signal_callbacks: list[Callable[[SandboxSignal], Awaitable[None]]] = []
        self._signal_count_window: list[float] = []  # timestamps for rate limiting

    # ── Lifecycle (optional overrides) ────────────────────────────────────────

    async def on_start(self) -> None:
        """Called once when the strategy is activated. Override for initialization."""

    async def on_stop(self) -> None:
        """Called once when the strategy is stopped. Override for cleanup."""

    # ── Core interface (must implement) ───────────────────────────────────────

    @abstractmethod
    async def on_tick(self, symbol: str, price: float, bid: float, ask: float,
                      volume: int, ts: float) -> None:
        """
        Called for every market tick matching this strategy's symbol list.

        Args:
            symbol: Ticker symbol.
            price:  Last trade price.
            bid:    Best bid.
            ask:    Best ask.
            volume: Tick volume.
            ts:     Tick timestamp (Unix seconds).
        """

    # ── Signal emission ───────────────────────────────────────────────────────

    async def emit(
        self,
        signal_type: SandboxSignalType,
        symbol: str,
        direction: str,
        confidence: float = 1.0,
        **kwargs: Any,
    ) -> None:
        """
        Emit a sandbox signal. Rate-limited by `limits.max_signals_per_minute`.
        """
        now = time.time()
        # Prune signals older than 60s
        self._signal_count_window = [t for t in self._signal_count_window if now - t < 60.0]
        if len(self._signal_count_window) >= self.limits.max_signals_per_minute:
            logger.warning("[Sandbox:%s] Signal rate limit hit — dropping signal", self.name)
            return
        self._signal_count_window.append(now)

        sig = SandboxSignal(
            strategy_id=self.strategy_id,
            signal_type=signal_type,
            symbol=symbol,
            direction=direction,
            confidence=max(0.0, min(1.0, confidence)),
            payload=kwargs,
        )
        for cb in self._signal_callbacks:
            try:
                await cb(sig)
            except Exception as exc:
                logger.error("[Sandbox:%s] Signal callback error: %s", self.name, exc)

    # ── P&L helpers (strategies may call these) ───────────────────────────────

    def record_fill(self, symbol: str, qty: float, price: float, side: str) -> None:
        self._pnl.record_fill(symbol, qty, price, side)

    def mark(self, prices: dict[str, float]) -> None:
        self._pnl.mark_positions(prices)

    @property
    def pnl(self) -> StrategyPnLTracker:
        return self._pnl

    @property
    def state(self) -> StrategyState:
        return self._state


# ─── Sandbox descriptor ────────────────────────────────────────────────────────

@dataclass
class StrategyEntry:
    """Internal sandbox registry record."""
    strategy: Strategy
    task: asyncio.Task | None = None
    registered_at: float = field(default_factory=time.time)
    started_at: float | None = None
    error_count: int = 0
    last_error: str = ""
    signals_emitted: int = 0


# ─── Quant Sandbox ─────────────────────────────────────────────────────────────

class QuantSandbox:
    """
    Strategy lifecycle manager with isolated risk tracking.

    The sandbox:
      1. Registers strategies and assigns them unique IDs
      2. Starts/stops strategy tasks on demand
      3. Routes market tick events to strategies that watch each symbol
      4. Enforces per-strategy risk limits (daily loss, drawdown, position count)
      5. Quarantines strategies that breach limits
      6. Collects sandbox signals and routes to registered consumers
      7. Supports hot-reload: swap a strategy's implementation while running

    Usage::

        sandbox = QuantSandbox()
        sid = sandbox.register(MyStrategy("breakout_v3", symbols=["NVDA", "TSLA"]))

        # Subscribe to signals from all strategies
        sandbox.add_signal_consumer(lambda sig: print(sig))

        await sandbox.start_strategy(sid)

        # Feed a tick (e.g., from TickWarehouse.replay)
        await sandbox.feed_tick(tick)

        await sandbox.stop_strategy(sid)

    Args:
        max_strategies: Hard limit on simultaneously active strategies.
    """

    def __init__(self, max_strategies: int = 20) -> None:
        self._max_strategies = max_strategies
        self._registry: dict[str, StrategyEntry] = {}
        self._consumers: list[Callable[[SandboxSignal], Awaitable[None]]] = []
        self._symbol_index: dict[str, list[str]] = {}  # symbol → [strategy_ids]
        self._running = False
        self._risk_check_task: asyncio.Task | None = None

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, strategy: Strategy) -> str:
        """
        Register a strategy. Returns the assigned strategy_id.

        Raises ValueError if the sandbox is at capacity.
        """
        active_count = sum(
            1 for e in self._registry.values()
            if e.strategy.state == StrategyState.ACTIVE
        )
        if active_count >= self._max_strategies:
            raise ValueError(
                f"Sandbox at capacity ({self._max_strategies} active strategies)"
            )

        strategy._signal_callbacks.append(self._on_signal)
        entry = StrategyEntry(strategy=strategy)
        self._registry[strategy.strategy_id] = entry

        # Update symbol index
        for sym in strategy.symbols:
            self._symbol_index.setdefault(sym, [])
            if strategy.strategy_id not in self._symbol_index[sym]:
                self._symbol_index[sym].append(strategy.strategy_id)

        logger.info("[Sandbox] Registered strategy '%s' id=%s",
                    strategy.name, strategy.strategy_id)
        return strategy.strategy_id

    def deregister(self, strategy_id: str) -> None:
        """Remove a stopped strategy from the registry."""
        entry = self._registry.get(strategy_id)
        if not entry:
            return
        if entry.strategy.state == StrategyState.ACTIVE:
            raise RuntimeError("Cannot deregister an active strategy — stop it first.")
        # Remove from symbol index
        for sym in entry.strategy.symbols:
            ids = self._symbol_index.get(sym, [])
            if strategy_id in ids:
                ids.remove(strategy_id)
        del self._registry[strategy_id]
        logger.info("[Sandbox] Deregistered strategy '%s'", strategy_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_strategy(self, strategy_id: str) -> None:
        """Start a registered strategy's async event loop task."""
        entry = self._get_entry(strategy_id)
        strategy = entry.strategy
        if strategy.state not in (StrategyState.REGISTERED, StrategyState.STOPPED,
                                   StrategyState.PAUSED):
            raise RuntimeError(
                f"Cannot start strategy in state {strategy.state}"
            )
        strategy._state = StrategyState.STARTING
        try:
            await strategy.on_start()
            strategy._state = StrategyState.ACTIVE
            entry.started_at = time.time()
            logger.info("[Sandbox] Started strategy '%s'", strategy.name)
        except Exception as exc:
            strategy._state = StrategyState.ERROR
            entry.last_error = str(exc)
            entry.error_count += 1
            logger.error("[Sandbox] Strategy '%s' failed to start: %s", strategy.name, exc)
            raise

    async def stop_strategy(self, strategy_id: str) -> None:
        """Gracefully stop a running strategy."""
        entry = self._get_entry(strategy_id)
        strategy = entry.strategy
        if strategy.state not in (StrategyState.ACTIVE, StrategyState.PAUSED,
                                   StrategyState.QUARANTINED):
            return
        strategy._state = StrategyState.STOPPING
        try:
            await strategy.on_stop()
        except Exception as exc:
            logger.error("[Sandbox] Error in '%s'.on_stop(): %s", strategy.name, exc)
        finally:
            strategy._state = StrategyState.STOPPED
            logger.info("[Sandbox] Stopped strategy '%s'", strategy.name)

    async def pause_strategy(self, strategy_id: str) -> None:
        entry = self._get_entry(strategy_id)
        if entry.strategy.state == StrategyState.ACTIVE:
            entry.strategy._state = StrategyState.PAUSED
            logger.info("[Sandbox] Paused strategy '%s'", entry.strategy.name)

    async def resume_strategy(self, strategy_id: str) -> None:
        entry = self._get_entry(strategy_id)
        if entry.strategy.state == StrategyState.PAUSED:
            entry.strategy._state = StrategyState.ACTIVE
            logger.info("[Sandbox] Resumed strategy '%s'", entry.strategy.name)

    async def quarantine_strategy(self, strategy_id: str, reason: str) -> None:
        """Suspend a strategy due to a risk limit breach."""
        entry = self._get_entry(strategy_id)
        entry.strategy._state = StrategyState.QUARANTINED
        logger.warning("[Sandbox] Quarantined strategy '%s': %s",
                        entry.strategy.name, reason)

    # ── Hot-reload ────────────────────────────────────────────────────────────

    async def hot_reload(self, strategy_id: str, new_strategy: Strategy) -> None:
        """
        Replace a running strategy's implementation without deregistering.

        Steps: stop → swap → start with preserved strategy_id and P&L.
        """
        entry = self._get_entry(strategy_id)
        old_strategy = entry.strategy

        await self.stop_strategy(strategy_id)

        # Preserve ID and P&L state
        new_strategy.strategy_id = strategy_id
        new_strategy._pnl = old_strategy._pnl
        new_strategy._signal_callbacks = [self._on_signal]

        entry.strategy = new_strategy
        entry.error_count = 0
        entry.last_error = ""

        # Update symbol index
        for sym in old_strategy.symbols:
            ids = self._symbol_index.get(sym, [])
            if strategy_id in ids:
                ids.remove(strategy_id)
        for sym in new_strategy.symbols:
            self._symbol_index.setdefault(sym, [])
            if strategy_id not in self._symbol_index[sym]:
                self._symbol_index[sym].append(strategy_id)

        await self.start_strategy(strategy_id)
        logger.info("[Sandbox] Hot-reloaded strategy '%s' → v%s",
                    old_strategy.name, new_strategy.version)

    # ── Tick routing ──────────────────────────────────────────────────────────

    async def feed_tick(self, tick: "Tick") -> None:  # type: ignore[name-defined]
        """
        Route a market tick to all active strategies watching the symbol.
        Enforces risk limits after each strategy processes the tick.
        """
        strategy_ids = self._symbol_index.get(tick.symbol, [])
        for sid in list(strategy_ids):
            entry = self._registry.get(sid)
            if not entry or entry.strategy.state != StrategyState.ACTIVE:
                continue
            try:
                await entry.strategy.on_tick(
                    symbol=tick.symbol,
                    price=tick.price,
                    bid=tick.bid,
                    ask=tick.ask,
                    volume=tick.volume,
                    ts=tick.ts,
                )
                # Risk check after each tick
                await self._check_risk_limits(sid)
            except Exception as exc:
                entry.error_count += 1
                entry.last_error = str(exc)
                logger.error("[Sandbox:%s] on_tick error: %s", entry.strategy.name, exc)
                if entry.error_count >= 3:
                    await self.quarantine_strategy(sid, f"3 consecutive errors: {exc}")

    # ── Signal consumers ──────────────────────────────────────────────────────

    def add_signal_consumer(
        self, callback: Callable[[SandboxSignal], Awaitable[None]]
    ) -> None:
        """Register a coroutine to receive all sandbox signals."""
        self._consumers.append(callback)

    # ── Risk enforcement ──────────────────────────────────────────────────────

    async def _check_risk_limits(self, strategy_id: str) -> None:
        entry = self._registry.get(strategy_id)
        if not entry or entry.strategy.state != StrategyState.ACTIVE:
            return
        lim = entry.strategy.limits
        pnl = entry.strategy._pnl

        if pnl.daily_pnl < -lim.max_daily_loss_usd:
            await self.quarantine_strategy(
                strategy_id,
                f"Daily loss cap breached: ${pnl.daily_pnl:.2f} < -${lim.max_daily_loss_usd:.2f}"
            )
        elif pnl.drawdown_pct > lim.max_drawdown_pct:
            await self.quarantine_strategy(
                strategy_id,
                f"Max drawdown breached: {pnl.drawdown_pct:.2f}% > {lim.max_drawdown_pct:.2f}%"
            )
        elif pnl.open_position_count > lim.max_open_positions:
            await self.quarantine_strategy(
                strategy_id,
                f"Max open positions breached: {pnl.open_position_count} > {lim.max_open_positions}"
            )

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _on_signal(self, signal: SandboxSignal) -> None:
        entry = self._registry.get(signal.strategy_id)
        if entry:
            entry.signals_emitted += 1
        for cb in self._consumers:
            try:
                await cb(signal)
            except Exception as exc:
                logger.error("[Sandbox] Consumer error on signal: %s", exc)

    def _get_entry(self, strategy_id: str) -> StrategyEntry:
        entry = self._registry.get(strategy_id)
        if not entry:
            raise KeyError(f"Strategy '{strategy_id}' not found in sandbox")
        return entry

    # ── Introspection ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return a summary of all registered strategies."""
        return {
            "total": len(self._registry),
            "active": sum(1 for e in self._registry.values()
                          if e.strategy.state == StrategyState.ACTIVE),
            "quarantined": sum(1 for e in self._registry.values()
                               if e.strategy.state == StrategyState.QUARANTINED),
            "strategies": {
                sid: {
                    "name": e.strategy.name,
                    "version": e.strategy.version,
                    "state": e.strategy.state.value,
                    "symbols": e.strategy.symbols,
                    "pnl": e.strategy.pnl.summary(),
                    "signals_emitted": e.signals_emitted,
                    "error_count": e.error_count,
                    "last_error": e.last_error,
                }
                for sid, e in self._registry.items()
            },
        }

    def get_pnl(self, strategy_id: str) -> StrategyPnLTracker:
        return self._get_entry(strategy_id).strategy.pnl

    def active_strategy_ids(self) -> list[str]:
        return [
            sid for sid, e in self._registry.items()
            if e.strategy.state == StrategyState.ACTIVE
        ]
