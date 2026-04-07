"""
Level 2 / Order Book data.

MiSA requires L2 data to detect liquidity sweeps (per §7.3).
The Data Integrity Agent monitors for missing L2 snapshots.

Note: Alpaca IEX feed provides limited L2 data. For full depth-of-book,
a separate data provider (e.g., Polygon.io) may be required.
This module provides the abstraction layer.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Callable, Awaitable

from core.models import L2Level, OrderBook

logger = logging.getLogger(__name__)

OrderBookHandler = Callable[[OrderBook], Awaitable[None]]


class OrderBookCache:
    """
    In-memory order book cache.
    Maintains the best 10 levels of bid/ask for each symbol.
    Updated from WebSocket snapshots and incremental updates.
    """

    def __init__(self, max_levels: int = 10) -> None:
        self._max_levels = max_levels
        self._books: dict[str, OrderBook] = {}
        self._last_updated: dict[str, datetime] = {}
        self._handlers: list[OrderBookHandler] = []

    def add_handler(self, handler: OrderBookHandler) -> None:
        self._handlers.append(handler)

    async def update(
        self,
        symbol: str,
        bids: list[tuple[float, int]],
        asks: list[tuple[float, int]],
        timestamp: datetime | None = None,
    ) -> None:
        """Update the order book for a symbol from raw bid/ask tuples."""
        ts = timestamp or datetime.utcnow()
        sorted_bids = sorted(bids, key=lambda x: x[0], reverse=True)[: self._max_levels]
        sorted_asks = sorted(asks, key=lambda x: x[0])[: self._max_levels]

        book = OrderBook(
            symbol=symbol,
            timestamp=ts,
            bids=[L2Level(price=p, size=s) for p, s in sorted_bids if s > 0],
            asks=[L2Level(price=p, size=s) for p, s in sorted_asks if s > 0],
        )
        self._books[symbol] = book
        self._last_updated[symbol] = ts

        for handler in self._handlers:
            try:
                await handler(book)
            except Exception as exc:
                logger.exception("OrderBook handler error: %s", exc)

    def get_book(self, symbol: str) -> OrderBook | None:
        return self._books.get(symbol.upper())

    def get_last_updated(self, symbol: str) -> datetime | None:
        return self._last_updated.get(symbol.upper())

    def is_stale(self, symbol: str, max_age_sec: float = 10.0) -> bool:
        """Return True if the order book snapshot is older than max_age_sec."""
        last = self.get_last_updated(symbol)
        if not last:
            return True
        return (datetime.utcnow() - last).total_seconds() > max_age_sec

    def bid_size_at(self, symbol: str, price: float) -> int:
        """Total bid size at a specific price level."""
        book = self._books.get(symbol.upper())
        if not book:
            return 0
        for level in book.bids:
            if abs(level.price - price) < 0.001:
                return level.size
        return 0

    def ask_size_at(self, symbol: str, price: float) -> int:
        book = self._books.get(symbol.upper())
        if not book:
            return 0
        for level in book.asks:
            if abs(level.price - price) < 0.001:
                return level.size
        return 0

    def total_bid_depth(self, symbol: str, levels: int = 5) -> int:
        book = self._books.get(symbol.upper())
        if not book:
            return 0
        return sum(l.size for l in book.bids[:levels])

    def total_ask_depth(self, symbol: str, levels: int = 5) -> int:
        book = self._books.get(symbol.upper())
        if not book:
            return 0
        return sum(l.size for l in book.asks[:levels])

    def detect_liquidity_sweep(
        self, symbol: str, direction: str, min_sweep_size: int = 5000
    ) -> bool:
        """
        Detect a liquidity sweep — large order consuming multiple price levels.
        Used by MiSA to identify high-probability momentum entries.

        direction: "bid" (buying pressure) or "ask" (selling pressure)
        """
        book = self._books.get(symbol.upper())
        if not book:
            return False

        if direction == "bid":
            depth = self.total_bid_depth(symbol, levels=3)
        else:
            depth = self.total_ask_depth(symbol, levels=3)

        return depth < min_sweep_size  # thin book after sweep


class MockOrderBookFeed:
    """
    Synthetic order book for paper trading / shadow mode.
    Generates realistic-looking L2 data from L1 quotes.
    """

    def __init__(self, cache: OrderBookCache) -> None:
        self._cache = cache

    async def update_from_tick(
        self, symbol: str, bid: float, ask: float, last_volume: int = 0
    ) -> None:
        """Generate synthetic L2 levels from an L1 tick."""
        spread = ask - bid
        tick_size = 0.01

        bids = []
        asks = []
        for i in range(10):
            bid_price = round(bid - i * tick_size, 2)
            ask_price = round(ask + i * tick_size, 2)
            # Simulate realistic size distribution (more at best levels)
            size = max(100, int(1000 / (i + 1)))
            bids.append((bid_price, size))
            asks.append((ask_price, size))

        await self._cache.update(symbol, bids, asks)


from data.mbo_orderbook import MBOOrderBookCache as MBOOrderBookCache  # noqa: re-export
