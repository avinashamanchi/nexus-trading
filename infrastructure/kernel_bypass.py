"""
Kernel bypass simulation via mmap + ctypes.
Simulates DPDK/OpenOnload interface for HFT-grade feed ingestion.

BypassFrame: 64-byte fixed structure written to anonymous mmap ring.
KernelBypassFeed: producer writes frames; consumer polls with magic validation.
KernelBypassAdapter: replaces WebSocket loop in AlpacaDataFeed.
"""
from __future__ import annotations
import asyncio
import ctypes
import mmap
import time
from typing import Callable, Awaitable

MAGIC = 0xDEADBEEF
FRAME_SIZE = 64
RING_CAPACITY = 65536  # must be power of 2


class BypassFrame(ctypes.Structure):
    _pack_ = 1   # suppress alignment padding so sizeof == 64 bytes exactly
    _fields_ = [
        ("magic",        ctypes.c_uint32),    # 4 bytes — sentinel
        ("sequence",     ctypes.c_uint64),    # 8 bytes
        ("timestamp_ns", ctypes.c_uint64),    # 8 bytes
        ("symbol_len",   ctypes.c_uint8),     # 1 byte
        ("symbol",       ctypes.c_char * 8),  # 8 bytes
        ("bid",          ctypes.c_double),    # 8 bytes
        ("ask",          ctypes.c_double),    # 8 bytes
        ("last",         ctypes.c_double),    # 8 bytes
        ("volume",       ctypes.c_uint32),    # 4 bytes
        ("_pad",         ctypes.c_uint8 * 7), # 7 bytes padding → total 64 bytes
    ]


assert ctypes.sizeof(BypassFrame) == FRAME_SIZE


TickHandler = Callable  # type alias


class KernelBypassFeed:
    def __init__(self, shm_name: str = "nexus_bypass", simulate: bool = True) -> None:
        self._simulate = simulate
        self._write_seq: int = 0
        self._read_seq: int = 0
        self._mask = RING_CAPACITY - 1
        # Anonymous mmap ring buffer
        self._mmap = mmap.mmap(-1, FRAME_SIZE * RING_CAPACITY)

    def _frame_offset(self, seq: int) -> int:
        return (seq & self._mask) * FRAME_SIZE

    async def produce_tick(self, tick) -> None:
        """Write a MarketTick-like object as a BypassFrame."""
        offset = self._frame_offset(self._write_seq)
        frame = BypassFrame()
        frame.magic = MAGIC
        frame.sequence = self._write_seq
        frame.timestamp_ns = int(getattr(tick, "timestamp_ns", time.perf_counter_ns()))
        sym = (getattr(tick, "symbol", "") or "").encode()[:8]
        frame.symbol_len = len(sym)
        frame.symbol = sym.ljust(8, b"\x00")
        frame.bid = float(getattr(tick, "bid", 0.0))
        frame.ask = float(getattr(tick, "ask", 0.0))
        frame.last = float(getattr(tick, "last", 0.0))
        frame.volume = int(getattr(tick, "volume", 0))
        raw = bytes(frame)
        self._mmap.seek(offset)
        self._mmap.write(raw)
        self._write_seq += 1

    async def poll(self, timeout_ns: int = 100_000):
        """Poll for next frame. Returns MarketTick-like object or None."""
        deadline = time.perf_counter_ns() + timeout_ns
        while time.perf_counter_ns() < deadline:
            offset = self._frame_offset(self._read_seq)
            self._mmap.seek(offset)
            raw = self._mmap.read(FRAME_SIZE)
            frame = BypassFrame.from_buffer_copy(raw)
            if frame.magic == MAGIC and frame.sequence == self._read_seq:
                self._read_seq += 1
                from types import SimpleNamespace
                return SimpleNamespace(
                    symbol=frame.symbol[:frame.symbol_len].decode(),
                    timestamp_ns=frame.timestamp_ns,
                    bid=frame.bid,
                    ask=frame.ask,
                    last=frame.last,
                    volume=frame.volume,
                )
            await asyncio.sleep(0)
        return None

    def close(self) -> None:
        self._mmap.close()


class KernelBypassAdapter:
    """Replaces the WebSocket ingestion loop in AlpacaDataFeed."""

    def __init__(self, bypass_feed: KernelBypassFeed, tick_handlers: list) -> None:
        self._feed = bypass_feed
        self._handlers = tick_handlers
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            tick = await self._feed.poll(timeout_ns=500_000)
            if tick is not None:
                for handler in self._handlers:
                    await handler(tick)
            else:
                await asyncio.sleep(0)

    def stop(self) -> None:
        self._running = False
