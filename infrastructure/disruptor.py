"""
LMAX Disruptor pattern — pure asyncio implementation.

Sequence: asyncio-safe CAS counter for claiming ring buffer slots.
RingBuffer: power-of-2 fixed-size ring of RingSlotD dataclasses.
EventProcessor: consumer spin-loop that calls handler per event.
DisruptorBus: drop-in replacement for InMemoryBus using Disruptor ring.
DeterministicClock: maps sequence numbers to synthetic datetime for SMRE replay.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Awaitable

from core.enums import Topic
from core.models import BusMessage
from infrastructure.sbe_codec import SBECodec

MessageHandler = Callable[[BusMessage], Awaitable[None]]


class Sequence:
    """asyncio-safe compare-and-set counter."""

    def __init__(self, initial: int = -1) -> None:
        self._value = initial
        self._lock = asyncio.Lock()

    def get(self) -> int:
        return self._value

    async def compare_and_set(self, expected: int, update: int) -> bool:
        """Atomically set value to update only if current value == expected."""
        async with self._lock:
            if self._value == expected:
                self._value = update
                return True
            return False

    async def increment_and_get(self) -> int:
        """Atomically increment by 1 and return the new value."""
        async with self._lock:
            self._value += 1
            return self._value

    async def set(self, value: int) -> None:
        """Unconditionally set the counter to value."""
        async with self._lock:
            self._value = value


@dataclass
class RingSlotD:
    """A single slot in the Disruptor ring buffer."""

    sequence: int = -1
    topic: str = ""
    source: str = ""
    payload: bytes = b""
    ts_ns: int = 0


class RingBuffer:
    """Power-of-2 fixed-size ring buffer of RingSlotD entries."""

    def __init__(self, size: int = 4096) -> None:
        assert (size & (size - 1)) == 0, "size must be power of 2"
        self._size = size
        self._mask = size - 1
        self._slots: list[RingSlotD] = [RingSlotD() for _ in range(size)]
        self._cursor = Sequence(-1)    # last published sequence
        self._claim_seq = Sequence(-1)  # last claimed by producer

    async def next_sequence(self) -> int:
        """
        Claim the next producer sequence number.
        Spin-waits if the ring is full (producer is more than size ahead of cursor).
        """
        seq = await self._claim_seq.increment_and_get()
        # Spin if we have wrapped around past unpublished entries
        while True:
            cursor = self._cursor.get()
            if seq - cursor <= self._size:
                break
            await asyncio.sleep(0)
        return seq

    def get(self, sequence: int) -> RingSlotD:
        """Return the slot for the given sequence number."""
        return self._slots[sequence & self._mask]

    async def publish(self, sequence: int) -> None:
        """Mark a sequence as published (advance cursor)."""
        await self._cursor.set(sequence)

    @property
    def cursor(self) -> int:
        """Return the last published sequence number."""
        return self._cursor.get()


class EventProcessor:
    """
    Consumer that spins on the ring buffer calling handler for each new event.
    """

    def __init__(
        self,
        ring_buffer: RingBuffer,
        handler: Callable[[RingSlotD], Awaitable[None]],
    ) -> None:
        self._ring = ring_buffer
        self._handler = handler
        self._sequence = Sequence(-1)
        self._running = False

    async def run(self) -> None:
        """Spin-poll the ring buffer and invoke handler for each new slot."""
        self._running = True
        while self._running:
            next_seq = self._sequence.get() + 1
            cursor = self._ring.cursor
            if next_seq <= cursor:
                slot = self._ring.get(next_seq)
                if slot.sequence == next_seq:
                    await self._handler(slot)
                    await self._sequence.set(next_seq)
            else:
                await asyncio.sleep(0)  # yield to event loop

    def stop(self) -> None:
        """Signal the processor to stop on next iteration."""
        self._running = False


class DisruptorBus:
    """
    Drop-in replacement for InMemoryBus using the LMAX Disruptor pattern.

    Messages are published to a ring buffer, and a single EventProcessor
    dispatches each event to all registered handlers by topic.
    """

    def __init__(self, size: int = 4096) -> None:
        self._ring = RingBuffer(size)
        self._handlers: dict[Topic, list[MessageHandler]] = {}
        self._processors: list[EventProcessor] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False

    async def publish(self, message: BusMessage) -> None:
        """Claim a ring slot, write the SBE-encoded message, and publish."""
        seq = await self._ring.next_sequence()
        slot = self._ring.get(seq)
        slot.topic = message.topic.value
        slot.source = message.source_agent or ""
        slot.payload = SBECodec.encode(message)
        slot.ts_ns = time.perf_counter_ns()
        slot.sequence = seq
        await self._ring.publish(seq)

    def subscribe(self, topic: Topic, handler: MessageHandler) -> None:
        """Register a handler coroutine for a topic."""
        self._handlers.setdefault(topic, []).append(handler)

    async def start_consuming(self) -> None:
        """Start an EventProcessor task that dispatches events to all handlers."""
        self._running = True

        async def dispatch(slot: RingSlotD) -> None:
            try:
                msg = SBECodec.decode(slot.payload)
                for h in self._handlers.get(msg.topic, []):
                    await h(msg)
            except Exception:
                pass

        processor = EventProcessor(self._ring, dispatch)
        self._processors.append(processor)
        task = asyncio.create_task(processor.run(), name="disruptor_processor")
        self._tasks.append(task)

    async def close(self) -> None:
        """Stop all processors and cancel background tasks."""
        self._running = False
        for p in self._processors:
            p.stop()
        for t in self._tasks:
            t.cancel()


class DeterministicClock:
    """
    Maps Disruptor sequence numbers to synthetic datetime objects.

    Useful for SMRE (Scenario Market Replay Engine) deterministic replay,
    where each ring buffer sequence represents a fixed time step.
    """

    _instance: "DeterministicClock | None" = None

    def __init__(self, base_ts: datetime, tick_us: float = 1.0) -> None:
        self._base_ts = base_ts
        self._tick_us = tick_us

    def time_for_sequence(self, seq: int) -> datetime:
        """Return the synthetic datetime for a given sequence number."""
        return self._base_ts + timedelta(microseconds=seq * self._tick_us)

    @classmethod
    def install(cls, base_ts: datetime | None = None) -> "DeterministicClock":
        """Install a singleton DeterministicClock and return it."""
        if base_ts is None:
            base_ts = datetime.now(timezone.utc)
        cls._instance = cls(base_ts)
        return cls._instance

    @classmethod
    def get(cls) -> "DeterministicClock | None":
        """Return the installed singleton, or None if not installed."""
        return cls._instance
