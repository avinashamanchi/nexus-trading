"""
Tests for:
  - BypassFrame  (64-byte ctypes structure)
  - KernelBypassFeed  (mmap ring buffer for HFT feed simulation)
  - KernelBypassAdapter  (replaces WebSocket loop)
"""
from __future__ import annotations

import asyncio
import ctypes
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from infrastructure.kernel_bypass import (
    FRAME_SIZE,
    MAGIC,
    BypassFrame,
    KernelBypassAdapter,
    KernelBypassFeed,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _tick(
    symbol: str = "AAPL",
    bid: float = 150.0,
    ask: float = 150.05,
    last: float = 150.02,
    volume: int = 1000,
    ts_ns: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        bid=bid,
        ask=ask,
        last=last,
        volume=volume,
        timestamp_ns=ts_ns if ts_ns is not None else time.perf_counter_ns(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  TestBypassFrame
# ═══════════════════════════════════════════════════════════════════════════════

class TestBypassFrame:

    def test_frame_is_64_bytes(self):
        assert ctypes.sizeof(BypassFrame) == FRAME_SIZE
        assert FRAME_SIZE == 64

    def test_frame_fields_exist(self):
        f = BypassFrame()
        f.magic = MAGIC
        f.sequence = 42
        f.timestamp_ns = 1_000_000
        f.bid = 150.0
        f.ask = 150.05
        f.last = 150.02
        f.volume = 500
        assert f.magic == MAGIC
        assert f.sequence == 42
        assert f.bid == 150.0

    def test_magic_constant(self):
        assert MAGIC == 0xDEADBEEF

    def test_frame_bytes_roundtrip(self):
        f = BypassFrame()
        f.magic = MAGIC
        f.sequence = 7
        f.bid = 99.99
        f.ask = 100.01
        raw = bytes(f)
        assert len(raw) == FRAME_SIZE
        f2 = BypassFrame.from_buffer_copy(raw)
        assert f2.magic == MAGIC
        assert f2.sequence == 7
        assert abs(f2.bid - 99.99) < 1e-9


# ═══════════════════════════════════════════════════════════════════════════════
#  TestKernelBypassFeed
# ═══════════════════════════════════════════════════════════════════════════════

class TestKernelBypassFeed:

    @pytest.mark.asyncio
    async def test_produce_then_poll_roundtrip(self):
        feed = KernelBypassFeed(simulate=True)
        tick = _tick("AAPL", bid=150.0, ask=150.05, volume=100)
        await feed.produce_tick(tick)
        result = await feed.poll(timeout_ns=5_000_000)
        assert result is not None
        assert result.symbol == "AAPL"
        assert abs(result.bid - 150.0) < 1e-9
        assert abs(result.ask - 150.05) < 1e-9
        assert result.volume == 100
        feed.close()

    @pytest.mark.asyncio
    async def test_multiple_ticks_in_order(self):
        feed = KernelBypassFeed(simulate=True)
        symbols = ["AAPL", "TSLA", "NVDA"]
        for sym in symbols:
            await feed.produce_tick(_tick(sym))
        received = []
        for _ in range(3):
            t = await feed.poll(timeout_ns=5_000_000)
            assert t is not None
            received.append(t.symbol)
        assert received == symbols
        feed.close()

    @pytest.mark.asyncio
    async def test_poll_timeout_returns_none(self):
        feed = KernelBypassFeed(simulate=True)
        # Nothing produced; should timeout
        result = await feed.poll(timeout_ns=1_000)   # 1µs timeout
        assert result is None
        feed.close()

    @pytest.mark.asyncio
    async def test_symbol_preserved(self):
        feed = KernelBypassFeed(simulate=True)
        await feed.produce_tick(_tick("NVDA"))
        t = await feed.poll(timeout_ns=5_000_000)
        assert t is not None
        assert t.symbol == "NVDA"
        feed.close()

    @pytest.mark.asyncio
    async def test_timestamp_ns_preserved(self):
        feed = KernelBypassFeed(simulate=True)
        ts = 1_234_567_890
        await feed.produce_tick(_tick(ts_ns=ts))
        t = await feed.poll(timeout_ns=5_000_000)
        assert t is not None
        assert t.timestamp_ns == ts
        feed.close()

    def test_simulate_mode_no_hardware_required(self):
        """simulate=True must work without any physical NIC."""
        feed = KernelBypassFeed(simulate=True)
        assert feed._simulate is True
        feed.close()

    @pytest.mark.asyncio
    async def test_invalid_magic_not_read(self):
        """If the magic sentinel is wrong, poll should not return that frame."""
        import mmap as mmap_mod
        feed = KernelBypassFeed(simulate=True)
        # Write a frame with wrong magic directly into mmap
        bad = BypassFrame()
        bad.magic = 0xDEADC0DE   # wrong!
        bad.sequence = 0
        raw = bytes(bad)
        feed._mmap.seek(0)
        feed._mmap.write(raw)
        result = await feed.poll(timeout_ns=1_000)
        assert result is None   # bad magic → not consumed
        feed.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  TestKernelBypassAdapter
# ═══════════════════════════════════════════════════════════════════════════════

class TestKernelBypassAdapter:

    @pytest.mark.asyncio
    async def test_adapter_delivers_to_handler(self):
        feed = KernelBypassFeed(simulate=True)
        received = []

        async def handler(tick) -> None:
            received.append(tick)

        await feed.produce_tick(_tick("AAPL"))
        adapter = KernelBypassAdapter(feed, [handler])

        # Run adapter briefly in a background task
        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.05)
        adapter.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(received) >= 1
        assert received[0].symbol == "AAPL"
        feed.close()

    @pytest.mark.asyncio
    async def test_adapter_calls_multiple_handlers(self):
        feed = KernelBypassFeed(simulate=True)
        h1_calls = []
        h2_calls = []

        async def h1(tick) -> None:
            h1_calls.append(tick)

        async def h2(tick) -> None:
            h2_calls.append(tick)

        await feed.produce_tick(_tick("TSLA"))
        adapter = KernelBypassAdapter(feed, [h1, h2])

        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.05)
        adapter.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(h1_calls) >= 1
        assert len(h2_calls) >= 1
        feed.close()

    @pytest.mark.asyncio
    async def test_adapter_stops_cleanly(self):
        feed = KernelBypassFeed(simulate=True)

        async def noop(tick) -> None:
            pass

        adapter = KernelBypassAdapter(feed, [noop])
        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.01)
        adapter.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert not adapter._running
        feed.close()

    @pytest.mark.asyncio
    async def test_adapter_no_ticks_is_safe(self):
        """Adapter with empty feed must not crash."""
        feed = KernelBypassFeed(simulate=True)

        async def noop(tick) -> None:
            pass

        adapter = KernelBypassAdapter(feed, [noop])
        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.02)
        adapter.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        feed.close()
