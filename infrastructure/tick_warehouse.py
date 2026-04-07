"""
infrastructure/tick_warehouse.py — Tick Data Warehouse

Compressed, partitioned time-series store for market tick data.

Features:
  - Per-symbol partitioned storage (one file per symbol per session date)
  - Delta-compressed OHLCV bars (store price diffs, not absolutes)
  - Async write/read interface (non-blocking)
  - OHLCV resampling from raw ticks (1s, 1m, 5m, 15m)
  - Deterministic replay feed (event-ordered, rate-controlled)
  - Session snapshot for SMRE (Shadow Mode Replay Engine)
  - Retention policy (auto-purge sessions older than N days)

Storage layout:
  {base_dir}/{YYYY-MM-DD}/{SYMBOL}.ticks   ← raw ticks (binary delta-compressed)
  {base_dir}/{YYYY-MM-DD}/{SYMBOL}.bars    ← pre-aggregated OHLCV bars (JSONL)
  {base_dir}/{YYYY-MM-DD}/_manifest.json  ← session metadata

All writes are append-only; files are never modified after close.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import AsyncIterator, Iterator


# ─── Data models ───────────────────────────────────────────────────────────────

@dataclass
class Tick:
    """
    A single market tick.

    Attributes:
        symbol:    Ticker symbol.
        ts:        Unix timestamp (float, UTC seconds).
        price:     Last trade price.
        bid:       Best bid price.
        ask:       Best ask price.
        volume:    Cumulative volume at this tick.
        side:      Trade aggressor: 'B' (buy) / 'S' (sell) / '' (unknown).
    """
    symbol: str
    ts: float
    price: float
    bid: float
    ask: float
    volume: int
    side: str = ""

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc)


@dataclass
class OHLCVBar:
    """
    OHLCV candlestick bar.

    Attributes:
        symbol:    Ticker symbol.
        ts:        Bar open timestamp (Unix seconds UTC).
        interval:  Bar interval in seconds (e.g., 60 = 1-minute bar).
        open:      Opening price.
        high:      High price.
        low:       Low price.
        close:     Closing price.
        volume:    Total volume.
        vwap:      Volume-weighted average price.
        tick_count: Number of ticks in the bar.
    """
    symbol: str
    ts: float
    interval: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float = 0.0
    tick_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OHLCVBar":
        return cls(**d)


# ─── Binary encoding for raw ticks ────────────────────────────────────────────

# Format: | ts_delta_ms (uint32) | price_int (int32) | bid_int (int32) |
#         | ask_int (int32) | volume (uint32) | side (char) |
# Prices are stored as int(price * 10000) — 4 decimal places.
# ts_delta_ms is milliseconds since the previous tick (or session start).
_TICK_STRUCT = struct.Struct(">IiiiIc")   # big-endian, 21 bytes per tick
_PRICE_SCALE = 10_000
_SENTINEL_TS = struct.pack(">I", 0xFFFFFFFF)  # start-of-symbol marker


def _encode_tick(tick: Tick, prev_ts: float) -> bytes:
    delta_ms = max(0, round((tick.ts - prev_ts) * 1000))
    delta_ms = min(delta_ms, 0xFFFFFFFE)   # cap at ~49 days
    price_i = round(tick.price * _PRICE_SCALE)
    bid_i = round(tick.bid * _PRICE_SCALE)
    ask_i = round(tick.ask * _PRICE_SCALE)
    side_char = (tick.side[:1] if tick.side else " ")
    side_b = side_char.encode("ascii") if isinstance(side_char, str) else side_char
    return _TICK_STRUCT.pack(delta_ms, price_i, bid_i, ask_i, tick.volume, side_b)


def _decode_tick(data: bytes, symbol: str, prev_ts: float) -> tuple[Tick, float]:
    """Decode one packed tick record; returns (Tick, new_ts)."""
    delta_ms, price_i, bid_i, ask_i, volume, side_b = _TICK_STRUCT.unpack(data)
    ts = prev_ts + delta_ms / 1000.0
    return Tick(
        symbol=symbol,
        ts=ts,
        price=price_i / _PRICE_SCALE,
        bid=bid_i / _PRICE_SCALE,
        ask=ask_i / _PRICE_SCALE,
        volume=volume,
        side=side_b.decode().strip(),
    ), ts


# ─── OHLCV Aggregator ──────────────────────────────────────────────────────────

class OHLCVAggregator:
    """
    Incrementally builds OHLCV bars from a stream of ticks.

    Supports multiple simultaneous intervals (e.g., 1s, 60s, 300s).

    Args:
        symbol:    Symbol being aggregated.
        intervals: List of bar intervals in seconds.
    """

    def __init__(self, symbol: str, intervals: list[int]) -> None:
        self.symbol = symbol
        self.intervals = sorted(intervals)
        # {interval: (bar_start_ts, open, high, low, close, volume, vwap_num, tick_count)}
        self._state: dict[int, list] = {}

    def update(self, tick: Tick) -> list[OHLCVBar]:
        """
        Feed a tick. Returns any completed bars (bars whose interval expired).
        """
        completed: list[OHLCVBar] = []
        for iv in self.intervals:
            bar_ts = tick.ts - (tick.ts % iv)
            if iv not in self._state:
                self._state[iv] = [bar_ts, tick.price, tick.price, tick.price, tick.price,
                                    tick.volume, tick.price * tick.volume, 1]
                continue
            st = self._state[iv]
            if bar_ts > st[0]:
                # Close the old bar
                vwap = st[6] / st[5] if st[5] > 0 else st[4]
                completed.append(OHLCVBar(
                    symbol=self.symbol, ts=st[0], interval=iv,
                    open=st[1], high=st[2], low=st[3], close=st[4],
                    volume=st[5], vwap=round(vwap, 6), tick_count=st[7],
                ))
                self._state[iv] = [bar_ts, tick.price, tick.price, tick.price, tick.price,
                                    tick.volume, tick.price * tick.volume, 1]
            else:
                # Update running bar
                st[2] = max(st[2], tick.price)  # high
                st[3] = min(st[3], tick.price)  # low
                st[4] = tick.price              # close
                st[5] += tick.volume            # volume
                st[6] += tick.price * tick.volume  # vwap numerator
                st[7] += 1                      # tick_count
        return completed

    def flush(self) -> list[OHLCVBar]:
        """Force-close all open bars (call at session end)."""
        out = []
        for iv, st in self._state.items():
            if st[5] > 0:
                vwap = st[6] / st[5] if st[5] > 0 else st[4]
                out.append(OHLCVBar(
                    symbol=self.symbol, ts=st[0], interval=iv,
                    open=st[1], high=st[2], low=st[3], close=st[4],
                    volume=st[5], vwap=round(vwap, 6), tick_count=st[7],
                ))
        self._state.clear()
        return out


# ─── Tick Warehouse ────────────────────────────────────────────────────────────

class TickWarehouse:
    """
    Async tick data warehouse with delta-compressed binary storage.

    Usage::

        wh = TickWarehouse("/data/ticks")
        await wh.open()

        # Write ticks
        await wh.write(tick)

        # Read back (sync generator)
        for t in wh.read("NVDA", "2026-04-07"):
            print(t.price)

        # Resample to 1-minute bars
        bars = wh.resample("NVDA", "2026-04-07", interval=60)

        # Async replay feed at controlled rate
        async for tick in wh.replay("NVDA", "2026-04-07", speed=10.0):
            process(tick)

        await wh.close()

    Args:
        base_dir:      Root directory for all tick data.
        bar_intervals: OHLCV bar intervals to auto-aggregate (seconds).
        retention_days: Auto-purge sessions older than this many days (0 = never).
        write_buffer:  Number of ticks to buffer before flushing to disk.
    """

    DEFAULT_INTERVALS = [1, 60, 300, 900]   # 1s, 1m, 5m, 15m

    def __init__(
        self,
        base_dir: str | Path,
        bar_intervals: list[int] | None = None,
        retention_days: int = 30,
        write_buffer: int = 500,
    ) -> None:
        self._base = Path(base_dir)
        self._intervals = bar_intervals if bar_intervals is not None else self.DEFAULT_INTERVALS
        self._retention_days = retention_days
        self._write_buffer = write_buffer

        # {symbol: (file_handle, last_ts, aggregator, bars_file_handle)}
        self._writers: dict[str, tuple] = {}
        # Per-symbol tick buffer
        self._buffers: dict[str, list[bytes]] = {}
        self._running = False
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the warehouse for writing. Creates today's session directory."""
        self._base.mkdir(parents=True, exist_ok=True)
        self._today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._session_dir = self._base / self._today
        self._session_dir.mkdir(exist_ok=True)
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop(), name="tw_flush")

        if self._retention_days > 0:
            self._purge_old_sessions()

    async def flush(self) -> None:
        """Flush all in-memory write buffers to disk (non-destructive)."""
        async with self._lock:
            await self._flush_all()
            for fh, _, _agg, bfh in self._writers.values():
                fh.flush()
                bfh.flush()

    async def close(self) -> None:
        """Flush all buffers, close files, write session manifest."""
        self._running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

        async with self._lock:
            await self._flush_all()
            for sym, (fh, _, agg, bfh) in self._writers.items():
                # Flush remaining aggregator state
                remaining_bars = agg.flush()
                for bar in remaining_bars:
                    bfh.write(json.dumps(bar.to_dict()) + "\n")
                fh.close()
                bfh.close()
            self._writers.clear()
            self._write_manifest()

    # ── Writing ───────────────────────────────────────────────────────────────

    async def write(self, tick: Tick) -> None:
        """Write a tick asynchronously (buffered)."""
        async with self._lock:
            await self._ensure_writer(tick.symbol)
            fh, last_ts, agg, bfh = self._writers[tick.symbol]
            encoded = _encode_tick(tick, last_ts)
            self._buffers[tick.symbol].append(encoded)
            self._writers[tick.symbol] = (fh, tick.ts, agg, bfh)

            # Write completed bars immediately
            bars = agg.update(tick)
            for bar in bars:
                bfh.write(json.dumps(bar.to_dict()) + "\n")

            # Flush buffer if full
            if len(self._buffers[tick.symbol]) >= self._write_buffer:
                self._flush_symbol(tick.symbol)

    async def write_many(self, ticks: list[Tick]) -> None:
        """Batch-write multiple ticks (more efficient than repeated write())."""
        for tick in ticks:
            await self.write(tick)

    # ── Reading ───────────────────────────────────────────────────────────────

    def read(self, symbol: str, session_date: str | None = None) -> Iterator[Tick]:
        """
        Synchronous generator yielding all ticks for a symbol on the given date.

        Args:
            symbol:       Symbol to read.
            session_date: 'YYYY-MM-DD'. Defaults to today.
        """
        date = session_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._base / date / f"{symbol}.ticks"
        if not path.exists():
            return

        with open(path, "rb") as f:
            # Read 8-byte header: uint64 session_start_ts * 1000 (ms)
            header = f.read(8)
            if len(header) < 8:
                return
            session_start_ms = struct.unpack(">Q", header)[0]
            prev_ts = session_start_ms / 1000.0

            record_size = _TICK_STRUCT.size
            while True:
                data = f.read(record_size)
                if len(data) < record_size:
                    break
                tick, prev_ts = _decode_tick(data, symbol, prev_ts)
                yield tick

    def read_bars(self, symbol: str, interval: int, session_date: str | None = None) -> list[OHLCVBar]:
        """
        Read pre-aggregated OHLCV bars for the given symbol/date/interval.

        Args:
            symbol:       Symbol.
            interval:     Bar interval in seconds (must match a stored interval).
            session_date: 'YYYY-MM-DD'. Defaults to today.
        """
        date = session_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._base / date / f"{symbol}.bars"
        if not path.exists():
            return []
        bars = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d.get("interval") == interval:
                    bars.append(OHLCVBar.from_dict(d))
        return sorted(bars, key=lambda b: b.ts)

    def resample(self, symbol: str, session_date: str | None = None, interval: int = 60) -> list[OHLCVBar]:
        """
        Build OHLCV bars by re-aggregating raw ticks (always accurate, slower).

        Use read_bars() for pre-aggregated bars when available.
        """
        agg = OHLCVAggregator(symbol, [interval])
        for tick in self.read(symbol, session_date):
            agg.update(tick)
        return sorted(agg.flush(), key=lambda b: b.ts)

    # ── Replay ────────────────────────────────────────────────────────────────

    async def replay(
        self,
        symbol: str,
        session_date: str | None = None,
        speed: float = 1.0,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> AsyncIterator[Tick]:
        """
        Async generator that replays ticks at controlled speed.

        Args:
            symbol:       Symbol to replay.
            session_date: 'YYYY-MM-DD'. Defaults to today.
            speed:        Playback speed multiplier (1.0 = real-time, 10.0 = 10×).
            start_ts:     Skip ticks before this Unix timestamp.
            end_ts:       Stop after this Unix timestamp.
        """
        ticks = list(self.read(symbol, session_date))
        if not ticks:
            return

        # Filter by time range
        if start_ts:
            ticks = [t for t in ticks if t.ts >= start_ts]
        if end_ts:
            ticks = [t for t in ticks if t.ts <= end_ts]
        if not ticks:
            return

        prev_real_ts = time.monotonic()
        prev_tick_ts = ticks[0].ts

        for tick in ticks:
            tick_delta = tick.ts - prev_tick_ts
            real_sleep = tick_delta / speed
            if real_sleep > 0.001:
                await asyncio.sleep(real_sleep)
            yield tick
            prev_tick_ts = tick.ts
            prev_real_ts = time.monotonic()

    async def multi_symbol_replay(
        self,
        symbols: list[str],
        session_date: str | None = None,
        speed: float = 1.0,
    ) -> AsyncIterator[Tick]:
        """
        Merge-sorted replay across multiple symbols (time-ordered).

        Yields ticks in chronological order across all symbols.
        """
        # Pre-load all ticks and merge
        all_ticks: list[Tick] = []
        for sym in symbols:
            all_ticks.extend(self.read(sym, session_date))
        all_ticks.sort(key=lambda t: t.ts)

        if not all_ticks:
            return

        prev_tick_ts = all_ticks[0].ts
        for tick in all_ticks:
            delta = (tick.ts - prev_tick_ts) / speed
            if delta > 0.001:
                await asyncio.sleep(delta)
            yield tick
            prev_tick_ts = tick.ts

    # ── Session Discovery ──────────────────────────────────────────────────────

    def available_sessions(self) -> list[str]:
        """Return all session dates with stored data, newest first."""
        if not self._base.exists():
            return []
        sessions = [
            d.name for d in self._base.iterdir()
            if d.is_dir() and d.name[0].isdigit()
        ]
        return sorted(sessions, reverse=True)

    def available_symbols(self, session_date: str | None = None) -> list[str]:
        """Return all symbols stored for the given session date."""
        date = session_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_dir = self._base / date
        if not session_dir.exists():
            return []
        return sorted(
            p.stem for p in session_dir.glob("*.ticks")
        )

    def tick_count(self, symbol: str, session_date: str | None = None) -> int:
        """Return the number of ticks stored for a symbol on the given date."""
        return sum(1 for _ in self.read(symbol, session_date))

    def session_stats(self, session_date: str | None = None) -> dict:
        """Return per-symbol tick counts and file sizes for the session."""
        date = session_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_dir = self._base / date
        manifest_path = session_dir / "_manifest.json"
        if manifest_path.exists():
            with open(manifest_path) as f:
                return json.load(f)
        # Build on-the-fly
        stats = {"date": date, "symbols": {}}
        for path in session_dir.glob("*.ticks"):
            sym = path.stem
            stats["symbols"][sym] = {
                "file_bytes": path.stat().st_size,
                "tick_count": (path.stat().st_size - 8) // _TICK_STRUCT.size,
            }
        return stats

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _ensure_writer(self, symbol: str) -> None:
        if symbol in self._writers:
            return
        tick_path = self._session_dir / f"{symbol}.ticks"
        bars_path = self._session_dir / f"{symbol}.bars"

        # Session start timestamp header (8 bytes)
        session_start_ts = time.time()
        fh = open(tick_path, "ab")
        if tick_path.stat().st_size == 0:
            fh.write(struct.pack(">Q", round(session_start_ts * 1000)))

        bfh = open(bars_path, "a")
        agg = OHLCVAggregator(symbol, self._intervals)
        self._writers[symbol] = (fh, session_start_ts, agg, bfh)
        self._buffers[symbol] = []

    def _flush_symbol(self, symbol: str) -> None:
        buf = self._buffers.get(symbol)
        if not buf:
            return
        fh = self._writers[symbol][0]
        fh.write(b"".join(buf))
        fh.flush()
        self._buffers[symbol] = []

    async def _flush_all(self) -> None:
        for sym in list(self._buffers.keys()):
            self._flush_symbol(sym)

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            async with self._lock:
                await self._flush_all()

    def _write_manifest(self) -> None:
        stats = {"date": self._today, "closed_at": time.time(), "symbols": {}}
        for path in self._session_dir.glob("*.ticks"):
            sym = path.stem
            size = path.stat().st_size
            tick_count = max(0, (size - 8) // _TICK_STRUCT.size)
            stats["symbols"][sym] = {
                "file_bytes": size,
                "tick_count": tick_count,
                "bars_file_bytes": (self._session_dir / f"{sym}.bars").stat().st_size
                    if (self._session_dir / f"{sym}.bars").exists() else 0,
            }
        manifest_path = self._session_dir / "_manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(stats, f, indent=2)

    def _purge_old_sessions(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        for session_dir in self._base.iterdir():
            if session_dir.is_dir() and session_dir.name < cutoff_str:
                import shutil
                shutil.rmtree(session_dir, ignore_errors=True)
