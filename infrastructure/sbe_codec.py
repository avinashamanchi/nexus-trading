"""
SBE (Simple Binary Encoding) codec for Nexus message bus and market ticks.

BusMessage wire format:
  38-byte fixed header: padding(16s), topic_id(uint16), source(16s), payload_len(uint32)
  variable payload: raw bytes (JSON of payload dict)

MarketTick wire format:
  60 bytes fixed: symbol(8s), timestamp_ns(int64), bid(float64), ask(float64),
                  last(float64), volume(uint32), sequence(int64)
"""
from __future__ import annotations

import json
import struct
from types import SimpleNamespace

from core.enums import Topic
from core.models import BusMessage

# Header: padding(16s), topic_id(uint16), source(16s), payload_len(uint32) = 38 bytes
_BUS_HEADER = struct.Struct(">16sH16sI")

# Tick: symbol(8s=8), ts_ns(q=8), bid(d=8), ask(d=8), last(d=8),
#       volume(I=4), _pad(4x=4), sequence(q=8), _pad2(4x=4) = 60 bytes total
_TICK_STRUCT = struct.Struct(">8sqdddI4xq4x")

# Build topic <-> int mapping from enum order
TOPIC_TABLE: dict[Topic, int] = {t: i for i, t in enumerate(Topic)}
TOPIC_REVERSE: dict[int, Topic] = {i: t for t, i in TOPIC_TABLE.items()}


class SBECodec:
    """Simple Binary Encoding codec for BusMessage and MarketTick."""

    @classmethod
    def encode(cls, msg: BusMessage) -> bytes:
        """Encode a BusMessage to binary: 38-byte header + variable JSON payload."""
        topic_id = TOPIC_TABLE.get(msg.topic, 0)
        source_raw = (msg.source_agent or "").encode("utf-8")[:16]
        source_b = source_raw.ljust(16, b"\x00")
        payload_b = json.dumps(msg.payload).encode("utf-8")
        header = _BUS_HEADER.pack(b"\x00" * 16, topic_id, source_b, len(payload_b))
        return header + payload_b

    @classmethod
    def decode(cls, data: bytes) -> BusMessage:
        """Decode binary data into a BusMessage."""
        hdr_size = _BUS_HEADER.size  # 38 bytes
        _pad, topic_id, source_b, payload_len = _BUS_HEADER.unpack(data[:hdr_size])
        source = source_b.rstrip(b"\x00").decode("utf-8")
        topic = TOPIC_REVERSE.get(topic_id, Topic.AGENT_HEARTBEAT)
        payload_b = data[hdr_size: hdr_size + payload_len]
        payload = json.loads(payload_b)
        return BusMessage(
            topic=topic,
            source_agent=source if source else "unknown",
            payload=payload,
        )

    @classmethod
    def encode_tick(cls, tick) -> bytes:
        """Encode a MarketTick (or duck-typed object) to 60 bytes."""
        sym_raw = (getattr(tick, "symbol", "") or "").encode("utf-8")[:8]
        sym_b = sym_raw.ljust(8, b"\x00")
        ts_ns = int(getattr(tick, "timestamp_ns", 0))
        bid = float(getattr(tick, "bid", 0.0))
        ask = float(getattr(tick, "ask", 0.0))
        last = float(getattr(tick, "last", 0.0))
        volume = int(getattr(tick, "volume", 0))
        sequence = int(getattr(tick, "sequence", 0))
        return _TICK_STRUCT.pack(sym_b, ts_ns, bid, ask, last, volume, sequence)

    @classmethod
    def decode_tick(cls, data: bytes) -> SimpleNamespace:
        """Decode 60 bytes into a SimpleNamespace representing a MarketTick."""
        sym_b, ts_ns, bid, ask, last, volume, sequence = _TICK_STRUCT.unpack(
            data[: _TICK_STRUCT.size]
        )
        return SimpleNamespace(
            symbol=sym_b.rstrip(b"\x00").decode("utf-8"),
            timestamp_ns=ts_ns,
            bid=bid,
            ask=ask,
            last=last,
            volume=volume,
            sequence=sequence,
        )
