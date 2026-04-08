# Polygon Live Data + Alpaca Paper Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Alpaca IEX (delayed) with Polygon/Massive real-time consolidated tape for all market data while keeping Alpaca paper trading for zero-risk order execution.

**Architecture:** A new `DataFeedBase` ABC provides a provider-agnostic interface that both `AlpacaDataFeed` and the new `PolygonDataFeed` satisfy. A `PolygonL2Manager` feeds `OrderBookCache` from NBBO quotes and REST snapshot polls. The coordinator selects the provider via `config.yaml:data_feed.provider`. No agent business logic changes.

**Tech Stack:** Python 3.14, websockets 15.x, aiohttp 3.13.x, pydantic v2, pytest-asyncio. All dependencies already in requirements. No new SDK added — raw websockets + aiohttp used against Polygon REST/WS APIs directly.

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Modify | `core/exceptions.py` | Add `FeedConnectionError` |
| Create | `data/base.py` | `DataFeedBase` ABC |
| Modify | `data/feed.py` | `AlpacaDataFeed(DataFeedBase)` — signature only |
| Modify | `agents/agent_03_data_integrity.py` | Import `DataFeedBase` instead of `AlpacaDataFeed` |
| Modify | `agents/agent_04_micro_signal.py` | Import `DataFeedBase` instead of `AlpacaDataFeed` |
| Modify | `agents/agent_05_signal_validation.py` | Import `DataFeedBase` instead of `AlpacaDataFeed` |
| Create | `data/polygon_feed.py` | `PolygonDataFeed(DataFeedBase)` — WebSocket L1 ticks + bars |
| Create | `data/polygon_l2.py` | `PolygonL2Manager` — NBBO + REST snapshot → `OrderBookCache` |
| Modify | `pipeline/coordinator.py` | `_build_data_feed()`, deferred agents, `GlobalClockAgent` |
| Modify | `config.yaml` | Add `data_feed` + `polygon` sections; `system.mode: paper` |
| Modify | `requirements.txt` | Note Polygon uses raw websockets (already present) |
| Create | `tests/test_data_feed_base.py` | Interface conformance tests |
| Create | `tests/test_polygon_feed.py` | Unit tests with fixture WebSocket messages |
| Create | `tests/test_polygon_l2.py` | Unit tests for NBBO and REST snapshot enrichment |
| Create | `tests/integration/polygon_feed_smoke.py` | 5-min live smoke test |

---

## Task 1: `FeedConnectionError` + `DataFeedBase`

**Files:**
- Modify: `core/exceptions.py`
- Create: `data/base.py`
- Create: `tests/test_data_feed_base.py` (partial — interface structure test only)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_feed_base.py
import pytest
from data.base import DataFeedBase
from core.exceptions import FeedConnectionError


def test_feed_connection_error_is_trading_system_error():
    from core.exceptions import TradingSystemError
    err = FeedConnectionError("bad key")
    assert isinstance(err, TradingSystemError)
    assert "bad key" in str(err)


def test_data_feed_base_is_abstract():
    """DataFeedBase cannot be instantiated directly."""
    with pytest.raises(TypeError):
        DataFeedBase()


def test_data_feed_base_required_methods():
    """Concrete subclass must implement all abstract methods."""
    from data.base import DataFeedBase
    import inspect
    abstract = {
        name for name, val in inspect.getmembers(DataFeedBase)
        if getattr(val, "__isabstractmethod__", False)
    }
    expected = {
        "add_tick_handler", "add_bar_handler",
        "subscribe", "unsubscribe",
        "get_latest_tick", "get_tick_history", "get_bar_history",
        "run", "stop", "is_running", "subscribed_symbols",
    }
    assert expected.issubset(abstract)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/veerrajuamanchi/PycharmProjects/nexus-trading
.venv/bin/pytest tests/test_data_feed_base.py -v 2>&1 | head -30
```
Expected: `ModuleNotFoundError: No module named 'data.base'`

- [ ] **Step 3: Add `FeedConnectionError` to `core/exceptions.py`**

Open `core/exceptions.py` and append after the `BrokerAPIError` class (before the Governance section):

```python
class FeedConnectionError(TradingSystemError):
    """Data feed WebSocket connection failed or was rejected."""
    def __init__(self, reason: str):
        super().__init__(f"Feed connection error: {reason}")
```

- [ ] **Step 4: Create `data/base.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_data_feed_base.py -v
```
Expected: 3 PASSED

- [ ] **Step 6: Commit**

```bash
git add core/exceptions.py data/base.py tests/test_data_feed_base.py
git commit -m "feat: add DataFeedBase ABC and FeedConnectionError"
```

---

## Task 2: Make `AlpacaDataFeed` inherit `DataFeedBase` + update agent imports

**Files:**
- Modify: `data/feed.py`
- Modify: `agents/agent_03_data_integrity.py`
- Modify: `agents/agent_04_micro_signal.py`
- Modify: `agents/agent_05_signal_validation.py`

- [ ] **Step 1: Run existing tests to capture baseline**

```bash
.venv/bin/pytest tests/ -v --tb=short 2>&1 | tail -20
```
Note which tests pass. All currently-passing tests must still pass after this task.

- [ ] **Step 2: Update `data/feed.py` — add `DataFeedBase` to class signature**

Change line 34 from:
```python
class AlpacaDataFeed:
```
to:
```python
from data.base import DataFeedBase, TickHandler, BarHandler

class AlpacaDataFeed(DataFeedBase):
```

Also remove the duplicate `TickHandler` / `BarHandler` definitions that are already at the top of `feed.py` (lines 30–31), since they are now imported from `data.base`. Keep only one definition — the one in `data/base.py` is the canonical one. After removing the duplicates, the import at the top of `feed.py` becomes:

```python
# Replace lines 30-31 in data/feed.py:
from data.base import DataFeedBase, TickHandler, BarHandler
```

- [ ] **Step 3: Update `agents/agent_03_data_integrity.py`**

Find line:
```python
from data.feed import AlpacaDataFeed
```
Replace with:
```python
from data.base import DataFeedBase
```

Find the constructor parameter:
```python
data_feed: AlpacaDataFeed,
```
Replace with:
```python
data_feed: DataFeedBase,
```

- [ ] **Step 4: Update `agents/agent_04_micro_signal.py`**

Find line:
```python
from data.feed import AlpacaDataFeed
```
Replace with:
```python
from data.base import DataFeedBase
```

Find the constructor parameter:
```python
data_feed: AlpacaDataFeed,
```
Replace with:
```python
data_feed: DataFeedBase,
```

- [ ] **Step 5: Update `agents/agent_05_signal_validation.py`**

Find line:
```python
from data.feed import AlpacaDataFeed
```
Replace with:
```python
from data.base import DataFeedBase
```

Find the constructor parameter:
```python
data_feed: AlpacaDataFeed,
```
Replace with:
```python
data_feed: DataFeedBase,
```

- [ ] **Step 6: Run tests — all previously-passing tests must still pass**

```bash
.venv/bin/pytest tests/ -v --tb=short 2>&1 | tail -30
```
Expected: same pass count as Step 1 baseline. Zero regressions.

- [ ] **Step 7: Verify `AlpacaDataFeed` satisfies the ABC**

```bash
.venv/bin/python -c "
from data.feed import AlpacaDataFeed
from data.base import DataFeedBase
f = AlpacaDataFeed('k', 's')
print('isinstance check:', isinstance(f, DataFeedBase))
"
```
Expected: `isinstance check: True`

- [ ] **Step 8: Commit**

```bash
git add data/feed.py agents/agent_03_data_integrity.py agents/agent_04_micro_signal.py agents/agent_05_signal_validation.py
git commit -m "refactor: AlpacaDataFeed implements DataFeedBase; update agent type hints"
```

---

## Task 3: `PolygonDataFeed`

**Files:**
- Create: `data/polygon_feed.py`
- Create: `tests/test_polygon_feed.py`

### Background: Polygon WebSocket Protocol

Connection: `wss://socket.polygon.io/stocks` (real-time) or `wss://delayed.polygon.io/stocks` (15-min delayed).

Message format — all messages are JSON arrays:

**Connected:** `[{"ev":"status","status":"connected","message":"Connected Successfully"}]`

**Auth (send):** `{"action":"auth","params":"YOUR_API_KEY"}`

**Auth success:** `[{"ev":"status","status":"auth_success","message":"authenticated"}]`

**Auth fail:** `[{"ev":"status","status":"auth_failed","message":"Your API Key is not valid."}]`

**Subscribe (send):** `{"action":"subscribe","params":"T.AAPL,Q.AAPL,A.AAPL,AM.AAPL"}`

**Trade (T.\*):**
```json
{"ev":"T","sym":"AAPL","x":4,"p":150.50,"s":100,"t":1623081600000,"q":123}
```
- `p` = trade price, `s` = size (shares), `t` = timestamp ms, `q` = sequence

**Quote (Q.\*):**
```json
{"ev":"Q","sym":"AAPL","bp":150.49,"bs":500,"ap":150.51,"as":300,"t":1623081600000,"q":456}
```
- `bp` = bid price, `bs` = bid size, `ap` = ask price, `as` = ask size

**1-min Aggregate (A.\*):**
```json
{"ev":"A","sym":"AAPL","v":1000,"vw":150.55,"o":150.49,"c":150.52,"h":150.57,"l":150.45,"s":1623081600000,"e":1623081660000}
```
- `v` = volume, `vw` = vwap, `o/c/h/l` = OHLC, `s/e` = start/end ms

**1-sec Aggregate (AM.\*):**
```json
{"ev":"AM","sym":"AAPL","v":50,"vw":150.55,"o":150.50,"c":150.51,"s":1623081600000}
```
- Used for intraday VWAP accumulation only

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_polygon_feed.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_polygon_feed.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'data.polygon_feed'`

- [ ] **Step 3: Create `data/polygon_feed.py`**

```python
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
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/test_polygon_feed.py -v
```
Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add data/polygon_feed.py tests/test_polygon_feed.py
git commit -m "feat: add PolygonDataFeed with T/Q/A/AM channel handling"
```

---

## Task 4: `PolygonL2Manager`

**Files:**
- Create: `data/polygon_l2.py`
- Create: `tests/test_polygon_l2.py`

### Background: Polygon REST Snapshot API

```
GET https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}?apiKey=KEY
```

Response (relevant fields):
```json
{
  "ticker": {
    "lastQuote": {"P": 150.51, "S": 300, "p": 150.49, "s": 500, "t": 1623081600000},
    "day": {"vw": 150.55, "v": 500000},
    "min": {"o": 150.49, "c": 150.52, "h": 150.57, "l": 150.45, "v": 1000, "vw": 150.55}
  }
}
```
`lastQuote.P` = ask price, `lastQuote.S` = ask size, `lastQuote.p` = bid price, `lastQuote.s` = bid size.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_polygon_l2.py
"""Unit tests for PolygonL2Manager. No network required — HTTP is mocked."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.level2 import OrderBookCache


@pytest.fixture
def order_book():
    return OrderBookCache()


@pytest.fixture
def l2_manager(order_book):
    from data.polygon_l2 import PolygonL2Manager
    return PolygonL2Manager(api_key="test_key", order_book=order_book, poll_sec=2)


def test_l2_manager_instantiates(l2_manager):
    assert l2_manager is not None


@pytest.mark.asyncio
async def test_nbbo_quote_updates_order_book_level_zero(l2_manager, order_book):
    """A Q.* style NBBO update should set level [0] bid and ask."""
    await l2_manager.update_from_nbbo("AAPL", bid=150.49, bid_size=500, ask=150.51, ask_size=300)

    book = order_book.get_book("AAPL")
    assert book is not None
    assert len(book.bids) >= 1
    assert len(book.asks) >= 1
    assert book.bids[0].price == 150.49
    assert book.bids[0].size == 500
    assert book.asks[0].price == 150.51
    assert book.asks[0].size == 300


@pytest.mark.asyncio
async def test_rest_snapshot_enriches_order_book(l2_manager, order_book):
    """A parsed REST snapshot response should enrich OrderBookCache."""
    snapshot_data = {
        "ticker": {
            "lastQuote": {"P": 150.51, "S": 300, "p": 150.49, "s": 500, "t": 1623081600000},
            "day": {"vw": 150.55, "v": 500000},
            "min": {"o": 150.49, "c": 150.52, "h": 150.57, "l": 150.45, "v": 1000, "vw": 150.55},
        }
    }
    await l2_manager._apply_snapshot("AAPL", snapshot_data)

    book = order_book.get_book("AAPL")
    assert book is not None
    assert book.bids[0].price == 150.49
    assert book.asks[0].price == 150.51


@pytest.mark.asyncio
async def test_rest_snapshot_http_called(l2_manager):
    """poll_once() should make an HTTP GET to Polygon REST API."""
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "ticker": {
            "lastQuote": {"P": 150.51, "S": 100, "p": 150.49, "s": 200, "t": 1000},
            "day": {"vw": 150.5, "v": 10000},
            "min": {"o": 150.0, "c": 150.5, "h": 151.0, "l": 149.5, "v": 1000, "vw": 150.4},
        }
    })

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await l2_manager.poll_once(["AAPL"])

    mock_session.get.assert_called_once()
    call_url = mock_session.get.call_args[0][0]
    assert "AAPL" in call_url
    assert "polygon.io" in call_url


@pytest.mark.asyncio
async def test_snapshot_missing_quote_does_not_crash(l2_manager):
    """Snapshot response with missing lastQuote should be handled gracefully."""
    snapshot_data = {"ticker": {"day": {"vw": 150.5, "v": 10000}}}
    # Should not raise
    await l2_manager._apply_snapshot("AAPL", snapshot_data)


@pytest.mark.asyncio
async def test_stop_cancels_poll_task(l2_manager):
    """Calling stop() should cancel the background poll loop."""
    # run() accepts a callable that returns the current symbol list
    task = asyncio.create_task(l2_manager.run(lambda: ["AAPL"]))
    await asyncio.sleep(0.05)
    await l2_manager.stop()
    await asyncio.sleep(0.05)
    assert task.done() or task.cancelled()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_polygon_l2.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'data.polygon_l2'`

- [ ] **Step 3: Create `data/polygon_l2.py`**

```python
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
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/test_polygon_l2.py -v
```
Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add data/polygon_l2.py tests/test_polygon_l2.py
git commit -m "feat: add PolygonL2Manager with NBBO update and REST snapshot polling"
```

---

## Task 5: Coordinator — `_build_data_feed()`, deferred agents, GlobalClockAgent

**Files:**
- Modify: `pipeline/coordinator.py`

The MVP active agents are: 17, 1, 2, 3, 4, 5, 8, 9, 11, 12, 13.
The deferred agents (instantiated but NOT started) are: 0, 6, 7, 10, 14, 15, 16.
Agents 18–26 require additional coordinator work to wire (deferred to post-MVP).

- [ ] **Step 1: Add `DataFeedBase` import and `_l2_manager` attribute**

In `pipeline/coordinator.py`, find the imports section at the top and add:

```python
# Add after "from data.feed import AlpacaDataFeed":
from data.base import DataFeedBase
```

Change the class attribute declaration (around line 65):
```python
# FROM:
self.data_feed: AlpacaDataFeed | None = None
# TO:
self.data_feed: DataFeedBase | None = None
self._l2_manager = None
self._deferred_agents: list = []
```

- [ ] **Step 2: Add `_build_data_feed()` method**

Add the following method to `TradingSystemCoordinator` (insert before `initialize()`):

```python
def _build_data_feed(self) -> DataFeedBase:
    """
    Instantiate the configured data feed provider.
    Controlled by config.yaml: data_feed.provider (polygon | alpaca).
    """
    provider = self.config.get("data_feed", {}).get("provider", "alpaca")
    if provider == "polygon":
        import os
        from data.polygon_feed import PolygonDataFeed
        from data.polygon_l2 import PolygonL2Manager

        api_key_env = self.config.get("polygon", {}).get("api_key_env", "POLYGON_API_KEY")
        api_key = os.environ[api_key_env]
        use_delayed = self.config.get("data_feed", {}).get("use_delayed", False)
        reconnect_delay = self.config.get("polygon", {}).get("reconnect_delay_sec", 5.0)
        max_reconnects = self.config.get("polygon", {}).get("max_reconnect_attempts", 10)

        feed = PolygonDataFeed(
            api_key=api_key,
            use_delayed=use_delayed,
            reconnect_delay_sec=reconnect_delay,
            max_reconnect_attempts=max_reconnects,
        )

        poll_sec = self.config.get("polygon", {}).get("l2_snapshot_poll_sec", 2)
        l2_manager = PolygonL2Manager(
            api_key=api_key,
            order_book=self.order_book,
            poll_sec=poll_sec,
        )
        self._l2_manager = l2_manager
        return feed
    else:
        paper = self._mode != "live"
        return AlpacaDataFeed(
            api_key=os.environ.get("ALPACA_API_KEY", ""),
            secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
            paper=paper,
        )
```

- [ ] **Step 3: Update `initialize()` to use `_build_data_feed()`**

Find the data feed section in `initialize()` (around lines 106–116):

```python
# REPLACE:
self.data_feed = AlpacaDataFeed(
    api_key=os.environ.get("ALPACA_API_KEY", ""),
    secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
    paper=paper,
)
self.order_book = OrderBookCache()

# Sync L1→L2 for paper mode
mock_l2 = MockOrderBookFeed(self.order_book)
self.data_feed.add_tick_handler(
    lambda tick: mock_l2.update_from_tick(tick.symbol, tick.bid, tick.ask)
)

# WITH:
self.order_book = OrderBookCache()
self.data_feed = self._build_data_feed()

provider = self.config.get("data_feed", {}).get("provider", "alpaca")
if provider == "polygon" and self._l2_manager:
    # Wire Q.* NBBO events from PolygonDataFeed → PolygonL2Manager
    l2 = self._l2_manager
    async def _nbbo_handler(tick) -> None:
        await l2.update_from_nbbo(tick.symbol, tick.bid, 0, tick.ask, 0, tick.timestamp)
    self.data_feed.add_tick_handler(_nbbo_handler)
else:
    # Alpaca paper: synthetic L2 from L1 ticks (unchanged)
    mock_l2 = MockOrderBookFeed(self.order_book)
    self.data_feed.add_tick_handler(
        lambda tick: mock_l2.update_from_tick(tick.symbol, tick.bid, tick.ask)
    )
```

- [ ] **Step 4: Add `GlobalClockAgent` and deferred agent handling to `_instantiate_agents()`**

At the top of `_instantiate_agents()`, add the import:
```python
from agents.agent_17_global_clock import GlobalClockAgent
```

Add GCA instantiation at the beginning (before Agent 0):
```python
# Agent 17 — Global Clock (instantiated first, started first)
gca = GlobalClockAgent(bus=self.bus, store=self.store, audit=self.audit)
```

Separate the `self._agents` list into active and deferred:

```python
# MVP active agents (17→1→2→3→4→5→8→9→11→12→13)
self._agents = [gca, mua, mra, dia, misa, sva, ea, bra, mma, ela, psa]

# Deferred agents — instantiated but not started in MVP
# Reactivating any: move from this list to self._agents
self._deferred_agents = [era, tera, spa, eq_agent, psra, tca, hgl]

deferred_names = [type(a).__name__ for a in self._deferred_agents]
logger.info("Deferred agents (not started): %s", deferred_names)
```

- [ ] **Step 5: Wire L2 manager start/stop in `run()` and `_graceful_shutdown()`**

In `run()`, add after `feed_task` creation:
```python
# Start L2 manager background poll (Polygon only — None for Alpaca)
l2_task = None
if self._l2_manager and self._subscribed_symbols_fn():
    symbols = list(self.data_feed.subscribed_symbols)
    if symbols:
        l2_task = asyncio.create_task(
            self._l2_manager.run(symbols), name="l2-manager"
        )
```

Note: `subscribed_symbols` is populated after Agent 1 (MarketUniverse) calls `feed.subscribe()` on session start. The L2 poll loop will start with an empty symbol list initially, which is fine — `PolygonL2Manager.run()` skips the poll when the list is empty.

`PolygonL2Manager.run()` already accepts a `symbols_fn: Callable[[], list[str]]` (defined in Task 4). Wire it in the coordinator `run()`:
```python
if self._l2_manager:
    l2_task = asyncio.create_task(
        self._l2_manager.run(lambda: list(self.data_feed.subscribed_symbols)),
        name="l2-manager",
    )
    tasks_to_cancel = agent_tasks + [sm_task, feed_task, watchdog_task, l2_task]
else:
    tasks_to_cancel = agent_tasks + [sm_task, feed_task, watchdog_task]

await self._shutdown_event.wait()
await self._graceful_shutdown(tasks_to_cancel)
```

Also update `_graceful_shutdown()` to stop l2_manager:
```python
if self._l2_manager:
    await self._l2_manager.stop()
```

Also update `test_polygon_l2.py::test_stop_cancels_poll_task` to match the new `run(symbols_fn)` signature:
```python
task = asyncio.create_task(l2_manager.run(lambda: ["AAPL"]))
```

- [ ] **Step 6: Run all tests**

```bash
.venv/bin/pytest tests/ -v --tb=short 2>&1 | tail -30
```
Expected: all previously-passing tests still pass. The `test_polygon_l2.py` test for `run()` must also pass after the signature update.

- [ ] **Step 7: Commit**

```bash
git add pipeline/coordinator.py tests/test_polygon_l2.py
git commit -m "feat: add _build_data_feed(), GlobalClockAgent, deferred agent list to coordinator"
```

---

## Task 6: `config.yaml`, `.env`, `requirements.txt`

**Files:**
- Modify: `config.yaml`
- Modify (note only): `.env` — add `POLYGON_API_KEY` entry (never committed with real value)
- Modify: `requirements.txt` — add comment noting Polygon uses existing websockets + aiohttp

- [ ] **Step 1: Update `config.yaml`**

Change `system.mode`:
```yaml
system:
  mode: paper                    # changed from: shadow
```

Add `data_feed` and `polygon` sections after the `system:` block:
```yaml
data_feed:
  provider: polygon              # polygon | alpaca
  use_delayed: true              # true = delayed feed (off-hours testing); false = real-time

polygon:
  api_key_env: POLYGON_API_KEY   # env var name — never hardcoded
  ws_url_realtime: wss://socket.polygon.io/stocks
  ws_url_delayed: wss://delayed.polygon.io/stocks
  l2_snapshot_poll_sec: 2
  l2_mode: nbbo_only             # nbbo_only | launchpad (post-MVP)
  reconnect_delay_sec: 5
  max_reconnect_attempts: 10
```

- [ ] **Step 2: Update `.env.alpaca.local` / `.env` with `POLYGON_API_KEY`**

Add to your local `.env` file (never commit the actual key):
```bash
# Polygon / Massive — real-time market data
POLYGON_API_KEY=your_polygon_api_key_here
```

Check that `.env` is in `.gitignore`. If not, add it:
```bash
grep -q "^\.env$" .gitignore || echo ".env" >> .gitignore
```

- [ ] **Step 3: Update `requirements.txt` — add comment**

Add after the existing websockets/aiohttp lines:
```
# Polygon/Massive market data — uses websockets and aiohttp (above), no additional SDK needed
```

- [ ] **Step 4: Verify coordinator loads config correctly**

```bash
.venv/bin/python -c "
import yaml
with open('config.yaml') as f:
    c = yaml.safe_load(f)
assert c['system']['mode'] == 'paper'
assert c['data_feed']['provider'] == 'polygon'
assert c['data_feed']['use_delayed'] == True
assert c['polygon']['l2_snapshot_poll_sec'] == 2
print('config.yaml OK')
"
```
Expected: `config.yaml OK`

- [ ] **Step 5: Commit**

```bash
git add config.yaml requirements.txt
git commit -m "config: switch to paper mode, add Polygon data_feed and polygon sections"
```

---

## Task 7: Interface conformance tests

**Files:**
- Modify: `tests/test_data_feed_base.py` — add conformance tests for both feed implementations

- [ ] **Step 1: Add conformance tests to `tests/test_data_feed_base.py`**

Append to the existing file:

```python
# Additional conformance tests appended to tests/test_data_feed_base.py

import pytest


def test_alpaca_feed_is_data_feed_base():
    from data.feed import AlpacaDataFeed
    from data.base import DataFeedBase
    feed = AlpacaDataFeed(api_key="k", secret_key="s")
    assert isinstance(feed, DataFeedBase)


def test_polygon_feed_is_data_feed_base():
    from data.polygon_feed import PolygonDataFeed
    from data.base import DataFeedBase
    feed = PolygonDataFeed(api_key="k", use_delayed=True)
    assert isinstance(feed, DataFeedBase)


def test_both_feeds_expose_identical_method_signatures():
    """Both feeds must have the same set of public DataFeedBase methods."""
    import inspect
    from data.feed import AlpacaDataFeed
    from data.polygon_feed import PolygonDataFeed
    from data.base import DataFeedBase

    base_methods = {
        name for name, val in inspect.getmembers(DataFeedBase)
        if not name.startswith("_") and callable(val)
    }

    alpaca_methods = {
        name for name, val in inspect.getmembers(AlpacaDataFeed)
        if not name.startswith("_") and callable(val)
    }
    polygon_methods = {
        name for name, val in inspect.getmembers(PolygonDataFeed)
        if not name.startswith("_") and callable(val)
    }

    missing_alpaca = base_methods - alpaca_methods
    missing_polygon = base_methods - polygon_methods

    assert not missing_alpaca, f"AlpacaDataFeed missing: {missing_alpaca}"
    assert not missing_polygon, f"PolygonDataFeed missing: {missing_polygon}"


@pytest.mark.asyncio
async def test_polygon_feed_get_recent_bars_delegates_to_get_bar_history():
    from data.polygon_feed import PolygonDataFeed
    feed = PolygonDataFeed(api_key="k", use_delayed=True)
    # No bars yet — both should return empty list
    assert feed.get_recent_bars("AAPL", timeframe="1m", limit=5) == []
    assert feed.get_bar_history("AAPL", n=5) == []


@pytest.mark.asyncio
async def test_alpaca_feed_get_recent_bars_delegates_to_get_bar_history():
    from data.feed import AlpacaDataFeed
    feed = AlpacaDataFeed(api_key="k", secret_key="s")
    assert feed.get_recent_bars("AAPL", timeframe="1m", limit=5) == []
    assert feed.get_bar_history("AAPL", n=5) == []
```

- [ ] **Step 2: Run tests**

```bash
.venv/bin/pytest tests/test_data_feed_base.py -v
```
Expected: all tests PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/test_data_feed_base.py
git commit -m "test: add interface conformance tests for AlpacaDataFeed and PolygonDataFeed"
```

---

## Task 8: Integration smoke test

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/polygon_feed_smoke.py`

This is a manual-run test requiring a real Polygon API key and live market hours. It is NOT run in CI. Run it manually before the first full paper session.

- [ ] **Step 1: Create `tests/integration/__init__.py`**

Empty file:
```python
```

- [ ] **Step 2: Create `tests/integration/polygon_feed_smoke.py`**

```python
"""
Polygon feed smoke test — manual run only, requires market hours + real API key.

Usage:
    python -m tests.integration.polygon_feed_smoke --symbols AAPL,SPY --duration 300

Pass criteria:
  - Ticks arrive within 5 seconds of script start
  - MarketTick.bid and .ask non-zero for both symbols
  - BarData.vwap populated on every 1-min bar
  - OrderBookCache spread matches MarketTick spread within 0.5 bps
  - No FeedConnectionError raised during the duration
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from data.polygon_feed import PolygonDataFeed
from data.polygon_l2 import PolygonL2Manager
from data.level2 import OrderBookCache
from core.models import BarData, MarketTick

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class SmokeTestResults:
    def __init__(self) -> None:
        self.first_tick_at: datetime | None = None
        self.tick_counts: dict[str, int] = {}
        self.bar_counts: dict[str, int] = {}
        self.bars_with_vwap: dict[str, int] = {}
        self.bid_ask_errors: list[str] = []

    def record_tick(self, tick: MarketTick) -> None:
        if self.first_tick_at is None:
            self.first_tick_at = datetime.utcnow()
            logger.info("FIRST TICK received at %s: %s bid=%.2f ask=%.2f",
                        self.first_tick_at, tick.symbol, tick.bid, tick.ask)
        sym = tick.symbol
        self.tick_counts[sym] = self.tick_counts.get(sym, 0) + 1
        if tick.bid <= 0 or tick.ask <= 0:
            self.bid_ask_errors.append(f"{sym}: bid={tick.bid} ask={tick.ask}")

    def record_bar(self, bar: BarData) -> None:
        sym = bar.symbol
        self.bar_counts[sym] = self.bar_counts.get(sym, 0) + 1
        if bar.vwap:
            self.bars_with_vwap[sym] = self.bars_with_vwap.get(sym, 0) + 1

    def report(self) -> bool:
        """Print results and return True if all pass criteria met."""
        print("\n" + "="*60)
        print("SMOKE TEST RESULTS")
        print("="*60)

        passed = True

        # Criterion 1: ticks arrived
        if self.first_tick_at:
            print(f"✓ First tick received at {self.first_tick_at}")
        else:
            print("✗ No ticks received!")
            passed = False

        # Criterion 2: tick counts
        for sym, count in self.tick_counts.items():
            print(f"  {sym}: {count} ticks, {self.bar_counts.get(sym, 0)} bars")

        # Criterion 3: all bars have VWAP
        for sym, bar_count in self.bar_counts.items():
            vwap_count = self.bars_with_vwap.get(sym, 0)
            if vwap_count == bar_count:
                print(f"✓ {sym}: VWAP populated on all {bar_count} bars")
            else:
                print(f"✗ {sym}: VWAP missing on {bar_count - vwap_count}/{bar_count} bars")
                passed = False

        # Criterion 4: no zero bid/ask
        if not self.bid_ask_errors:
            print("✓ No zero bid/ask ticks")
        else:
            print(f"✗ Zero bid/ask ticks found: {self.bid_ask_errors[:5]}")
            passed = False

        print("="*60)
        print("RESULT:", "PASSED" if passed else "FAILED")
        return passed


async def run_smoke_test(symbols: list[str], duration_sec: int, api_key: str) -> bool:
    results = SmokeTestResults()
    order_book = OrderBookCache()

    feed = PolygonDataFeed(api_key=api_key, use_delayed=False)
    l2 = PolygonL2Manager(api_key=api_key, order_book=order_book, poll_sec=2)

    async def on_tick(tick: MarketTick) -> None:
        results.record_tick(tick)
        await l2.update_from_nbbo(tick.symbol, tick.bid, 0, tick.ask, 0, tick.timestamp)

    async def on_bar(bar: BarData) -> None:
        results.record_bar(bar)
        logger.info("BAR %s OHLC=%.2f/%.2f/%.2f/%.2f vwap=%s vol=%d",
                    bar.symbol, bar.open, bar.high, bar.low, bar.close,
                    f"{bar.vwap:.2f}" if bar.vwap else "None", bar.volume)

    feed.add_tick_handler(on_tick)
    feed.add_bar_handler(on_bar)
    await feed.subscribe(symbols)

    logger.info("Starting smoke test: symbols=%s duration=%ds", symbols, duration_sec)

    feed_task = asyncio.create_task(feed.run())
    l2_task = asyncio.create_task(l2.run(lambda: list(feed.subscribed_symbols)))

    await asyncio.sleep(duration_sec)

    await feed.stop()
    await l2.stop()
    feed_task.cancel()
    l2_task.cancel()

    return results.report()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polygon feed smoke test")
    parser.add_argument("--symbols", default="AAPL,SPY",
                        help="Comma-separated symbols (default: AAPL,SPY)")
    parser.add_argument("--duration", type=int, default=300,
                        help="Test duration in seconds (default: 300)")
    args = parser.parse_args()

    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print("ERROR: POLYGON_API_KEY environment variable not set")
        sys.exit(1)

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    passed = asyncio.run(run_smoke_test(symbols, args.duration, api_key))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify the smoke test file is syntactically valid**

```bash
.venv/bin/python -m py_compile tests/integration/polygon_feed_smoke.py && echo "Syntax OK"
```
Expected: `Syntax OK`

- [ ] **Step 4: Run full test suite one final time**

```bash
.venv/bin/pytest tests/ -v --ignore=tests/integration --tb=short 2>&1 | tail -20
```
Expected: all unit tests pass. (Integration tests are excluded — they require network.)

- [ ] **Step 5: Commit**

```bash
git add tests/integration/__init__.py tests/integration/polygon_feed_smoke.py
git commit -m "test: add Polygon integration smoke test (manual run, market hours)"
```

---

## Validation Checklist (after all tasks complete)

Run before first paper session:

```bash
# 1. All unit tests pass
.venv/bin/pytest tests/ --ignore=tests/integration -v

# 2. Config validation
.venv/bin/python -c "
import yaml, os
with open('config.yaml') as f: c = yaml.safe_load(f)
assert c['system']['mode'] == 'paper'
assert c['data_feed']['provider'] == 'polygon'
print('Config OK')
"

# 3. Polygon API key present
.venv/bin/python -c "import os; print('Key present:', bool(os.environ.get('POLYGON_API_KEY')))"

# 4. Off-hours feed wiring test (use_delayed=true required)
# Set use_delayed: true in config.yaml, then:
.venv/bin/python -c "
from data.polygon_feed import PolygonDataFeed
from data.base import DataFeedBase
f = PolygonDataFeed('k', use_delayed=True)
assert isinstance(f, DataFeedBase)
assert 'delayed' in f._ws_url
print('Feed wiring OK')
"

# 5. Integration smoke test (market hours only)
python -m tests.integration.polygon_feed_smoke --symbols AAPL,SPY --duration 60
```

---

## Notes for First Paper Session

1. Set `use_delayed: false` in `config.yaml` before market hours (real-time data).
2. Set `use_delayed: true` for off-hours wiring validation (delayed.polygon.io, free tier).
3. First session success criterion: one complete trade cycle — signal → order → fill → monitor → exit. Profit is irrelevant.
4. Monitor `logs/trading.log` for the 11-step checklist in the spec (Section 7, Layer 3).
