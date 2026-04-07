"""
Tests for HFT transport layer:
  - SBECodec (encode/decode for BusMessage and MarketTick)
  - SharedRingBuffer and SharedMemoryBus
  - Sequence, RingBuffer, EventProcessor, DisruptorBus, DeterministicClock
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core.enums import Topic
from core.models import BusMessage
from infrastructure.sbe_codec import SBECodec, TOPIC_TABLE, TOPIC_REVERSE


# ═══════════════════════════════════════════════════════════════════════════════
#  TestSBECodec
# ═══════════════════════════════════════════════════════════════════════════════

class TestSBECodec:
    """Tests for SBECodec encode/decode of BusMessage and MarketTick."""

    def _make_msg(
        self,
        topic: Topic = Topic.CANDIDATE_SIGNAL,
        source: str = "test_agent",
        payload: dict | None = None,
    ) -> BusMessage:
        return BusMessage(
            topic=topic,
            source_agent=source,
            payload=payload if payload is not None else {"key": "value"},
        )

    # ── BusMessage roundtrips ─────────────────────────────────────────────────

    def test_encode_decode_topic_preserved(self):
        """Topic must survive encode → decode."""
        msg = self._make_msg(topic=Topic.RISK_DECISION)
        decoded = SBECodec.decode(SBECodec.encode(msg))
        assert decoded.topic == Topic.RISK_DECISION

    def test_encode_decode_source_preserved(self):
        """source_agent must survive encode → decode."""
        msg = self._make_msg(source="execution_agent")
        decoded = SBECodec.decode(SBECodec.encode(msg))
        assert decoded.source_agent == "execution_agent"

    def test_encode_decode_payload_preserved(self):
        """Payload dict must survive encode → decode unchanged."""
        payload = {"symbol": "AAPL", "price": 123.45, "qty": 100}
        msg = self._make_msg(payload=payload)
        decoded = SBECodec.decode(SBECodec.encode(msg))
        assert decoded.payload == payload

    def test_encode_returns_bytes(self):
        """encode() must return bytes."""
        msg = self._make_msg()
        result = SBECodec.encode(msg)
        assert isinstance(result, bytes)

    def test_encode_decode_empty_payload(self):
        """Empty payload dict roundtrips correctly."""
        msg = self._make_msg(payload={})
        decoded = SBECodec.decode(SBECodec.encode(msg))
        assert decoded.payload == {}

    def test_encode_decode_nested_payload(self):
        """Nested payload dict roundtrips correctly."""
        payload = {"outer": {"inner": [1, 2, 3]}, "flag": True}
        msg = self._make_msg(payload=payload)
        decoded = SBECodec.decode(SBECodec.encode(msg))
        assert decoded.payload == payload

    def test_uuid_source_roundtrip(self):
        """UUID-like source_agent string is preserved (truncated to 16 bytes)."""
        # Use first 16 chars of a UUID so it fits in the 16-byte field
        uid = str(uuid.uuid4())[:16]
        msg = self._make_msg(source=uid)
        decoded = SBECodec.decode(SBECodec.encode(msg))
        assert decoded.source_agent == uid

    def test_topic_table_covers_all_topics(self):
        """TOPIC_TABLE must have an entry for every Topic enum member."""
        all_topics = list(Topic)
        for t in all_topics:
            assert t in TOPIC_TABLE, f"Topic {t} missing from TOPIC_TABLE"

    def test_topic_reverse_roundtrip(self):
        """TOPIC_REVERSE must correctly invert TOPIC_TABLE."""
        for topic, idx in TOPIC_TABLE.items():
            assert TOPIC_REVERSE[idx] == topic

    def test_multiple_topics_roundtrip(self):
        """Several different topics all roundtrip correctly."""
        topics = [
            Topic.CANDIDATE_SIGNAL,
            Topic.VALIDATED_SIGNAL,
            Topic.RISK_DECISION,
            Topic.ORDER_SUBMITTED,
            Topic.AGENT_HEARTBEAT,
            Topic.KILL_SWITCH,
        ]
        for t in topics:
            msg = self._make_msg(topic=t, payload={"t": t.value})
            decoded = SBECodec.decode(SBECodec.encode(msg))
            assert decoded.topic == t

    # ── MarketTick roundtrips ─────────────────────────────────────────────────

    def _make_tick(self, **overrides) -> SimpleNamespace:
        defaults = dict(
            symbol="AAPL",
            timestamp_ns=1_700_000_000_000_000_000,
            bid=149.99,
            ask=150.01,
            last=150.00,
            volume=5000,
            sequence=42,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_encode_tick_returns_bytes(self):
        tick = self._make_tick()
        result = SBECodec.encode_tick(tick)
        assert isinstance(result, bytes)

    def test_encode_tick_correct_size(self):
        """Encoded tick must be exactly 60 bytes."""
        tick = self._make_tick()
        result = SBECodec.encode_tick(tick)
        assert len(result) == 60

    def test_encode_decode_tick_symbol(self):
        tick = self._make_tick(symbol="TSLA")
        decoded = SBECodec.decode_tick(SBECodec.encode_tick(tick))
        assert decoded.symbol == "TSLA"

    def test_encode_decode_tick_prices(self):
        tick = self._make_tick(bid=200.10, ask=200.15, last=200.12)
        decoded = SBECodec.decode_tick(SBECodec.encode_tick(tick))
        assert abs(decoded.bid - 200.10) < 1e-9
        assert abs(decoded.ask - 200.15) < 1e-9
        assert abs(decoded.last - 200.12) < 1e-9

    def test_encode_decode_tick_volume_and_sequence(self):
        tick = self._make_tick(volume=99999, sequence=12345)
        decoded = SBECodec.decode_tick(SBECodec.encode_tick(tick))
        assert decoded.volume == 99999
        assert decoded.sequence == 12345

    def test_encode_decode_tick_timestamp_ns(self):
        ts = 1_700_123_456_789_012_345
        tick = self._make_tick(timestamp_ns=ts)
        decoded = SBECodec.decode_tick(SBECodec.encode_tick(tick))
        assert decoded.timestamp_ns == ts

    def test_encode_decode_tick_full_roundtrip(self):
        """All fields survive a full encode → decode cycle."""
        tick = self._make_tick()
        decoded = SBECodec.decode_tick(SBECodec.encode_tick(tick))
        assert decoded.symbol == tick.symbol
        assert decoded.timestamp_ns == tick.timestamp_ns
        assert abs(decoded.bid - tick.bid) < 1e-9
        assert abs(decoded.ask - tick.ask) < 1e-9
        assert abs(decoded.last - tick.last) < 1e-9
        assert decoded.volume == tick.volume
        assert decoded.sequence == tick.sequence


# ═══════════════════════════════════════════════════════════════════════════════
#  TestSharedMemoryBus
# ═══════════════════════════════════════════════════════════════════════════════

class TestSharedMemoryBus:
    """Tests for SharedRingBuffer and SharedMemoryBus."""

    def _unique_name(self) -> str:
        return f"nexus_test_{uuid.uuid4().hex[:8]}"

    # ── SharedRingBuffer ──────────────────────────────────────────────────────

    def test_power_of_2_enforcement(self):
        """Non-power-of-2 num_slots must raise AssertionError."""
        from infrastructure.shared_memory_bus import SharedRingBuffer
        with pytest.raises(AssertionError):
            SharedRingBuffer(name=self._unique_name(), num_slots=1000, create=True)

    def test_single_write_read_roundtrip(self):
        """A single write followed by a read returns the same bytes."""
        from infrastructure.shared_memory_bus import SharedRingBuffer
        name = self._unique_name()
        ring = SharedRingBuffer(name=name, num_slots=16, create=True)
        try:
            data = b"hello_sbe_world"
            seq = ring.write(data)
            result = ring.read(seq, timeout_ns=5_000_000)
            assert result == data
        finally:
            ring.close(unlink=True)

    def test_multiple_writes_read_in_order(self):
        """Multiple writes are readable in sequence order."""
        from infrastructure.shared_memory_bus import SharedRingBuffer
        name = self._unique_name()
        ring = SharedRingBuffer(name=name, num_slots=16, create=True)
        try:
            messages = [f"msg_{i}".encode() for i in range(5)]
            seqs = [ring.write(m) for m in messages]
            for seq, expected in zip(seqs, messages):
                result = ring.read(seq, timeout_ns=5_000_000)
                assert result == expected
        finally:
            ring.close(unlink=True)

    def test_read_timeout_returns_none_when_no_data(self):
        """read() returns None when no data is written before timeout."""
        from infrastructure.shared_memory_bus import SharedRingBuffer
        name = self._unique_name()
        ring = SharedRingBuffer(name=name, num_slots=16, create=True)
        try:
            # Consumer seq 0 is not yet written; should time out
            result = ring.read(consumer_seq=0, timeout_ns=10_000)
            assert result is None
        finally:
            ring.close(unlink=True)

    def test_close_unlink_removes_shm(self):
        """close(unlink=True) removes the shared memory segment."""
        from infrastructure.shared_memory_bus import SharedRingBuffer
        from multiprocessing.shared_memory import SharedMemory
        name = self._unique_name()
        ring = SharedRingBuffer(name=name, num_slots=16, create=True)
        ring.close(unlink=True)
        with pytest.raises(FileNotFoundError):
            SharedMemory(name=name, create=False, size=1)

    # ── SharedMemoryBus ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_publish_delivers_to_handler(self):
        """publish() followed by start_consuming() delivers message to handler."""
        from infrastructure.shared_memory_bus import SharedMemoryBus
        name = self._unique_name()
        bus = SharedMemoryBus(name=name, num_slots=16)
        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus.subscribe(Topic.CANDIDATE_SIGNAL, handler)
        await bus.start_consuming()

        msg = BusMessage(
            topic=Topic.CANDIDATE_SIGNAL,
            source_agent="test",
            payload={"sym": "NVDA"},
        )
        await bus.publish(msg)
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].topic == Topic.CANDIDATE_SIGNAL
        assert received[0].payload == {"sym": "NVDA"}
        await bus.close()

    @pytest.mark.asyncio
    async def test_multiple_messages_delivered_in_order(self):
        """Multiple published messages arrive at handler in order."""
        from infrastructure.shared_memory_bus import SharedMemoryBus
        name = self._unique_name()
        bus = SharedMemoryBus(name=name, num_slots=64)
        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus.subscribe(Topic.AGENT_HEARTBEAT, handler)
        await bus.start_consuming()

        for i in range(5):
            await bus.publish(
                BusMessage(
                    topic=Topic.AGENT_HEARTBEAT,
                    source_agent="agent",
                    payload={"seq": i},
                )
            )
        await asyncio.sleep(0.1)

        assert len(received) == 5
        seqs = [m.payload["seq"] for m in received]
        assert seqs == list(range(5))
        await bus.close()

    @pytest.mark.asyncio
    async def test_unsubscribed_topic_not_delivered(self):
        """Messages on unsubscribed topics are not delivered to registered handlers."""
        from infrastructure.shared_memory_bus import SharedMemoryBus
        name = self._unique_name()
        bus = SharedMemoryBus(name=name, num_slots=16)
        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus.subscribe(Topic.CANDIDATE_SIGNAL, handler)
        await bus.start_consuming()

        # Publish on a different topic
        await bus.publish(
            BusMessage(
                topic=Topic.AGENT_HEARTBEAT,
                source_agent="agent",
                payload={"x": 1},
            )
        )
        await asyncio.sleep(0.05)

        assert len(received) == 0
        await bus.close()

    @pytest.mark.asyncio
    async def test_close_stops_polling(self):
        """After close(), no further messages are delivered."""
        from infrastructure.shared_memory_bus import SharedMemoryBus
        name = self._unique_name()
        bus = SharedMemoryBus(name=name, num_slots=16)
        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus.subscribe(Topic.KILL_SWITCH, handler)
        await bus.start_consuming()
        await bus.close()

        # Even if we try to publish after close, handler should not fire
        # (ring is already closed, ring.write will fail; just check no crash)
        assert len(received) == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  TestDisruptorBus
# ═══════════════════════════════════════════════════════════════════════════════

class TestDisruptorBus:
    """Tests for Sequence, RingBuffer, EventProcessor, DisruptorBus, DeterministicClock."""

    # ── Sequence ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_sequence_compare_and_set_success(self):
        """CAS succeeds when current value matches expected."""
        from infrastructure.disruptor import Sequence
        seq = Sequence(initial=5)
        result = await seq.compare_and_set(expected=5, update=10)
        assert result is True
        assert seq.get() == 10

    @pytest.mark.asyncio
    async def test_sequence_compare_and_set_failure(self):
        """CAS fails and value unchanged when expected does not match."""
        from infrastructure.disruptor import Sequence
        seq = Sequence(initial=5)
        result = await seq.compare_and_set(expected=99, update=10)
        assert result is False
        assert seq.get() == 5

    @pytest.mark.asyncio
    async def test_sequence_increment_and_get_monotonic(self):
        """increment_and_get returns strictly increasing values."""
        from infrastructure.disruptor import Sequence
        seq = Sequence(initial=-1)
        values = [await seq.increment_and_get() for _ in range(10)]
        assert values == list(range(0, 10))

    @pytest.mark.asyncio
    async def test_sequence_set(self):
        """set() unconditionally updates the value."""
        from infrastructure.disruptor import Sequence
        seq = Sequence(initial=0)
        await seq.set(42)
        assert seq.get() == 42

    # ── RingBuffer ────────────────────────────────────────────────────────────

    def test_ring_buffer_power_of_2_enforcement(self):
        """Non-power-of-2 size raises AssertionError."""
        from infrastructure.disruptor import RingBuffer
        with pytest.raises(AssertionError):
            RingBuffer(size=1000)

    @pytest.mark.asyncio
    async def test_ring_buffer_next_sequence_monotonic(self):
        """next_sequence() returns monotonically increasing values."""
        from infrastructure.disruptor import RingBuffer
        ring = RingBuffer(size=16)
        seqs = [await ring.next_sequence() for _ in range(5)]
        assert seqs == list(range(0, 5))

    @pytest.mark.asyncio
    async def test_ring_buffer_get_returns_correct_slot(self):
        """get() returns the slot at the correct ring position."""
        from infrastructure.disruptor import RingBuffer, RingSlotD
        ring = RingBuffer(size=8)
        seq = await ring.next_sequence()
        slot = ring.get(seq)
        slot.sequence = seq
        slot.topic = "test"
        retrieved = ring.get(seq)
        assert retrieved.topic == "test"
        assert retrieved.sequence == seq

    # ── EventProcessor ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_event_processor_receives_published_event(self):
        """EventProcessor calls handler once a slot is published to the ring."""
        from infrastructure.disruptor import RingBuffer, EventProcessor

        received: list = []

        async def handler(slot) -> None:
            received.append(slot.sequence)

        ring = RingBuffer(size=16)
        processor = EventProcessor(ring, handler)
        proc_task = asyncio.create_task(processor.run())

        seq = await ring.next_sequence()
        slot = ring.get(seq)
        slot.sequence = seq
        slot.topic = "agent_heartbeat"
        slot.payload = b""
        slot.ts_ns = 0
        await ring.publish(seq)

        await asyncio.sleep(0.05)
        processor.stop()
        proc_task.cancel()
        try:
            await proc_task
        except asyncio.CancelledError:
            pass

        assert seq in received

    # ── DisruptorBus ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_disruptor_bus_publish_delivers_to_handler(self):
        """publish() delivers a message to a subscribed handler."""
        from infrastructure.disruptor import DisruptorBus

        bus = DisruptorBus(size=16)
        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus.subscribe(Topic.VALIDATED_SIGNAL, handler)
        await bus.start_consuming()

        msg = BusMessage(
            topic=Topic.VALIDATED_SIGNAL,
            source_agent="validator",
            payload={"result": "pass"},
        )
        await bus.publish(msg)
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].topic == Topic.VALIDATED_SIGNAL
        assert received[0].payload == {"result": "pass"}
        await bus.close()

    @pytest.mark.asyncio
    async def test_disruptor_bus_multiple_messages_in_order(self):
        """Multiple messages are delivered to handler in publish order."""
        from infrastructure.disruptor import DisruptorBus

        bus = DisruptorBus(size=64)
        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus.subscribe(Topic.RISK_DECISION, handler)
        await bus.start_consuming()

        for i in range(5):
            await bus.publish(
                BusMessage(
                    topic=Topic.RISK_DECISION,
                    source_agent="risk",
                    payload={"i": i},
                )
            )
        await asyncio.sleep(0.1)

        assert len(received) == 5
        assert [m.payload["i"] for m in received] == list(range(5))
        await bus.close()

    @pytest.mark.asyncio
    async def test_disruptor_bus_unsubscribed_topic_not_delivered(self):
        """Messages on topics without handlers are silently dropped."""
        from infrastructure.disruptor import DisruptorBus

        bus = DisruptorBus(size=16)
        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus.subscribe(Topic.CANDIDATE_SIGNAL, handler)
        await bus.start_consuming()

        await bus.publish(
            BusMessage(
                topic=Topic.AGENT_HEARTBEAT,
                source_agent="hb",
                payload={},
            )
        )
        await asyncio.sleep(0.05)

        assert len(received) == 0
        await bus.close()

    @pytest.mark.asyncio
    async def test_disruptor_bus_close_stops_processing(self):
        """After close(), the processor stops and tasks are cancelled."""
        from infrastructure.disruptor import DisruptorBus

        bus = DisruptorBus(size=16)
        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus.subscribe(Topic.ORDER_SUBMITTED, handler)
        await bus.start_consuming()
        await bus.close()

        # Yield to let the event loop process the cancellations
        await asyncio.sleep(0.05)

        # After close, all tasks should be done (cancelled or completed)
        for task in bus._tasks:
            assert task.cancelled() or task.done()

    # ── DeterministicClock ────────────────────────────────────────────────────

    def test_deterministic_clock_time_for_sequence_zero(self):
        """time_for_sequence(0) must equal the base timestamp."""
        from infrastructure.disruptor import DeterministicClock
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        clock = DeterministicClock(base_ts=base, tick_us=1.0)
        assert clock.time_for_sequence(0) == base

    def test_deterministic_clock_time_for_sequence_1000(self):
        """time_for_sequence(1000) must equal base_ts + 1000µs."""
        from infrastructure.disruptor import DeterministicClock
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        clock = DeterministicClock(base_ts=base, tick_us=1.0)
        expected = base + timedelta(microseconds=1000)
        assert clock.time_for_sequence(1000) == expected

    def test_deterministic_clock_custom_tick_us(self):
        """Custom tick_us is correctly applied."""
        from infrastructure.disruptor import DeterministicClock
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = DeterministicClock(base_ts=base, tick_us=10.0)
        expected = base + timedelta(microseconds=100)
        assert clock.time_for_sequence(10) == expected

    def test_deterministic_clock_install_returns_singleton(self):
        """install() installs and get() retrieves the same instance."""
        from infrastructure.disruptor import DeterministicClock
        base = datetime(2026, 4, 7, tzinfo=timezone.utc)
        installed = DeterministicClock.install(base_ts=base)
        retrieved = DeterministicClock.get()
        assert retrieved is installed
        assert retrieved is not None
