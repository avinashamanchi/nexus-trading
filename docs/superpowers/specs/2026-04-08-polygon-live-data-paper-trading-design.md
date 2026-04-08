# Polygon Live Data + Alpaca Paper Trading Integration
**Date:** 2026-04-08  
**Status:** Approved — ready for implementation planning  
**Goal:** Replace Alpaca IEX (delayed, low-quality) with Polygon/Massive real-time consolidated tape for market data, while keeping Alpaca paper trading for zero-risk order execution. Validates the full architecture: `Polygon → Market Data → Signals → Alpaca PAPER → Orders`.

---

## 1. Architecture Overview

```
Polygon WebSocket (wss://socket.polygon.io/stocks)
  │
  ├── T.* trades  ─────────────────────┐
  ├── Q.* quotes  ─────────────────────┼──► PolygonDataFeed ──► MarketTick / BarData
  ├── A.* 1-min bars ──────────────────┘         │
  └── AM.* 1-sec bars (VWAP accumulation)         │
                                                  │
Polygon REST  (/v2/snapshot/...tickers/{sym})      │
  └── Polled every 2s ──────────────────────────► PolygonL2Manager ──► OrderBookCache
                                                            │
                                                            ▼
                                        ┌─────────────────────────────────┐
                                        │         MVP Agent Chain          │
                                        │  17→1→2→3→4→5→8→9→11→12→13     │
                                        └─────────────────────────────────┘
                                                            │
                                                            ▼
                                              AlpacaBroker(paper=True)
                                              Alpaca Paper Trading API
```

**Core principle:** No agent changes. All agents already consume `MarketTick` and `BarData` from internal models. Polygon slots in at the feed layer below all of them.

---

## 2. MVP Agent Chain

11 agents active for the initial paper trading validation run.

| Agent | Name | Role |
|---|---|---|
| 17 | GlobalClock | Market hours gate — nothing runs without it |
| 1 | MarketUniverse | Builds daily tradeable symbol list; feed subscribes to this list |
| 2 | MarketRegime | VIX-based regime classification; gates all signal generation |
| 3 | DataIntegrity | Feed quality gate; MiSA blocked until `FeedStatus.CLEAN` |
| 4 | MicroSignal | Candidate signal detection (BREAKOUT, VWAP_TAP, LIQUIDITY_SWEEP, MOMENTUM_BURST) |
| 5 | SignalValidation | Approves/rejects candidate signals; no orders without this |
| 8 | Execution | Submits bracket orders to Alpaca paper |
| 9 | BrokerReconciliation | Confirms fills; state consistency |
| 11 | MicroMonitoring | Tick-by-tick position watching; exit triggers |
| 12 | ExitLockIn | Closes positions via 6 named exit modes |
| 13 | PortfolioSupervisor | Daily loss cap, drawdown velocity, circuit breaker |

### Deferred Agents (16) — wired but no-op in MVP

Agents 0, 6, 7, 10, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26.

Each is instantiated at coordinator startup but added to a `_deferred_agents` list rather than started. Reactivating any one is a single-line coordinator change. A log line at startup lists all deferred agents so nothing is silently missing.

---

## 3. Data Layer Design

### 3.1 New file: `data/base.py`

Abstract contract shared by both feed implementations. Mirrors the existing `brokers/base.py` pattern.

```python
class DataFeedBase(ABC):
    def add_tick_handler(self, handler: TickHandler) -> None
    def add_bar_handler(self, handler: BarHandler) -> None
    async def subscribe(self, symbols: list[str]) -> None
    async def unsubscribe(self, symbols: list[str]) -> None
    def get_latest_tick(self, symbol: str) -> MarketTick | None
    def get_tick_history(self, symbol: str, n: int) -> list[MarketTick]
    def get_bar_history(self, symbol: str, n: int) -> list[BarData]
    async def run(self) -> None
    async def stop(self) -> None
    @property is_running: bool
    @property subscribed_symbols: set[str]
```

### 3.2 Modified file: `data/feed.py`

`AlpacaDataFeed` gains `(DataFeedBase)` in its class signature. Zero logic changes.

### 3.3 New file: `data/polygon_feed.py` — `PolygonDataFeed(DataFeedBase)`

Connects to Polygon/Massive WebSocket and maps four channels to existing internal models:

| Polygon channel | Internal model field |
|---|---|
| `T.*` trade | `MarketTick.last`, `.volume`, `.sequence` |
| `Q.*` quote | `MarketTick.bid`, `.ask` |
| `A.*` 1-min aggregate | `BarData.open/high/low/close/volume/vwap` |
| `AM.*` 1-sec aggregate | Running intraday VWAP accumulation (sum/volume) |

Trades and quotes arrive on separate messages. The feed merges them per symbol using a held-state cache — last known values are overlaid as each message arrives. Same pattern as `AlpacaDataFeed`.

Reconnect: exponential backoff up to `max_reconnect_attempts`, then raises `FeedConnectionError`. Agent 3 detects this via `FeedStatus.DIRTY` and halts signal generation.

WebSocket URL is config-driven:
- `use_delayed: false` → `wss://socket.polygon.io/stocks` (real-time, requires paid plan)
- `use_delayed: true` → `wss://delayed.polygon.io/stocks` (15-min delayed, free tier — for off-hours testing)

### 3.4 New file: `data/polygon_l2.py` — `PolygonL2Manager`

Feeds `OrderBookCache` from two sources without changing the `OrderBookCache` interface:

**Source 1 — Quote stream (real-time NBBO, ~1ms latency):**  
Every `Q.*` message is pushed into `OrderBookCache` as level `[0]`. Gives Agent 3 and Agent 4 a live spread immediately.

**Source 2 — REST snapshot poll (depth enrichment, every 2 seconds):**  
`GET /v2/snapshot/locale/us/markets/stocks/tickers/{symbol}` returns `lastQuote`, `day.vwap`, `min` (latest 1-min bar), `prevDay.close`. Enriches `OrderBookCache` with additional depth levels when available and keeps session VWAP current for Agent 4's VWAP_TAP detection.

**Post-MVP note:** Polygon's Launchpad tier provides `LU.*` (Level 2 book updates) via WebSocket — real-time full 10-level depth, replacing the 2-second REST poll entirely. Controlled by `l2_mode: nbbo_only | launchpad` in config.

---

## 4. Configuration Changes

### `.env` additions

```bash
# Polygon / Massive — market data
POLYGON_API_KEY=your_key_here

# Alpaca paper trading (existing)
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true
```

### `config.yaml` additions

```yaml
system:
  mode: paper                      # changed from: shadow

data_feed:
  provider: polygon                # polygon | alpaca
  use_delayed: false               # true = delayed feed for off-hours testing

polygon:
  api_key_env: POLYGON_API_KEY     # env var name — never hardcoded
  ws_url_realtime: wss://socket.polygon.io/stocks
  ws_url_delayed: wss://delayed.polygon.io/stocks
  l2_snapshot_poll_sec: 2
  l2_mode: nbbo_only               # nbbo_only | launchpad (post-MVP)
  reconnect_delay_sec: 5
  max_reconnect_attempts: 10
```

`use_delayed: true` lets you validate the full pipeline wiring outside market hours before the first live paper session.

---

## 5. Coordinator Changes

One new private method `_build_data_feed()` in `pipeline/coordinator.py` replaces inline `AlpacaDataFeed` construction. No other coordinator logic changes.

```python
def _build_data_feed(self) -> DataFeedBase:
    provider = self.config.get("data_feed", {}).get("provider", "alpaca")
    if provider == "polygon":
        from data.polygon_feed import PolygonDataFeed
        from data.polygon_l2 import PolygonL2Manager
        api_key = os.environ[self.config["polygon"]["api_key_env"]]
        use_delayed = self.config["data_feed"].get("use_delayed", False)
        feed = PolygonDataFeed(api_key=api_key, use_delayed=use_delayed, ...)
        self._l2_manager = PolygonL2Manager(
            api_key=api_key,
            order_book=self._order_book,
            poll_sec=self.config["polygon"]["l2_snapshot_poll_sec"],
        )
        return feed
    else:
        return AlpacaDataFeed(api_key=..., secret_key=..., paper=True)
```

The `data_feed` variable flowing into Agent 3, Agent 4, etc. is typed as `DataFeedBase`. Agents receive the same object they always have — just sourced from Polygon.

---

## 6. New Files Summary

| File | Type | Purpose |
|---|---|---|
| `data/base.py` | New | `DataFeedBase` abstract class |
| `data/polygon_feed.py` | New | `PolygonDataFeed(DataFeedBase)` — L1 ticks + bars |
| `data/polygon_l2.py` | New | `PolygonL2Manager` — NBBO + REST snapshot → `OrderBookCache` |
| `tests/test_polygon_feed.py` | New | Unit tests with recorded WebSocket fixture messages |
| `tests/test_polygon_l2.py` | New | Unit tests for L2 NBBO and snapshot enrichment |
| `tests/test_data_feed_base.py` | New | Interface conformance tests for both feed implementations |
| `tests/integration/polygon_feed_smoke.py` | New | 5-min live feed smoke test (market hours) |

## Modified Files

| File | Change |
|---|---|
| `data/feed.py` | `AlpacaDataFeed` inherits `DataFeedBase` — signature only |
| `pipeline/coordinator.py` | Add `_build_data_feed()`, deferred agent list |
| `config.yaml` | Add `data_feed`, `polygon` sections; `system.mode: paper` |
| `.env` | Add `POLYGON_API_KEY` |
| `requirements.txt` | Add `polygon-api-client>=1.12` (Massive SDK) |

---

## 7. Testing Strategy

### Layer 1 — Unit tests (no network, fixture-driven)

**`tests/test_polygon_feed.py`**
- `T.*` trade message → correct `MarketTick.last` and `.volume`
- `Q.*` quote message → correct `MarketTick.bid` / `.ask` and spread
- `A.*` bar → correct `BarData.vwap`, OHLCV
- `AM.*` 1-sec bars → intraday VWAP accumulates correctly across multiple messages
- Reconnect: feed reconnects after simulated disconnect within `reconnect_delay_sec`
- Auth failure: raises `FeedConnectionError` on bad API key response

**`tests/test_polygon_l2.py`**
- NBBO quote → `OrderBookCache` updated at level `[0]`
- REST snapshot mock → `OrderBookCache` enriched with depth levels
- `use_delayed=true` → WebSocket URL resolves to `delayed.polygon.io`

**`tests/test_data_feed_base.py`**
- Both `AlpacaDataFeed` and `PolygonDataFeed` satisfy `isinstance(feed, DataFeedBase)`
- Both expose identical method signatures

### Layer 2 — Feed integration smoke test (market hours, 2 symbols)

Run manually before first full paper session. Connects to Polygon with real credentials, subscribes `AAPL` and `SPY` for 5 minutes.

**Pass criteria:**
- Ticks arrive within 5 seconds of market open
- `MarketTick.bid` and `.ask` non-zero, within 1% of each other for SPY
- `BarData.vwap` populated on every 1-min bar
- `OrderBookCache` spread matches `MarketTick` spread within 0.5 bps
- Agent 3 declares `FeedStatus.CLEAN` within 30 seconds of first tick

```bash
python -m tests.integration.polygon_feed_smoke --symbols AAPL,SPY --duration 300
```

### Layer 3 — Full paper session (market hours)

```bash
python main.py --mode paper
```

**One-trade validation checklist:**

| Check | Expected | Where |
|---|---|---|
| Polygon feed connects | `DataFeed starting for N symbols` logged | `logs/trading.log` |
| Agent 3 clears | `FeedStatus.CLEAN` within 30s | log + audit |
| Agent 2 classifies regime | `TREND_DAY` or `RANGE_DAY` | log |
| Agent 1 publishes universe | 5–12 symbols approved | log |
| Agent 4 emits signal | `CANDIDATE_SIGNAL` on bus | log |
| Agent 5 approves signal | `APPROVED_SIGNAL` on bus | log |
| Agent 8 submits order | Bracket order in Alpaca paper dashboard | Alpaca UI |
| Agent 9 reconciles fill | `ReconciliationStatus.CLEAN` after fill | log |
| Agent 11 monitors | Position tick-watch loop starts | log |
| Agent 12 exits | Exit triggered within 5-min time stop | log + Alpaca UI |
| Agent 13 holds | No halt triggered on normal session | log |

**First session goal:** One complete trade cycle — signal → order → fill → monitor → exit. Profit is irrelevant. Proving the full pipe is wired correctly is the only success criterion.

---

## 8. Post-MVP Integration Path (Deferred Agents)

Once the 11-agent MVP is validated over multiple paper sessions:

| Priority | Agent | What it adds |
|---|---|---|
| High | Agent 10 — ExecutionQuality | Slippage tracking, rejection rate monitoring |
| High | Agent 14 — PostSessionReview | Daily P&L and expectancy analysis |
| High | Agent 19 — SystemHealth | Automated anomaly detection and restart |
| Medium | Agent 16 — HumanGovernance | Parameter change approval workflow |
| Medium | Agent 0 — EdgeResearch | LLM-driven setup discovery |
| Medium | Agent 6 — TERA | Regime-aware execution adjustment |
| Medium | Agent 7 — SPA | (to be investigated) |
| Low | Agent 18 — ShadowReplay | SMRE deterministic replay validation |
| Low | Agent 15 — TaxCompliance | Wash sale tracking |
| Low | Agent 23 — CATCompliance | SEC CAT reporting |
| Future | Agents 20, 21, 22, 24, 25, 26 | Market making, stat arb, alt data, dark pool, enterprise risk |

**L2 upgrade path:** When Polygon Launchpad tier is enabled, set `l2_mode: launchpad` in config. `PolygonL2Manager` activates the `LU.*` WebSocket channel and stops the REST poll. `OrderBookCache` interface is unchanged — agents see no difference.

---

## 9. Dependencies

```
# requirements.txt addition
polygon-api-client>=1.12        # Massive/Polygon Python SDK
```

Or use raw `websockets` + `aiohttp` (already in requirements) directly against the Polygon WebSocket and REST API — avoids an additional SDK dependency. Decision deferred to implementation.
