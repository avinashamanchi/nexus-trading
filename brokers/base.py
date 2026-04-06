"""
Abstract broker interface.  All broker implementations must subclass BrokerBase.

The Execution Agent (Agent 8) and Broker Reconciliation Agent (Agent 9) interact
exclusively through this interface — never directly with a specific broker SDK.
This ensures broker portability and testability.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from core.models import Order, Position, FillEvent
from core.enums import OrderType, OrderSide, TimeInForce


@dataclass
class AccountInfo:
    account_id: str
    equity: float
    buying_power: float
    cash: float
    day_trade_buying_power: float
    pattern_day_trader: bool
    day_trades_remaining: int       # PDT counter for rolling 5-day window
    currency: str = "USD"


@dataclass
class BrokerPosition:
    symbol: str
    qty: int                        # positive = long, negative = short
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float
    side: str                       # "long" | "short"


@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: int
    order_type: str
    status: str
    filled_qty: int
    avg_fill_price: float | None
    limit_price: float | None
    stop_price: float | None
    submitted_at: datetime | None
    filled_at: datetime | None


class BrokerBase(ABC):
    """
    Abstract base for all broker integrations.
    All methods are async.
    """

    # ── Connection ────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to broker API. Raises BrokerConnectionError on failure."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close all connections."""

    @abstractmethod
    async def is_connected(self) -> bool:
        """Return True if the broker API is reachable."""

    # ── Account ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_account(self) -> AccountInfo:
        """Return current account equity, buying power, and PDT status."""

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
    async def submit_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        limit_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
        client_order_id: str | None = None,
    ) -> BrokerOrder:
        """Submit a limit order. Returns immediately with broker order state."""

    @abstractmethod
    async def submit_bracket_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
        client_order_id: str | None = None,
    ) -> BrokerOrder:
        """
        Submit a bracket order (entry + stop + target in a single submission).
        Required capability per §7.7.
        """

    @abstractmethod
    async def submit_market_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        client_order_id: str | None = None,
    ) -> BrokerOrder:
        """
        Submit a market order.
        EMERGENCY USE ONLY — never used for normal entries.
        Used for emergency position flattening.
        """

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""

    @abstractmethod
    async def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns count of cancelled orders."""

    @abstractmethod
    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        """Fetch current state of a specific order."""

    @abstractmethod
    async def get_open_orders(self) -> list[BrokerOrder]:
        """Return all currently open orders."""

    # ── Positions ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]:
        """Return all open positions at the broker."""

    @abstractmethod
    async def close_position(self, symbol: str) -> BrokerOrder | None:
        """
        Close an entire position for a symbol via market order.
        Emergency use only — used by kill switch and PSA.
        """

    @abstractmethod
    async def close_all_positions(self) -> list[BrokerOrder]:
        """
        Flatten all positions.
        Called by the kill switch and PSA circuit breaker.
        """

    # ── Market Status ─────────────────────────────────────────────────────────

    @abstractmethod
    async def is_market_open(self) -> bool:
        """Return True if the primary market is currently open."""

    @abstractmethod
    async def get_clock(self) -> dict:
        """Return broker market clock (next open, next close, is_open)."""
