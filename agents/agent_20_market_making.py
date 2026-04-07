"""
Agent 20 — Institutional Market Making Agent (MMA-MM)

Implements optimal two-sided liquidity provision using the
Avellaneda-Stoikov (2008) model for reservation price and spread.

Responsibilities:
  - Maintain continuous bid/ask quotes on a universe of symbols
  - Dynamic spread adjustment based on inventory risk and volatility
  - Hard inventory limits with automatic offload (skew and aggress)
  - Fill probability estimation per venue
  - Inventory risk tracking: 99th-pct VaR per symbol
  - Quote lifecycle management (cancel/replace on each tick)
  - Publishes: ORDER_SUBMITTED (bid/ask pairs), POSITION_UPDATE
  - Subscribes: CLOCK_TICK (re-quote trigger), FILL_EVENT, KILL_SWITCH

SLOs:
  - Quote refresh latency: ≤ 500µs (FPGA path target: ≤ 50µs)
  - Inventory-adjusted spread: within 0.5% of A-S theoretical
  - Max inventory: configurable per symbol (default 1000 shares)

This agent does NOT go through the standard 17-agent pipeline — it
operates independently as a separate liquidity provision loop that
publishes directly to ORDER_SUBMITTED.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable

from core.enums import AgentState, InventoryAction, OrderSide, Topic
from core.microstructure import (
    ASTParameters,
    Level,
    OrderBook,
    inventory_risk,
    inventory_skew_factor,
    optimal_quotes,
    reservation_price,
    optimal_spread,
    order_book_imbalance,
    weighted_mid_price,
)
from core.models import BusMessage
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog
from infrastructure.colocation import LatencyBudget, ColocationProfile, LatencyTier

logger = logging.getLogger(__name__)


# ─── Per-Symbol Market Making State ──────────────────────────────────────────

@dataclass
class MMSymbolState:
    """Running state for one symbol being market-made."""
    symbol: str
    params: ASTParameters

    inventory: float = 0.0              # current signed inventory (shares)
    session_start_ns: int = field(default_factory=time.perf_counter_ns)

    # Current resting quotes
    active_bid_id: str | None = None
    active_ask_id: str | None = None
    active_bid_price: float = 0.0
    active_ask_price: float = 0.0

    # Fill tracking (for P&L)
    total_pnl: float = 0.0
    fills_today: int = 0

    # Inventory action
    action: InventoryAction = InventoryAction.NEUTRAL

    def t_elapsed(self) -> float:
        return (time.perf_counter_ns() - self.session_start_ns) / 1e9

    def inventory_var(self, horizon_sec: float = 60.0) -> float:
        return inventory_risk(self.inventory, self.params.sigma, horizon_sec)


# ─── Market Making Agent ─────────────────────────────────────────────────────

class MarketMakingAgent:
    """
    Agent 20: Institutional Market Making Agent.

    Provides continuous two-sided quotes using Avellaneda-Stoikov optimal
    pricing. Manages inventory across a configurable symbol universe.

    This agent is NOT a BaseAgent subclass — it operates on its own
    async loop and interacts with the bus directly.

    Args:
        symbols:          List of symbols to make markets in.
        bus:              Message bus for order submission and fills.
        store:            State store for persistence.
        audit:            Immutable audit log.
        order_fn:         Async function to submit orders to the broker.
                          Signature: (symbol, side, qty, price) -> order_id
        cancel_fn:        Async function to cancel a resting order by ID.
        get_book_fn:      Sync function returning current OrderBook for symbol.
        get_mid_fn:       Sync function returning current mid price for symbol.
        params_override:  Optional per-symbol A-S parameter overrides.
        profile:          Colocation profile for latency monitoring.
        quote_interval:   Seconds between quote refreshes (default 0.1s = 10Hz).
    """

    AGENT_ID = 20
    AGENT_NAME = "market_making"

    def __init__(
        self,
        symbols: list[str],
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        order_fn: Callable[[str, OrderSide, float, float], Awaitable[str]],
        cancel_fn: Callable[[str], Awaitable[None]],
        get_book_fn: Callable[[str], OrderBook | None],
        get_mid_fn: Callable[[str], float | None],
        params_override: dict[str, ASTParameters] | None = None,
        profile: ColocationProfile | None = None,
        quote_interval: float = 0.1,
        max_inventory: float = 1000.0,
        tick_size: float = 0.01,
    ) -> None:
        self._symbols = symbols
        self._bus = bus
        self._store = store
        self._audit = audit
        self._order_fn = order_fn
        self._cancel_fn = cancel_fn
        self._get_book_fn = get_book_fn
        self._get_mid_fn = get_mid_fn
        self._quote_interval = quote_interval
        self._max_inventory = max_inventory
        self._tick_size = tick_size
        self._profile = profile or ColocationProfile(tier=LatencyTier.INSTITUTIONAL)

        # Per-symbol state
        default_params = ASTParameters()
        self._states: dict[str, MMSymbolState] = {
            sym: MMSymbolState(
                symbol=sym,
                params=(params_override or {}).get(sym, default_params),
            )
            for sym in symbols
        }

        self._running = False
        self._state = AgentState.IDLE
        self._quote_task: asyncio.Task | None = None
        self._fill_task: asyncio.Task | None = None

        # Session P&L
        self._session_pnl: float = 0.0
        self._total_fills: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._state = AgentState.PROCESSING
        logger.info("[MM] Starting market making on %d symbols", len(self._symbols))

        # Subscribe to fills and kill switch
        await self._bus.subscribe(Topic.FILL_EVENT, self._on_fill)
        await self._bus.subscribe(Topic.KILL_SWITCH, self._on_kill_switch)
        await self._bus.subscribe(Topic.SESSION_END, self._on_session_end)

        self._quote_task = asyncio.create_task(self._quote_loop(), name="mm_quotes")
        self._fill_task = asyncio.create_task(self._heartbeat_loop(), name="mm_hb")

    async def stop(self) -> None:
        self._running = False
        for task in (self._quote_task, self._fill_task):
            if task and not task.done():
                task.cancel()
        await self._cancel_all_quotes()
        logger.info("[MM] Market making stopped. Session P&L: $%.2f", self._session_pnl)

    # ── Quote Loop ────────────────────────────────────────────────────────────

    async def _quote_loop(self) -> None:
        while self._running:
            t0 = time.perf_counter_ns()
            for symbol in self._symbols:
                try:
                    await self._refresh_quotes(symbol)
                except Exception as exc:
                    logger.error("[MM] Quote error for %s: %s", symbol, exc)
            elapsed_ms = (time.perf_counter_ns() - t0) / 1e6
            sleep_time = max(0.0, self._quote_interval - elapsed_ms / 1000)
            await asyncio.sleep(sleep_time)

    async def _refresh_quotes(self, symbol: str) -> None:
        """Cancel stale quotes, compute new A-S prices, submit fresh pair."""
        budget = LatencyBudget()
        state = self._states[symbol]

        # Get current market data
        stage = budget.begin_stage("market_data")
        mid = self._get_mid_fn(symbol)
        book = self._get_book_fn(symbol)
        stage.stop()

        if mid is None or mid <= 0:
            return

        # Compute optimal quotes
        stage = budget.begin_stage("compute")
        t_elapsed = state.t_elapsed()
        bid_px, ask_px = optimal_quotes(
            mid=mid,
            inventory=state.inventory,
            t_elapsed=t_elapsed,
            params=state.params,
            tick_size=self._tick_size,
        )

        # Apply inventory skew if near limits
        skew = inventory_skew_factor(state.inventory, self._max_inventory)
        half_spread = (ask_px - bid_px) / 2.0
        bid_px -= skew * half_spread * 0.5   # skew quotes toward offload
        ask_px -= skew * half_spread * 0.5

        # Determine inventory action
        state.action = self._classify_inventory_action(state)
        stage.stop()

        # Cancel existing quotes
        stage = budget.begin_stage("cancel")
        await self._cancel_symbol_quotes(state)
        stage.stop()

        # Do not quote if at hard inventory limit
        if abs(state.inventory) >= self._max_inventory:
            state.action = InventoryAction.PULL_QUOTES
            logger.warning("[MM] %s inventory limit hit (%.0f shares) — pulling quotes",
                           symbol, state.inventory)
            return

        # Submit new bid/ask pair
        stage = budget.begin_stage("submit")
        quote_size = self._compute_quote_size(state, mid)
        bid_id = await self._order_fn(symbol, OrderSide.BUY, quote_size, round(bid_px, 4))
        ask_id = await self._order_fn(symbol, OrderSide.SELL, quote_size, round(ask_px, 4))
        stage.stop()

        state.active_bid_id = bid_id
        state.active_ask_id = ask_id
        state.active_bid_price = bid_px
        state.active_ask_price = ask_px

        # Publish quote metadata to bus
        await self._bus.publish(BusMessage(
            topic=Topic.ORDER_SUBMITTED,
            payload={
                "agent": self.AGENT_NAME,
                "symbol": symbol,
                "bid_price": bid_px,
                "ask_price": ask_px,
                "bid_id": bid_id,
                "ask_id": ask_id,
                "inventory": state.inventory,
                "mid": mid,
                "spread_bps": (ask_px - bid_px) / mid * 10_000,
                "action": state.action.value,
            },
            source_agent=self.AGENT_NAME,
        ))

        total_us = budget.total_us
        if total_us > 1000:
            logger.warning("[MM] %s quote refresh took %.0fµs", symbol, total_us)

    def _compute_quote_size(self, state: MMSymbolState, mid: float) -> float:
        """
        Determine quote size based on inventory position and A-S model.
        Reduce size when near inventory limit.
        """
        base_size = 100.0
        inv_fraction = abs(state.inventory) / self._max_inventory
        # Reduce quote size linearly as inventory approaches limit
        scale = max(0.1, 1.0 - inv_fraction)
        return max(1.0, round(base_size * scale))

    def _classify_inventory_action(self, state: MMSymbolState) -> InventoryAction:
        inv_fraction = state.inventory / self._max_inventory
        if inv_fraction > 0.7:
            return InventoryAction.SKEW_BID      # unload long inventory
        if inv_fraction < -0.7:
            return InventoryAction.SKEW_ASK      # unload short inventory
        return InventoryAction.NEUTRAL

    async def _cancel_symbol_quotes(self, state: MMSymbolState) -> None:
        for oid in (state.active_bid_id, state.active_ask_id):
            if oid:
                try:
                    await self._cancel_fn(oid)
                except Exception:
                    pass
        state.active_bid_id = None
        state.active_ask_id = None

    async def _cancel_all_quotes(self) -> None:
        for state in self._states.values():
            await self._cancel_symbol_quotes(state)

    # ── Fill Handling ─────────────────────────────────────────────────────────

    async def _on_fill(self, message: BusMessage) -> None:
        payload = message.payload
        symbol = payload.get("symbol", "")
        if symbol not in self._states:
            return

        state = self._states[symbol]
        side = payload.get("side", "buy")
        fill_qty = float(payload.get("qty", 0))
        fill_price = float(payload.get("price", 0))

        # Update inventory
        if side in ("buy", "buy_to_cover"):
            state.inventory += fill_qty
        else:
            state.inventory -= fill_qty

        # Approximate P&L: vs mid at fill time
        mid = self._get_mid_fn(symbol) or fill_price
        edge = abs(fill_price - mid)   # distance from mid = captured spread component
        state.total_pnl += edge * fill_qty
        self._session_pnl += edge * fill_qty
        state.fills_today += 1
        self._total_fills += 1

        logger.debug(
            "[MM] Fill %s %s qty=%.0f @%.4f | inv=%.0f | P&L=$%.2f",
            symbol, side, fill_qty, fill_price, state.inventory, state.total_pnl,
        )

        # Publish position update
        await self._bus.publish(BusMessage(
            topic=Topic.POSITION_UPDATE,
            payload={
                "agent": self.AGENT_NAME,
                "symbol": symbol,
                "inventory": state.inventory,
                "pnl": state.total_pnl,
                "action": state.action.value,
            },
            source_agent=self.AGENT_NAME,
        ))

    async def _on_kill_switch(self, message: BusMessage) -> None:
        logger.critical("[MM] KILL SWITCH received — pulling all quotes immediately")
        self._running = False
        await self._cancel_all_quotes()

    async def _on_session_end(self, message: BusMessage) -> None:
        logger.info("[MM] Session end — pulling all quotes. Day P&L: $%.2f", self._session_pnl)
        await self._cancel_all_quotes()
        self._running = False

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            await self._bus.publish(BusMessage(
                topic=Topic.AGENT_HEARTBEAT,
                payload={
                    "agent_id": self.AGENT_ID,
                    "agent_name": self.AGENT_NAME,
                    "state": self._state.value,
                    "session_pnl": self._session_pnl,
                    "total_fills": self._total_fills,
                    "symbols": len(self._symbols),
                },
                source_agent=self.AGENT_NAME,
            ))

    # ── Introspection ─────────────────────────────────────────────────────────

    def inventory_report(self) -> list[dict]:
        return [
            {
                "symbol": sym,
                "inventory": s.inventory,
                "inventory_risk_60s": s.inventory_var(60.0),
                "pnl": s.total_pnl,
                "fills": s.fills_today,
                "action": s.action.value,
                "bid": s.active_bid_price,
                "ask": s.active_ask_price,
            }
            for sym, s in self._states.items()
        ]

    @property
    def session_pnl(self) -> float:
        return self._session_pnl

    @property
    def total_fills(self) -> int:
        return self._total_fills
