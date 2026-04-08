"""
Market Data Feed — real-time L1 + L2 via Alpaca WebSocket streams.

Architecture:
  - AlpacaDataFeed subscribes to trades, quotes, and bars via WebSocket.
  - Data is published to the internal tick cache AND to the message bus
    so all agents receive live price updates.
  - The Data Integrity Agent (Agent 3) reads from the same feed and
    validates every tick before marking the feed as "clean".
  - Level 2 data is handled in data/level2.py.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Awaitable

from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar, Quote, Trade

from core.models import BarData, MarketTick

if TYPE_CHECKING:
    from infrastructure.kernel_bypass import KernelBypassFeed

logger = logging.getLogger(__name__)

from data.base import DataFeedBase, TickHandler, BarHandler


class AlpacaDataFeed(DataFeedBase):
    """
    Alpaca WebSocket market data feed.

    Provides:
      - Real-time L1 quotes (bid/ask) and last-trade prices
      - 1-minute OHLCV bars
      - In-memory tick cache for the last N ticks per symbol

    Usage:
        feed = AlpacaDataFeed(api_key, secret_key, paper=True)
        feed.add_tick_handler(my_handler)
        await feed.subscribe(["AAPL", "TSLA"])
        await feed.run()

    Kernel-bypass mode:
        bypass_feed = KernelBypassFeed()
        feed = AlpacaDataFeed(api_key, secret_key, bypass=bypass_feed)
        await feed.run()   # delegates to KernelBypassAdapter instead of WebSocket
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        tick_cache_size: int = 500,
        bypass: "KernelBypassFeed | None" = None,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._tick_cache_size = tick_cache_size
        self._bypass = bypass

        self._stream: StockDataStream | None = None
        self._subscribed_symbols: set[str] = set()
        self._tick_handlers: list[TickHandler] = []
        self._bar_handlers: list[BarHandler] = []

        # In-memory caches
        self._latest_ticks: dict[str, MarketTick] = {}
        self._tick_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=tick_cache_size)
        )
        self._bar_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=200)
        )

        self._running = False
        self._sequence: dict[str, int] = defaultdict(int)

    def add_tick_handler(self, handler: TickHandler) -> None:
        self._tick_handlers.append(handler)

    def add_bar_handler(self, handler: BarHandler) -> None:
        self._bar_handlers.append(handler)

    async def subscribe(self, symbols: list[str]) -> None:
        self._subscribed_symbols.update(s.upper() for s in symbols)

    async def unsubscribe(self, symbols: list[str]) -> None:
        for s in symbols:
            self._subscribed_symbols.discard(s.upper())

    def get_latest_tick(self, symbol: str) -> MarketTick | None:
        return self._latest_ticks.get(symbol.upper())

    def get_tick_history(self, symbol: str, n: int = 50) -> list[MarketTick]:
        hist = self._tick_history.get(symbol.upper(), deque())
        return list(hist)[-n:]

    def get_bar_history(self, symbol: str, n: int = 20) -> list[BarData]:
        hist = self._bar_history.get(symbol.upper(), deque())
        return list(hist)[-n:]

    async def run(self) -> None:
        """Start the WebSocket feed. Blocks until stopped.

        If a KernelBypassFeed was provided at construction, delegates to
        KernelBypassAdapter instead of opening a WebSocket connection.
        """
        if self._bypass is not None:
            from infrastructure.kernel_bypass import KernelBypassAdapter
            adapter = KernelBypassAdapter(self._bypass, self._tick_handlers)
            self._running = True
            try:
                await adapter.run()
            finally:
                self._running = False
            return

        feed_type = "iex" if not self._paper else "iex"
        self._stream = StockDataStream(
            api_key=self._api_key,
            secret_key=self._secret_key,
            feed=feed_type,
        )

        symbols = list(self._subscribed_symbols)
        if not symbols:
            logger.warning("DataFeed: no symbols subscribed — feed will be idle")
            return

        self._stream.subscribe_quotes(self._on_quote, *symbols)
        self._stream.subscribe_bars(self._on_bar, *symbols)
        self._running = True
        logger.info("DataFeed starting for %d symbols: %s", len(symbols), symbols)

        try:
            await self._stream.run()
        except Exception as exc:
            logger.error("DataFeed stream error: %s", exc)
        finally:
            self._running = False

    async def stop(self) -> None:
        if self._stream:
            await self._stream.stop_ws()
        self._running = False

    async def _on_quote(self, quote: Quote) -> None:
        seq = self._sequence[quote.symbol]
        self._sequence[quote.symbol] += 1

        tick = MarketTick(
            symbol=quote.symbol,
            timestamp=quote.timestamp or datetime.utcnow(),
            bid=float(quote.bid_price or 0),
            ask=float(quote.ask_price or 0),
            last=self._latest_ticks.get(quote.symbol, MarketTick(
                symbol=quote.symbol,
                timestamp=datetime.utcnow(),
                bid=float(quote.bid_price or 0),
                ask=float(quote.ask_price or 0),
                last=float(quote.bid_price or 0),
                volume=0,
            )).last,
            volume=0,
            sequence=seq,
        )

        self._latest_ticks[quote.symbol] = tick
        self._tick_history[quote.symbol].append(tick)

        for handler in self._tick_handlers:
            try:
                await handler(tick)
            except Exception as exc:
                logger.exception("Tick handler error: %s", exc)

    async def _on_bar(self, bar: Bar) -> None:
        bar_data = BarData(
            symbol=bar.symbol,
            timestamp=bar.timestamp or datetime.utcnow(),
            timeframe="1m",
            open=float(bar.open or 0),
            high=float(bar.high or 0),
            low=float(bar.low or 0),
            close=float(bar.close or 0),
            volume=int(bar.volume or 0),
            vwap=float(bar.vwap) if bar.vwap else None,
        )
        self._bar_history[bar.symbol].append(bar_data)

        # Update last price in tick cache from bar close
        if bar.symbol in self._latest_ticks:
            existing = self._latest_ticks[bar.symbol]
            updated = existing.model_copy(
                update={"last": float(bar.close or 0), "volume": int(bar.volume or 0)}
            )
            self._latest_ticks[bar.symbol] = updated

        for handler in self._bar_handlers:
            try:
                await handler(bar_data)
            except Exception as exc:
                logger.exception("Bar handler error: %s", exc)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed_symbols(self) -> set[str]:
        return set(self._subscribed_symbols)
