"""
Polygon/Massive WebSocket market data feed.

Connects to wss://socket.polygon.io/stocks (real-time) or
wss://delayed.polygon.io/stocks (15-min delayed, free tier).

Handles four channels:
  T.*  — trade events  → MarketTick.last, .volume, .sequence
  Q.*  — quote events  → MarketTick.bid, .ask
  A.*  — 1-min bars    → BarData (OHLCV + VWAP)
  AM.* — 1-sec bars    → intraday VWAP accumulation

Trades and quotes are merged per symbol into a single MarketTick by
overlaying the latest known values from each message type.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from core.exceptions import FeedConnectionError
from core.models import BarData, MarketTick
from data.base import DataFeedBase, BarHandler, TickHandler

logger = logging.getLogger(__name__)

_WS_URL_REALTIME = "wss://socket.polygon.io/stocks"
_WS_URL_DELAYED = "wss://delayed.polygon.io/stocks"


class PolygonDataFeed(DataFeedBase):
    """
    Polygon/Massive WebSocket market data feed.

    Usage:
        feed = PolygonDataFeed(api_key="...", use_delayed=False)
        feed.add_tick_handler(my_tick_handler)
        feed.add_bar_handler(my_bar_handler)
        await feed.subscribe(["AAPL", "SPY"])
        await feed.run()   # blocks until stopped
    """

    def __init__(
        self,
        api_key: str,
        use_delayed: bool = False,
        tick_cache_size: int = 500,
        reconnect_delay_sec: float = 5.0,
        max_reconnect_attempts: int = 10,
    ) -> None:
        self._api_key = api_key
        self._ws_url = _WS_URL_DELAYED if use_delayed else _WS_URL_REALTIME
        self._tick_cache_size = tick_cache_size
        self._reconnect_delay_sec = reconnect_delay_sec
        self._max_reconnect_attempts = max_reconnect_attempts

        self._subscribed_symbols: set[str] = set()
        self._tick_handlers: list[TickHandler] = []
        self._bar_handlers: list[BarHandler] = []

        # In-memory caches
        self._latest_ticks: dict[str, MarketTick] = {}
        self._tick_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=tick_cache_size)
        )
        self._bar_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))

        # Intraday VWAP accumulation (from AM.* 1-sec bars)
        # Stored as (cumulative_dollar_volume, cumulative_shares)
        self._intraday_vwap_acc: dict[str, tuple[float, int]] = {}

        # Held-state cache for merging T.* and Q.* into a single tick
        # Keyed by symbol, stores partial {last, volume, bid, ask, sequence}
        self._tick_state: dict[str, dict] = defaultdict(
            lambda: {"last": 0.0, "volume": 0, "bid": 0.0, "ask": 0.0, "sequence": 0}
        )

        self._running = False
        self._ws = None

    # ── DataFeedBase interface ─────────────────────────────────────────────────

    def add_tick_handler(self, handler: TickHandler) -> None:
        self._tick_handlers.append(handler)

    def add_bar_handler(self, handler: BarHandler) -> None:
        self._bar_handlers.append(handler)

    async def subscribe(self, symbols: list[str]) -> None:
        self._subscribed_symbols.update(s.upper() for s in symbols)
        if self._ws and self._running:
            await self._send_subscribe(list(self._subscribed_symbols))

    async def unsubscribe(self, symbols: list[str]) -> None:
        for s in symbols:
            self._subscribed_symbols.discard(s.upper())

    def get_latest_tick(self, symbol: str) -> MarketTick | None:
        return self._latest_ticks.get(symbol.upper())

    def get_tick_history(self, symbol: str, n: int = 50) -> list[MarketTick]:
        return list(self._tick_history.get(symbol.upper(), deque()))[-n:]

    def get_bar_history(self, symbol: str, n: int = 20) -> list[BarData]:
        return list(self._bar_history.get(symbol.upper(), deque()))[-n:]

    def get_intraday_vwap(self, symbol: str) -> float | None:
        """Return the accumulated intraday VWAP from 1-sec AM.* bars."""
        acc = self._intraday_vwap_acc.get(symbol.upper())
        if not acc or acc[1] == 0:
            return None
        return acc[0] / acc[1]

    async def run(self) -> None:
        """Connect to Polygon WebSocket with exponential backoff reconnection."""
        attempts = 0
        while attempts < self._max_reconnect_attempts:
            try:
                await self._connect_and_stream()
                break  # clean stop
            except FeedConnectionError:
                raise  # auth failures are not retried
            except (ConnectionClosedError, ConnectionClosedOK, OSError) as exc:
                attempts += 1
                if attempts >= self._max_reconnect_attempts:
                    raise FeedConnectionError(
                        f"Max reconnect attempts ({self._max_reconnect_attempts}) exhausted: {exc}"
                    )
                delay = self._reconnect_delay_sec * (2 ** (attempts - 1))
                logger.warning(
                    "Feed disconnected (%s). Reconnecting in %.1fs (attempt %d/%d)",
                    exc, delay, attempts, self._max_reconnect_attempts,
                )
                self._running = False
                await asyncio.sleep(delay)
            except Exception as exc:
                logger.exception("Unexpected feed error: %s", exc)
                raise

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed_symbols(self) -> set[str]:
        return set(self._subscribed_symbols)

    # ── Internal: connection lifecycle ────────────────────────────────────────

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(self._ws_url) as ws:
            self._ws = ws

            # Step 1: receive connected message
            raw = await ws.recv()
            msgs = json.loads(raw)
            if not any(m.get("status") == "connected" for m in msgs):
                raise FeedConnectionError(f"Unexpected connect response: {raw}")

            # Step 2: authenticate
            await ws.send(json.dumps({"action": "auth", "params": self._api_key}))
            raw = await ws.recv()
            await self._handle_auth_response(raw)

            # Step 3: subscribe to symbols
            if self._subscribed_symbols:
                await self._send_subscribe(list(self._subscribed_symbols))

            self._running = True
            logger.info(
                "PolygonDataFeed connected (%s) for %d symbols",
                "delayed" if "delayed" in self._ws_url else "real-time",
                len(self._subscribed_symbols),
            )

            # Step 4: stream
            async for message in ws:
                await self._process_message(message)

    async def _handle_auth_response(self, raw: str) -> None:
        msgs = json.loads(raw)
        for m in msgs:
            if m.get("ev") == "status":
                if m.get("status") == "auth_success":
                    return
                if m.get("status") == "auth_failed":
                    raise FeedConnectionError(
                        f"Polygon auth failed: {m.get('message', 'unknown')}"
                    )
        raise FeedConnectionError(f"Unexpected auth response: {raw}")

    async def _send_subscribe(self, symbols: list[str]) -> None:
        channels = []
        for sym in symbols:
            channels.extend([f"T.{sym}", f"Q.{sym}", f"A.{sym}", f"AM.{sym}"])
        await self._ws.send(json.dumps({"action": "subscribe", "params": ",".join(channels)}))

    # ── Internal: message dispatch ─────────────────────────────────────────────

    async def _process_message(self, raw: str) -> None:
        try:
            events = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from Polygon: %s", raw[:200])
            return

        for event in events:
            ev = event.get("ev")
            if ev == "T":
                await self._on_trade(event)
            elif ev == "Q":
                await self._on_quote(event)
            elif ev == "A":
                await self._on_bar(event)
            elif ev == "AM":
                self._on_sec_bar(event)
            elif ev == "status":
                logger.debug("Polygon status: %s", event.get("message"))

    async def _on_trade(self, event: dict) -> None:
        symbol = event["sym"].upper()
        state = self._tick_state[symbol]
        state["last"] = float(event.get("p", state["last"]))
        state["volume"] = int(event.get("s", state["volume"]))
        state["sequence"] = int(event.get("q", state["sequence"]))
        await self._emit_tick(symbol, event.get("t"))

    async def _on_quote(self, event: dict) -> None:
        symbol = event["sym"].upper()
        state = self._tick_state[symbol]
        state["bid"] = float(event.get("bp", state["bid"]))
        state["ask"] = float(event.get("ap", state["ask"]))
        state["sequence"] = int(event.get("q", state["sequence"]))
        await self._emit_tick(symbol, event.get("t"))

    async def _emit_tick(self, symbol: str, ts_ms: int | None) -> None:
        state = self._tick_state[symbol]
        ts = (
            datetime.utcfromtimestamp(ts_ms / 1000.0)
            if ts_ms is not None
            else datetime.utcnow()
        )
        tick = MarketTick(
            symbol=symbol,
            timestamp=ts,
            bid=state["bid"],
            ask=state["ask"],
            last=state["last"],
            volume=state["volume"],
            sequence=state["sequence"],
        )
        self._latest_ticks[symbol] = tick
        self._tick_history[symbol].append(tick)

        for handler in self._tick_handlers:
            try:
                await handler(tick)
            except Exception as exc:
                logger.exception("Tick handler error: %s", exc)

    async def _on_bar(self, event: dict) -> None:
        symbol = event["sym"].upper()
        ts_ms = event.get("s")
        ts = (
            datetime.utcfromtimestamp(ts_ms / 1000.0)
            if ts_ms is not None
            else datetime.utcnow()
        )
        bar = BarData(
            symbol=symbol,
            timestamp=ts,
            timeframe="1m",
            open=float(event.get("o", 0)),
            high=float(event.get("h", 0)),
            low=float(event.get("l", 0)),
            close=float(event.get("c", 0)),
            volume=int(event.get("v", 0)),
            vwap=float(event["vw"]) if event.get("vw") is not None else None,
        )
        self._bar_history[symbol].append(bar)

        # Update last tick price from bar close
        if symbol in self._latest_ticks:
            existing = self._latest_ticks[symbol]
            updated = existing.model_copy(
                update={"last": bar.close, "volume": bar.volume}
            )
            self._latest_ticks[symbol] = updated

        for handler in self._bar_handlers:
            try:
                await handler(bar)
            except Exception as exc:
                logger.exception("Bar handler error: %s", exc)

    def _on_sec_bar(self, event: dict) -> None:
        """Accumulate intraday VWAP from 1-sec AM.* bars (dollar_vol / shares)."""
        symbol = event["sym"].upper()
        vw = float(event.get("vw", 0))
        v = int(event.get("v", 0))
        if v <= 0:
            return
        dollar_vol, shares = self._intraday_vwap_acc.get(symbol, (0.0, 0))
        self._intraday_vwap_acc[symbol] = (dollar_vol + vw * v, shares + v)
