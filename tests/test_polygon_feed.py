"""Unit tests for PolygonDataFeed using fixture WebSocket messages. No network required."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.models import BarData, MarketTick
from data.base import DataFeedBase


# ── Fixtures ──────────────────────────────────────────────────────────────────

TRADE_MSG = json.dumps([{"ev": "T", "sym": "AAPL", "p": 150.50, "s": 100, "t": 1623081600000, "q": 123}])
QUOTE_MSG = json.dumps([{"ev": "Q", "sym": "AAPL", "bp": 150.49, "bs": 500, "ap": 150.51, "as": 300, "t": 1623081600001, "q": 456}])
BAR_MSG = json.dumps([{"ev": "A", "sym": "AAPL", "v": 1000, "vw": 150.55, "o": 150.49, "c": 150.52, "h": 150.57, "l": 150.45, "s": 1623081600000, "e": 1623081660000}])
SEC_BAR_MSG = json.dumps([{"ev": "AM", "sym": "AAPL", "v": 50, "vw": 150.55, "o": 150.50, "c": 150.51, "s": 1623081600000}])
AUTH_OK = json.dumps([{"ev": "status", "status": "auth_success", "message": "authenticated"}])
AUTH_FAIL = json.dumps([{"ev": "status", "status": "auth_failed", "message": "Your API Key is not valid."}])
CONNECTED = json.dumps([{"ev": "status", "status": "connected", "message": "Connected Successfully"}])


@pytest.fixture
def feed():
    from data.polygon_feed import PolygonDataFeed
    return PolygonDataFeed(api_key="test_key", use_delayed=True)


# ── Interface ─────────────────────────────────────────────────────────────────

def test_polygon_feed_is_data_feed_base(feed):
    assert isinstance(feed, DataFeedBase)


def test_polygon_feed_not_running_initially(feed):
    assert feed.is_running is False


def test_subscribed_symbols_empty_initially(feed):
    assert feed.subscribed_symbols == set()


@pytest.mark.asyncio
async def test_subscribe_adds_symbols(feed):
    await feed.subscribe(["AAPL", "SPY"])
    assert "AAPL" in feed.subscribed_symbols
    assert "SPY" in feed.subscribed_symbols


@pytest.mark.asyncio
async def test_unsubscribe_removes_symbol(feed):
    await feed.subscribe(["AAPL", "SPY"])
    await feed.unsubscribe(["SPY"])
    assert "SPY" not in feed.subscribed_symbols
    assert "AAPL" in feed.subscribed_symbols


# ── Trade message parsing ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trade_message_updates_last_and_volume(feed):
    tick_received: list[MarketTick] = []
    feed.add_tick_handler(lambda t: asyncio.coroutine(lambda: tick_received.append(t))())

    async def _handler(t: MarketTick) -> None:
        tick_received.append(t)

    feed.add_tick_handler(_handler)
    await feed._process_message(TRADE_MSG)

    assert len(tick_received) == 1
    tick = tick_received[0]
    assert tick.symbol == "AAPL"
    assert tick.last == 150.50
    assert tick.volume == 100
    assert tick.sequence == 123


# ── Quote message parsing ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quote_message_updates_bid_ask(feed):
    ticks: list[MarketTick] = []

    async def _handler(t: MarketTick) -> None:
        ticks.append(t)

    feed.add_tick_handler(_handler)
    await feed._process_message(QUOTE_MSG)

    assert len(ticks) == 1
    tick = ticks[0]
    assert tick.bid == 150.49
    assert tick.ask == 150.51
    assert tick.symbol == "AAPL"


# ── Bar message parsing ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bar_message_maps_ohlcv_and_vwap(feed):
    bars: list[BarData] = []

    async def _handler(b: BarData) -> None:
        bars.append(b)

    feed.add_bar_handler(_handler)
    await feed._process_message(BAR_MSG)

    assert len(bars) == 1
    bar = bars[0]
    assert bar.symbol == "AAPL"
    assert bar.open == 150.49
    assert bar.close == 150.52
    assert bar.high == 150.57
    assert bar.low == 150.45
    assert bar.volume == 1000
    assert bar.vwap == 150.55
    assert bar.timeframe == "1m"


# ── 1-sec bar (AM.*) — VWAP accumulation ─────────────────────────────────────

@pytest.mark.asyncio
async def test_sec_bar_accumulates_intraday_vwap(feed):
    """Two AM messages — intraday VWAP should be cumulative weighted average."""
    msg1 = json.dumps([{"ev": "AM", "sym": "AAPL", "v": 100, "vw": 150.00, "o": 150.0, "c": 150.0, "s": 1623081600000}])
    msg2 = json.dumps([{"ev": "AM", "sym": "AAPL", "v": 200, "vw": 151.00, "o": 151.0, "c": 151.0, "s": 1623081601000}])
    await feed._process_message(msg1)
    await feed._process_message(msg2)
    # weighted avg: (100*150 + 200*151) / 300 = (15000 + 30200) / 300 = 150.666...
    vwap = feed.get_intraday_vwap("AAPL")
    assert vwap is not None
    assert abs(vwap - 150.666) < 0.01


# ── Merge: trade + quote per symbol ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_trade_then_quote_merges_into_single_tick(feed):
    """After a trade then a quote for the same symbol, latest tick has both last and bid/ask."""
    ticks: list[MarketTick] = []

    async def _handler(t: MarketTick) -> None:
        ticks.append(t)

    feed.add_tick_handler(_handler)
    await feed._process_message(TRADE_MSG)
    await feed._process_message(QUOTE_MSG)

    latest = feed.get_latest_tick("AAPL")
    assert latest is not None
    assert latest.last == 150.50    # from trade
    assert latest.bid == 150.49     # from quote
    assert latest.ask == 150.51     # from quote


# ── get_latest_tick / get_tick_history / get_bar_history ──────────────────────

@pytest.mark.asyncio
async def test_get_latest_tick_none_before_any_message(feed):
    assert feed.get_latest_tick("AAPL") is None


@pytest.mark.asyncio
async def test_get_tick_history_after_messages(feed):
    async def _noop(t):
        pass
    feed.add_tick_handler(_noop)
    await feed._process_message(TRADE_MSG)
    await feed._process_message(QUOTE_MSG)
    history = feed.get_tick_history("AAPL", n=10)
    assert len(history) == 2


@pytest.mark.asyncio
async def test_get_bar_history_after_bar_message(feed):
    async def _noop(b):
        pass
    feed.add_bar_handler(_noop)
    await feed._process_message(BAR_MSG)
    bars = feed.get_bar_history("AAPL", n=10)
    assert len(bars) == 1
    assert bars[0].vwap == 150.55


# ── Auth failure raises FeedConnectionError ───────────────────────────────────

@pytest.mark.asyncio
async def test_auth_failure_raises_feed_connection_error(feed):
    from core.exceptions import FeedConnectionError
    with pytest.raises(FeedConnectionError):
        await feed._handle_auth_response(AUTH_FAIL)


# ── use_delayed sets correct WS URL ──────────────────────────────────────────

def test_use_delayed_true_uses_delayed_url():
    from data.polygon_feed import PolygonDataFeed
    f = PolygonDataFeed(api_key="k", use_delayed=True)
    assert "delayed.polygon.io" in f._ws_url


def test_use_delayed_false_uses_realtime_url():
    from data.polygon_feed import PolygonDataFeed
    f = PolygonDataFeed(api_key="k", use_delayed=False)
    assert "socket.polygon.io" in f._ws_url
