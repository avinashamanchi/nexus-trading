"""
Pytest configuration and shared fixtures for the trading system test suite.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Asyncio event loop ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ── Shared mock factories ──────────────────────────────────────────────────────

@pytest.fixture
def mock_bus():
    from unittest.mock import AsyncMock, MagicMock
    from infrastructure.message_bus import MessageBus

    bus = MagicMock(spec=MessageBus)
    bus.publish_raw = AsyncMock()
    bus.publish = AsyncMock()
    bus.subscribe = MagicMock()
    bus.start_consuming = AsyncMock()
    return bus


@pytest.fixture
def mock_store():
    from unittest.mock import AsyncMock, MagicMock
    from infrastructure.state_store import StateStore

    store = MagicMock(spec=StateStore)
    store.save_system_state = AsyncMock()
    store.load_system_state = AsyncMock(return_value=("initializing", None, False))
    store.upsert_position = AsyncMock()
    store.delete_position = AsyncMock()
    store.load_open_positions = AsyncMock(return_value=[])
    store.load_open_orders = AsyncMock(return_value=[])
    store.save_closed_trade = AsyncMock()
    store.upsert_daily_pnl = AsyncMock()
    store.save_heartbeat = AsyncMock()
    store.get_wash_sale_flag = AsyncMock(return_value=None)
    store.upsert_change_proposal = AsyncMock()
    store.save_reconciliation = AsyncMock()
    return store


@pytest.fixture
def mock_audit():
    from unittest.mock import AsyncMock, MagicMock
    from infrastructure.audit_log import AuditLog

    audit = MagicMock(spec=AuditLog)
    audit.record = AsyncMock()
    return audit
