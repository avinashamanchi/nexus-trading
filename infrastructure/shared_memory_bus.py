"""
Shared memory ring buffer IPC for zero-copy inter-process message passing.

Ring buffer: power-of-2 slots, each slot laid out as:
  [sequence: uint64 (8)] [length: uint32 (4)] [data: bytes (SLOT_DATA_SIZE)]

Single-producer, multi-consumer (each consumer tracks own read pointer).
Producer writes SBE-encoded BusMessage into next slot.
Consumer spins (with asyncio.sleep(0) yield) waiting for new sequences.

Implementation note: uses memoryview + struct directly instead of ctypes
c_char arrays to avoid null-byte truncation in ctypes c_char assignment and
BufferError on SharedMemory.close() from lingering ctypes from_buffer views.
"""
from __future__ import annotations

import asyncio
import struct
import time
from multiprocessing.shared_memory import SharedMemory

from core.enums import Topic
from core.models import BusMessage
from infrastructure.sbe_codec import SBECodec

SLOT_DATA_SIZE = 8192

# Slot layout: sequence(Q=8) + length(I=4) + data(8192) = 8204 bytes
_HDR = struct.Struct("=QI")   # native-endian: uint64 sequence, uint32 length
_HDR_SIZE = _HDR.size         # 12
SLOT_SIZE = _HDR_SIZE + SLOT_DATA_SIZE  # 8204

# Sentinel: all bits set = "unwritten".  New shared memory is zeroed, so
# without a sentinel seq=0 would immediately match consumer_seq=0.
_UNWRITTEN = (1 << 64) - 1   # 0xFFFFFFFFFFFFFFFF


# ── Kept for API compatibility (tests may import it) ──────────────────────────

class RingSlot:
    """Documentation-only placeholder; actual storage uses memoryview+struct."""
    __slots__ = ("sequence", "length")

    def __init__(self) -> None:
        self.sequence: int = _UNWRITTEN
        self.length: int = 0


class SharedRingBuffer:
    """
    Lock-free single-producer multi-consumer ring buffer backed by shared memory.

    Slots are indexed by sequence number modulo num_slots.
    A slot is "readable" when its stored sequence equals the expected consumer_seq.
    """

    def __init__(
        self,
        name: str,
        num_slots: int = 4096,
        slot_size: int = SLOT_DATA_SIZE,
        create: bool = True,
    ) -> None:
        assert (num_slots & (num_slots - 1)) == 0, "num_slots must be power of 2"
        self._num_slots = num_slots
        self._mask = num_slots - 1
        self._name = name
        total = SLOT_SIZE * num_slots
        if create:
            try:
                self._shm = SharedMemory(name=name, create=True, size=total)
            except FileExistsError:
                self._shm = SharedMemory(name=name, create=False, size=total)
        else:
            self._shm = SharedMemory(name=name, create=False, size=total)

        # Keep a memoryview — much easier to release cleanly than ctypes from_buffer
        self._mv = memoryview(self._shm.buf).cast("B")
        self._producer_seq: int = 0

        # Write UNWRITTEN sentinel into every slot's sequence field
        sentinel = struct.pack("=Q", _UNWRITTEN)
        for i in range(self._num_slots):
            off = i * SLOT_SIZE
            self._mv[off: off + 8] = sentinel

    def _off(self, seq: int) -> int:
        """Byte offset of the slot for the given sequence number."""
        return (seq & self._mask) * SLOT_SIZE

    def write(self, data: bytes) -> int:
        """Write *data* into the next available slot. Returns the sequence number."""
        seq = self._producer_seq
        off = self._off(seq)
        payload = data[:SLOT_DATA_SIZE]
        n = len(payload)
        # Write length
        self._mv[off + 8: off + 12] = struct.pack("=I", n)
        # Write payload (raw bytes — no null-truncation risk)
        self._mv[off + 12: off + 12 + n] = payload
        # Write sequence LAST — acts as "published" flag for the consumer
        self._mv[off: off + 8] = struct.pack("=Q", seq)
        self._producer_seq += 1
        return seq

    def read(self, consumer_seq: int, timeout_ns: int = 1_000_000) -> bytes | None:
        """
        Spin-wait for the slot at *consumer_seq* to be published.
        Returns the raw slot data bytes, or None on timeout.
        """
        off = self._off(consumer_seq)
        deadline = time.perf_counter_ns() + timeout_ns
        while time.perf_counter_ns() < deadline:
            stored_seq = struct.unpack_from("=Q", self._mv, off)[0]
            if stored_seq == consumer_seq:
                length = struct.unpack_from("=I", self._mv, off + 8)[0]
                return bytes(self._mv[off + 12: off + 12 + length])
        return None

    def close(self, unlink: bool = False) -> None:
        """Release the memoryview then close (and optionally unlink) shared memory."""
        self._mv.release()
        self._shm.close()
        if unlink:
            try:
                self._shm.unlink()
            except Exception:
                pass


class SharedMemoryBus:
    """
    Shared memory backed message bus.

    Provides the same publish/subscribe interface as InMemoryBus,
    using a shared memory ring buffer as the transport layer.
    Messages are SBE-encoded before writing to the ring.
    """

    def __init__(self, name: str = "nexus_smbus", num_slots: int = 4096) -> None:
        self._ring = SharedRingBuffer(name, num_slots, create=True)
        self._consumer_seq: int = 0
        self._handlers: dict[Topic, list] = {}
        self._running = False
        self._poll_task: asyncio.Task | None = None

    async def publish(self, message: BusMessage) -> None:
        """Publish a message to the shared memory ring buffer."""
        data = SBECodec.encode(message)
        self._ring.write(data)

    def subscribe(self, topic: Topic, handler) -> None:
        """Register a handler coroutine for a topic."""
        self._handlers.setdefault(topic, []).append(handler)

    async def start_consuming(self) -> None:
        """Start the background poll loop as an asyncio task."""
        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="smbus_poll_loop"
        )

    async def _poll_loop(self) -> None:
        """Continuously poll the ring buffer and dispatch messages to handlers."""
        while self._running:
            data = self._ring.read(self._consumer_seq, timeout_ns=100_000)
            if data is not None:
                try:
                    msg = SBECodec.decode(data)
                    for h in self._handlers.get(msg.topic, []):
                        await h(msg)
                except Exception:
                    pass
                self._consumer_seq += 1
            await asyncio.sleep(0)

    async def close(self) -> None:
        """Stop consuming and release the shared memory segment."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._ring.close(unlink=True)
