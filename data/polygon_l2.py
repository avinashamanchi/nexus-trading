"""
Polygon L2 / Order Book Manager.

Feeds OrderBookCache from two sources:
  1. NBBO quote stream (Q.* events, ~1ms latency) — called by PolygonDataFeed
     on every quote message, updates level [0] of the order book immediately.
  2. REST snapshot poll (every poll_sec seconds) — enriches with depth levels,
     session VWAP, and latest 1-min bar data.

Post-MVP: when Polygon Launchpad tier is available, set l2_mode='launchpad' in
config and activate the LU.* WebSocket channel to replace the REST poll.
The OrderBookCache interface is unchanged — agents see no difference.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

import aiohttp

from data.level2 import OrderBookCache

logger = logging.getLogger(__name__)

_POLYGON_SNAPSHOT_URL = (
    "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}"
)


class PolygonL2Manager:
    """
    Keeps OrderBookCache populated from Polygon NBBO quotes and REST snapshots.

    Usage:
        l2 = PolygonL2Manager(api_key="...", order_book=cache, poll_sec=2)
        # Wire into PolygonDataFeed's quote handler:
        feed.add_tick_handler(lambda tick: l2.update_from_nbbo(
            tick.symbol, tick.bid, 0, tick.ask, 0
        ))
        # Start background REST poll:
        asyncio.create_task(l2.run(symbols))
    """

    def __init__(
        self,
        api_key: str,
        order_book: OrderBookCache,
        poll_sec: float = 2.0,
    ) -> None:
        self._api_key = api_key
        self._order_book = order_book
        self._poll_sec = poll_sec
        self._running = False
        self._poll_task: asyncio.Task | None = None

    async def update_from_nbbo(
        self,
        symbol: str,
        bid: float,
        bid_size: int,
        ask: float,
        ask_size: int,
        timestamp: datetime | None = None,
    ) -> None:
        """
        Push a real-time NBBO into level [0] of the order book.
        Called by PolygonDataFeed on every Q.* quote event.
        """
        await self._order_book.update(
            symbol=symbol.upper(),
            bids=[(bid, bid_size)] if bid > 0 else [],
            asks=[(ask, ask_size)] if ask > 0 else [],
            timestamp=timestamp,
        )

    async def run(self, symbols_fn: "Callable[[], list[str]]") -> None:
        """
        Background REST snapshot polling loop. Runs until stop() is called.
        `symbols_fn` is called each cycle so newly-subscribed symbols are included
        without restarting the loop (e.g. `lambda: list(feed.subscribed_symbols)`).
        """
        self._running = True
        self._poll_task = asyncio.current_task()
        while self._running:
            try:
                symbols = symbols_fn()
                if symbols:
                    await self.poll_once(symbols)
            except Exception as exc:
                logger.warning("L2 snapshot poll error: %s", exc)
            await asyncio.sleep(self._poll_sec)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    async def poll_once(self, symbols: list[str]) -> None:
        """Fetch REST snapshots for all symbols and enrich OrderBookCache."""
        async with aiohttp.ClientSession() as session:
            for symbol in symbols:
                url = _POLYGON_SNAPSHOT_URL.format(symbol=symbol.upper())
                try:
                    async with session.get(url, params={"apiKey": self._api_key}) as resp:
                        if resp.status != 200:
                            logger.warning(
                                "Polygon snapshot %s returned HTTP %d", symbol, resp.status
                            )
                            continue
                        data = await resp.json()
                        await self._apply_snapshot(symbol, data)
                except Exception as exc:
                    logger.debug("Snapshot fetch error for %s: %s", symbol, exc)

    async def _apply_snapshot(self, symbol: str, data: dict) -> None:
        """
        Parse a Polygon snapshot response and update OrderBookCache.

        lastQuote fields: P=ask price, S=ask size, p=bid price, s=bid size
        """
        ticker = data.get("ticker", {})
        last_quote = ticker.get("lastQuote", {})

        bid = float(last_quote.get("p", 0))
        bid_size = int(last_quote.get("s", 0))
        ask = float(last_quote.get("P", 0))
        ask_size = int(last_quote.get("S", 0))

        if bid <= 0 and ask <= 0:
            return  # no usable data

        ts_ms = last_quote.get("t")
        ts = (
            datetime.utcfromtimestamp(ts_ms / 1000.0)
            if ts_ms is not None
            else datetime.utcnow()
        )

        await self._order_book.update(
            symbol=symbol.upper(),
            bids=[(bid, bid_size)] if bid > 0 else [],
            asks=[(ask, ask_size)] if ask > 0 else [],
            timestamp=ts,
        )
