"""
Abstract data feed interface.

All market data feed implementations must subclass DataFeedBase.
Agents 3, 4, 5, 8, 11 interact exclusively through this interface —
never directly with a specific feed SDK.

Pattern mirrors brokers/base.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Awaitable

from core.models import BarData, MarketTick

TickHandler = Callable[[MarketTick], Awaitable[None]]
BarHandler = Callable[[BarData], Awaitable[None]]


class DataFeedBase(ABC):
    """Abstract base for all market data feed implementations."""

    @abstractmethod
    def add_tick_handler(self, handler: TickHandler) -> None:
        """Register a coroutine to be called on every new MarketTick."""

    @abstractmethod
    def add_bar_handler(self, handler: BarHandler) -> None:
        """Register a coroutine to be called on every completed BarData."""

    @abstractmethod
    async def subscribe(self, symbols: list[str]) -> None:
        """Add symbols to the active subscription set."""

    @abstractmethod
    async def unsubscribe(self, symbols: list[str]) -> None:
        """Remove symbols from the active subscription set."""

    @abstractmethod
    def get_latest_tick(self, symbol: str) -> MarketTick | None:
        """Return the most recent MarketTick for a symbol, or None."""

    @abstractmethod
    def get_tick_history(self, symbol: str, n: int = 50) -> list[MarketTick]:
        """Return the last n ticks for a symbol (oldest-first)."""

    @abstractmethod
    def get_bar_history(self, symbol: str, n: int = 20) -> list[BarData]:
        """Return the last n completed 1-min bars for a symbol (oldest-first)."""

    def get_recent_bars(
        self, symbol: str, timeframe: str = "1m", limit: int = 20
    ) -> list[BarData]:
        """
        Convenience alias for get_bar_history.
        `timeframe` is accepted but ignored — only 1m bars are cached.
        Provided for Agent 11 compatibility.
        """
        return self.get_bar_history(symbol, n=limit)

    @abstractmethod
    async def run(self) -> None:
        """Start the feed. Blocks until stopped or a fatal error occurs."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the feed."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """True if the feed is currently connected and streaming."""

    @property
    @abstractmethod
    def subscribed_symbols(self) -> set[str]:
        """Return the set of currently subscribed symbols."""
