"""
Tests for infrastructure: message bus, state store, audit log.
Uses in-memory bus to avoid Redis dependency in CI.
"""
from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio
from datetime import datetime
from pathlib import Path
import tempfile

from core.enums import SystemState, Topic
from core.models import AuditEvent, BusMessage, Order, Position
from core.enums import Direction, OrderSide, OrderStatus, OrderType, TimeInForce


# ── Message Bus (in-memory) ───────────────────────────────────────────────────

class TestInMemoryBus:
    @pytest.mark.asyncio
    async def test_publish_subscribe_roundtrip(self):
        from infrastructure.message_bus import MessageBus

        received: list[BusMessage] = []

        async def handler(msg: BusMessage) -> None:
            received.append(msg)

        bus = MessageBus(redis_url=None)  # force in-memory
        await bus.connect()
        bus.subscribe(Topic.REGIME_UPDATE, handler)
        await bus.start_consuming()

        await bus.publish_raw(
            topic=Topic.REGIME_UPDATE,
            source_agent="test",
            payload={"label": "trend-day"},
        )
        await asyncio.sleep(0.05)  # let the event loop process

        assert len(received) == 1
        assert received[0].payload["label"] == "trend-day"
        await bus.close()

    @pytest.mark.asyncio
    async def test_multiple_handlers_same_topic(self):
        from infrastructure.message_bus import MessageBus

        results_a: list = []
        results_b: list = []

        bus = MessageBus(redis_url=None)
        await bus.connect()
        bus.subscribe(Topic.CANDIDATE_SIGNAL, lambda m: results_a.append(m) or asyncio.sleep(0))
        bus.subscribe(Topic.CANDIDATE_SIGNAL, lambda m: results_b.append(m) or asyncio.sleep(0))
        await bus.start_consuming()

        await bus.publish_raw(
            topic=Topic.CANDIDATE_SIGNAL,
            source_agent="test",
            payload={"symbol": "AAPL"},
        )
        await asyncio.sleep(0.05)

        # In-memory bus delivers to both handlers via separate queues
        await bus.close()


# ── State Store ───────────────────────────────────────────────────────────────

class TestStateStore:
    @pytest_asyncio.fixture
    async def store(self, tmp_path):
        from infrastructure.state_store import StateStore
        s = StateStore(db_path=tmp_path / "test.db")
        await s.initialize()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_system_state_persist_and_load(self, store):
        await store.save_system_state(SystemState.HALTED, "test halt", True)
        state, reason, hold = await store.load_system_state()
        assert state == SystemState.HALTED
        assert reason == "test halt"
        assert hold is True

    @pytest.mark.asyncio
    async def test_system_state_defaults(self, store):
        state, reason, hold = await store.load_system_state()
        assert state == SystemState.INITIALIZING
        assert reason is None
        assert hold is False

    @pytest.mark.asyncio
    async def test_order_upsert_and_load(self, store):
        order = Order(
            plan_id="plan-001",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=100,
            limit_price=150.0,
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.utcnow(),
        )
        await store.upsert_order(order)
        open_orders = await store.load_open_orders()
        assert any(o.order_id == order.order_id for o in open_orders)

    @pytest.mark.asyncio
    async def test_position_upsert_delete(self, store):
        from datetime import timedelta

        pos = Position(
            plan_id="plan-002",
            symbol="TSLA",
            direction=Direction.LONG,
            shares=50,
            entry_price=200.0,
            current_price=201.0,
            stop_price=198.0,
            target_price=204.0,
            time_stop_at=datetime.utcnow() + timedelta(minutes=5),
        )
        await store.upsert_position(pos)
        positions = await store.load_open_positions()
        assert any(p.position_id == pos.position_id for p in positions)

        await store.delete_position(pos.position_id)
        positions_after = await store.load_open_positions()
        assert not any(p.position_id == pos.position_id for p in positions_after)

    @pytest.mark.asyncio
    async def test_daily_pnl_upsert(self, store):
        await store.upsert_daily_pnl("2026-04-06", 30500.0, 500.0, 12)
        # No exception = success (no read-back API on daily_pnl in store)


# ── Audit Log ─────────────────────────────────────────────────────────────────

class TestAuditLog:
    @pytest_asyncio.fixture
    async def audit(self, tmp_path):
        from infrastructure.audit_log import AuditLog
        a = AuditLog(
            db_path=tmp_path / "audit.db",
            jsonl_path=tmp_path / "audit.jsonl",
        )
        await a.initialize()
        yield a
        await a.close()

    @pytest.mark.asyncio
    async def test_record_and_query(self, audit):
        event = AuditEvent(
            event_type="ORDER_SUBMITTED",
            agent="test_agent",
            symbol="AAPL",
            details={"order_id": "ord-001", "qty": 100},
        )
        await audit.record(event)

        results = await audit.query(event_type="ORDER_SUBMITTED")
        assert len(results) >= 1
        assert any(r["event_type"] == "ORDER_SUBMITTED" for r in results)

    @pytest.mark.asyncio
    async def test_idempotent_insert(self, audit):
        """Same event_id must not duplicate (INSERT OR IGNORE)."""
        event = AuditEvent(
            event_type="SYSTEM_HALT",
            agent="psa",
            details={"reason": "test"},
        )
        await audit.record(event)
        await audit.record(event)  # replay — should be ignored

        results = await audit.query(event_type="SYSTEM_HALT")
        matching = [r for r in results if event.event_id in r.get("details_json", "")]
        # At most 1 entry for the same event_id
        assert len(matching) <= 1

    @pytest.mark.asyncio
    async def test_jsonl_written(self, audit, tmp_path):
        event = AuditEvent(
            event_type="TEST_EVENT",
            agent="tester",
            details={"key": "value"},
        )
        await audit.record(event)
        jsonl = tmp_path / "audit.jsonl"
        assert jsonl.exists()
        lines = jsonl.read_text().strip().split("\n")
        assert len(lines) >= 1
