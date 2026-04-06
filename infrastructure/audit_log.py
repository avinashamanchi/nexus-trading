"""
Immutable Audit Log — append-only SQLite table + JSONL file.

Design constraints:
  - NO row may ever be updated or deleted after insert.
  - Two-layer storage: SQLite for query + JSONL file for archival/export.
  - Every order, fill, position change, halt, and governance action is recorded.
  - Tax & Compliance Agent (Agent 15) is the primary reader.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

from core.models import AuditEvent

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "db" / "audit.db"
JSONL_PATH = Path(__file__).parent.parent / "logs" / "audit.jsonl"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS audit_log (
    event_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    agent           TEXT NOT NULL,
    symbol          TEXT,
    timestamp       TEXT NOT NULL,
    details_json    TEXT NOT NULL,
    inserted_at     TEXT NOT NULL
);

-- Intentionally no PRIMARY KEY with ON CONFLICT REPLACE — entries are
-- never modified. event_id may repeat only if the same event is replayed
-- after a crash (idempotency handled at the insert level via INSERT OR IGNORE).
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_event_id ON audit_log(event_id);
"""


class AuditLog:
    """
    Append-only audit logger.  Call `await log.initialize()` before use.
    """

    def __init__(
        self,
        db_path: Path = DB_PATH,
        jsonl_path: Path = JSONL_PATH,
    ) -> None:
        self._db_path = db_path
        self._jsonl_path = jsonl_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info("AuditLog initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def record(self, event: AuditEvent) -> None:
        """
        Append an audit event.  This method is idempotent — replaying the
        same event_id is silently ignored (INSERT OR IGNORE).
        """
        inserted_at = datetime.utcnow().isoformat()
        details_json = json.dumps(event.details)

        # ── SQLite ────────────────────────────────────────────────────────────
        await self._db.execute(  # type: ignore[union-attr]
            """
            INSERT OR IGNORE INTO audit_log
                (event_id, event_type, agent, symbol, timestamp,
                 details_json, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.agent,
                event.symbol,
                event.timestamp.isoformat(),
                details_json,
                inserted_at,
            ),
        )
        await self._db.commit()  # type: ignore[union-attr]

        # ── JSONL ─────────────────────────────────────────────────────────────
        line = json.dumps(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "agent": event.agent,
                "symbol": event.symbol,
                "timestamp": event.timestamp.isoformat(),
                "details": event.details,
                "inserted_at": inserted_at,
            }
        )
        with open(self._jsonl_path, "a") as f:
            f.write(line + "\n")

    async def query(
        self,
        event_type: str | None = None,
        symbol: str | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Read-only query helper for Tax & Compliance Agent."""
        clauses = []
        params: list = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        cursor = await self._db.execute(  # type: ignore[union-attr]
            f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ─── Convenience event constructors ───────────────────────────────────────────

def order_submitted_event(agent: str, order_id: str, symbol: str, details: dict) -> AuditEvent:
    return AuditEvent(event_type="ORDER_SUBMITTED", agent=agent, symbol=symbol,
                      details={"order_id": order_id, **details})

def order_filled_event(agent: str, order_id: str, symbol: str, fill_price: float, qty: int) -> AuditEvent:
    return AuditEvent(event_type="ORDER_FILLED", agent=agent, symbol=symbol,
                      details={"order_id": order_id, "fill_price": fill_price, "qty": qty})

def order_cancelled_event(agent: str, order_id: str, symbol: str, reason: str) -> AuditEvent:
    return AuditEvent(event_type="ORDER_CANCELLED", agent=agent, symbol=symbol,
                      details={"order_id": order_id, "reason": reason})

def trade_closed_event(agent: str, trade_id: str, symbol: str, net_pnl: float, exit_mode: str) -> AuditEvent:
    return AuditEvent(event_type="TRADE_CLOSED", agent=agent, symbol=symbol,
                      details={"trade_id": trade_id, "net_pnl": net_pnl, "exit_mode": exit_mode})

def system_halt_event(agent: str, reason: str) -> AuditEvent:
    return AuditEvent(event_type="SYSTEM_HALT", agent=agent,
                      details={"reason": reason})

def system_resume_event(agent: str, approved_by: str) -> AuditEvent:
    return AuditEvent(event_type="SYSTEM_RESUME", agent=agent,
                      details={"approved_by": approved_by})

def risk_breach_event(agent: str, rule: str, details: dict) -> AuditEvent:
    return AuditEvent(event_type="RISK_BREACH", agent=agent,
                      details={"rule": rule, **details})

def governance_approval_event(approver: str, proposal_id: str, proposal_type: str) -> AuditEvent:
    return AuditEvent(event_type="GOVERNANCE_APPROVAL", agent="human_governance",
                      details={"approver": approver, "proposal_id": proposal_id,
                               "proposal_type": proposal_type})

def wash_sale_flag_event(symbol: str, net_loss: float) -> AuditEvent:
    return AuditEvent(event_type="WASH_SALE_FLAG", agent="tax_compliance",
                      symbol=symbol, details={"net_loss": net_loss})
