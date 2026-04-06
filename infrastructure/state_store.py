"""
State Store — async SQLite via aiosqlite.

Persists all critical system state so the system can recover gracefully
from any process crash. All writes are atomic via transactions.

Tables:
  - positions         : open positions (live truth)
  - orders            : all orders and their status
  - daily_pnl         : per-session P/L snapshots
  - reconciliation    : broker reconciliation reports
  - system_state      : current FSM state
  - agent_heartbeats  : last heartbeat per agent
  - change_proposals  : §5.5 handoff protocol state
  - wash_sale_flags   : rolling wash-sale exposure
  - shadow_trades     : shadow mode trade records
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

from core.models import (
    ChangeProposal,
    ClosedTrade,
    Order,
    PortfolioState,
    Position,
    ReconciliationReport,
    WashSaleFlag,
)
from core.enums import SystemState

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "db" / "trading.db"


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS positions (
    position_id     TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    shares          INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    current_price   REAL NOT NULL,
    stop_price      REAL NOT NULL,
    target_price    REAL NOT NULL,
    time_stop_at    TEXT NOT NULL,
    trailing_stop   REAL,
    opened_at       TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    broker_order_id TEXT,
    plan_id         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    status          TEXT NOT NULL,
    submitted_at    TEXT,
    filled_at       TEXT,
    cancelled_at    TEXT,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closed_trades (
    trade_id        TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    setup_type      TEXT NOT NULL,
    net_pnl         REAL NOT NULL,
    closed_at       TEXT NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    session_date    TEXT PRIMARY KEY,
    account_equity  REAL NOT NULL,
    daily_pnl       REAL NOT NULL,
    trade_count     INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_reports (
    report_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    status          TEXT NOT NULL,
    reconciled_at   TEXT NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    state           TEXT NOT NULL,
    halt_reason     TEXT,
    anomaly_hold    INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_heartbeats (
    agent_id        TEXT PRIMARY KEY,
    agent_name      TEXT NOT NULL,
    state           TEXT NOT NULL,
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_proposals (
    proposal_id     TEXT PRIMARY KEY,
    proposal_type   TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wash_sale_flags (
    symbol          TEXT PRIMARY KEY,
    wash_sale_window_end  TEXT NOT NULL,
    net_loss        REAL NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_trades (
    shadow_id       TEXT PRIMARY KEY,
    session_date    TEXT NOT NULL,
    net_pnl         REAL,
    raw_json        TEXT NOT NULL
);
"""


class StateStore:
    """
    Async SQLite state store. All methods are coroutines.
    Call `await store.initialize()` before use.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info("StateStore initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    # ── Positions ─────────────────────────────────────────────────────────────

    async def upsert_position(self, pos: Position) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT INTO positions
                (position_id, plan_id, symbol, direction, shares, entry_price,
                 current_price, stop_price, target_price, time_stop_at,
                 trailing_stop, opened_at, updated_at, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(position_id) DO UPDATE SET
                current_price=excluded.current_price,
                stop_price=excluded.stop_price,
                trailing_stop=excluded.trailing_stop,
                updated_at=excluded.updated_at,
                raw_json=excluded.raw_json
            """,
            (
                pos.position_id, pos.plan_id, pos.symbol,
                pos.direction.value, pos.shares, pos.entry_price,
                pos.current_price, pos.stop_price, pos.target_price,
                pos.time_stop_at.isoformat(),
                pos.trailing_stop, pos.opened_at.isoformat(),
                self._now(), pos.model_dump_json(),
            ),
        )
        await self._db.commit()  # type: ignore[union-attr]

    async def delete_position(self, position_id: str) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            "DELETE FROM positions WHERE position_id = ?", (position_id,)
        )
        await self._db.commit()  # type: ignore[union-attr]

    async def load_open_positions(self) -> list[Position]:
        cursor = await self._db.execute(  # type: ignore[union-attr]
            "SELECT raw_json FROM positions"
        )
        rows = await cursor.fetchall()
        return [Position.model_validate_json(r["raw_json"]) for r in rows]

    # ── Orders ────────────────────────────────────────────────────────────────

    async def upsert_order(self, order: Order) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT INTO orders
                (order_id, broker_order_id, plan_id, symbol, status,
                 submitted_at, filled_at, cancelled_at, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(order_id) DO UPDATE SET
                broker_order_id=excluded.broker_order_id,
                status=excluded.status,
                filled_at=excluded.filled_at,
                cancelled_at=excluded.cancelled_at,
                raw_json=excluded.raw_json
            """,
            (
                order.order_id, order.broker_order_id, order.plan_id,
                order.symbol, order.status.value,
                order.submitted_at.isoformat() if order.submitted_at else None,
                order.filled_at.isoformat() if order.filled_at else None,
                order.cancelled_at.isoformat() if order.cancelled_at else None,
                order.model_dump_json(),
            ),
        )
        await self._db.commit()  # type: ignore[union-attr]

    async def load_open_orders(self) -> list[Order]:
        cursor = await self._db.execute(  # type: ignore[union-attr]
            "SELECT raw_json FROM orders WHERE status NOT IN ('filled','cancelled','rejected','expired')"
        )
        rows = await cursor.fetchall()
        return [Order.model_validate_json(r["raw_json"]) for r in rows]

    # ── Closed Trades ─────────────────────────────────────────────────────────

    async def save_closed_trade(self, trade: ClosedTrade, session_date: str) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT OR IGNORE INTO closed_trades
                (trade_id, plan_id, symbol, session_date, setup_type,
                 net_pnl, closed_at, raw_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                trade.trade_id, trade.plan_id, trade.symbol,
                session_date, trade.setup_type.value,
                trade.net_pnl, trade.closed_at.isoformat(),
                trade.model_dump_json(),
            ),
        )
        await self._db.commit()  # type: ignore[union-attr]

    async def load_closed_trades(self, session_date: str) -> list[ClosedTrade]:
        cursor = await self._db.execute(  # type: ignore[union-attr]
            "SELECT raw_json FROM closed_trades WHERE session_date = ?", (session_date,)
        )
        rows = await cursor.fetchall()
        return [ClosedTrade.model_validate_json(r["raw_json"]) for r in rows]

    # ── System State ──────────────────────────────────────────────────────────

    async def save_system_state(
        self,
        state: SystemState,
        halt_reason: str | None = None,
        anomaly_hold: bool = False,
    ) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT INTO system_state (id, state, halt_reason, anomaly_hold, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state=excluded.state,
                halt_reason=excluded.halt_reason,
                anomaly_hold=excluded.anomaly_hold,
                updated_at=excluded.updated_at
            """,
            (state.value, halt_reason, int(anomaly_hold), self._now()),
        )
        await self._db.commit()  # type: ignore[union-attr]

    async def load_system_state(self) -> tuple[SystemState, str | None, bool]:
        cursor = await self._db.execute(  # type: ignore[union-attr]
            "SELECT state, halt_reason, anomaly_hold FROM system_state WHERE id = 1"
        )
        row = await cursor.fetchone()
        if not row:
            return SystemState.INITIALIZING, None, False
        return (
            SystemState(row["state"]),
            row["halt_reason"],
            bool(row["anomaly_hold"]),
        )

    # ── Daily P/L ─────────────────────────────────────────────────────────────

    async def upsert_daily_pnl(
        self, session_date: str, equity: float, pnl: float, trade_count: int
    ) -> None:
        data = {
            "session_date": session_date,
            "account_equity": equity,
            "daily_pnl": pnl,
            "trade_count": trade_count,
            "updated_at": self._now(),
        }
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT INTO daily_pnl
                (session_date, account_equity, daily_pnl, trade_count, updated_at, raw_json)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(session_date) DO UPDATE SET
                account_equity=excluded.account_equity,
                daily_pnl=excluded.daily_pnl,
                trade_count=excluded.trade_count,
                updated_at=excluded.updated_at,
                raw_json=excluded.raw_json
            """,
            (session_date, equity, pnl, trade_count, self._now(), json.dumps(data)),
        )
        await self._db.commit()  # type: ignore[union-attr]

    # ── Reconciliation ────────────────────────────────────────────────────────

    async def save_reconciliation(self, report: ReconciliationReport) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT INTO reconciliation_reports (status, reconciled_at, raw_json)
            VALUES (?, ?, ?)
            """,
            (report.status.value, report.reconciled_at.isoformat(), report.model_dump_json()),
        )
        await self._db.commit()  # type: ignore[union-attr]

    # ── Agent Heartbeats ──────────────────────────────────────────────────────

    async def save_heartbeat(
        self, agent_id: str, agent_name: str, state: str, error_count: int
    ) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT INTO agent_heartbeats (agent_id, agent_name, state, error_count, last_seen)
            VALUES (?,?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET
                state=excluded.state,
                error_count=excluded.error_count,
                last_seen=excluded.last_seen
            """,
            (agent_id, agent_name, state, error_count, self._now()),
        )
        await self._db.commit()  # type: ignore[union-attr]

    async def load_all_heartbeats(self) -> list[dict]:
        cursor = await self._db.execute(  # type: ignore[union-attr]
            "SELECT agent_id, agent_name, state, error_count, last_seen FROM agent_heartbeats"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Change Proposals ──────────────────────────────────────────────────────

    async def upsert_change_proposal(self, proposal: ChangeProposal) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT INTO change_proposals
                (proposal_id, proposal_type, status, created_at, updated_at, raw_json)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at,
                raw_json=excluded.raw_json
            """,
            (
                proposal.proposal_id, proposal.proposal_type.value,
                proposal.status.value,
                proposal.created_at.isoformat(), self._now(),
                proposal.model_dump_json(),
            ),
        )
        await self._db.commit()  # type: ignore[union-attr]

    async def load_pending_proposals(self) -> list[ChangeProposal]:
        cursor = await self._db.execute(  # type: ignore[union-attr]
            """SELECT raw_json FROM change_proposals
               WHERE status NOT IN ('live', 'hgl_rejected', 'edge_failed')"""
        )
        rows = await cursor.fetchall()
        return [ChangeProposal.model_validate_json(r["raw_json"]) for r in rows]

    # ── Wash Sale Flags ───────────────────────────────────────────────────────

    async def upsert_wash_sale(self, flag: WashSaleFlag) -> None:
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT INTO wash_sale_flags (symbol, wash_sale_window_end, net_loss, raw_json)
            VALUES (?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                wash_sale_window_end=excluded.wash_sale_window_end,
                net_loss=excluded.net_loss,
                raw_json=excluded.raw_json
            """,
            (
                flag.symbol,
                flag.wash_sale_window_end.isoformat(),
                flag.net_loss,
                flag.model_dump_json(),
            ),
        )
        await self._db.commit()  # type: ignore[union-attr]

    async def get_wash_sale_flag(self, symbol: str) -> WashSaleFlag | None:
        cursor = await self._db.execute(  # type: ignore[union-attr]
            "SELECT raw_json FROM wash_sale_flags WHERE symbol = ?", (symbol,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        flag = WashSaleFlag.model_validate_json(row["raw_json"])
        if flag.wash_sale_window_end < datetime.utcnow():
            await self._db.execute(  # type: ignore[union-attr]
                "DELETE FROM wash_sale_flags WHERE symbol = ?", (symbol,)
            )
            await self._db.commit()  # type: ignore[union-attr]
            return None
        return flag
