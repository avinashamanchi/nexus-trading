"""
ClickHouse columnar database integration for tick and OHLCV storage.

ClickHouseClient: async HTTP client via httpx (ClickHouse HTTP interface).
TickWarehouseClickHouse: dual-write facade — writes to ClickHouse + binary fallback.
  All reads delegate to the fallback TickWarehouse.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# DDL
_TICKS_DDL = """
CREATE TABLE IF NOT EXISTS nexus.ticks (
    ts       UInt64,
    symbol   LowCardinality(String),
    price    Float64,
    bid      Float64,
    ask      Float64,
    volume   UInt32,
    side     LowCardinality(String)
) ENGINE = MergeTree()
ORDER BY (symbol, ts)
"""

_OHLCV_DDL = """
CREATE TABLE IF NOT EXISTS nexus.ohlcv (
    ts       UInt64,
    symbol   LowCardinality(String),
    interval UInt32,
    open     Float64,
    high     Float64,
    low      Float64,
    close    Float64,
    vwap     Float64,
    volume   UInt32
) ENGINE = MergeTree()
ORDER BY (symbol, interval, ts)
"""


class ClickHouseClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        database: str = "nexus",
        timeout: float = 5.0,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._database = database
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> bool:
        """Test connection. Returns False on any error (ClickHouse may not be running)."""
        try:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            resp = await self._client.get(f"{self._base_url}/", params={"query": "SELECT 1"})
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("[CH] Connection failed: %s", exc)
            if self._client:
                await self._client.aclose()
                self._client = None
            return False

    async def execute(self, query: str) -> str:
        """Execute a DDL or DML query via HTTP POST."""
        if self._client is None:
            return ""
        try:
            resp = await self._client.post(
                f"{self._base_url}/",
                params={"database": self._database},
                content=query.encode(),
                timeout=self._timeout,
            )
            return resp.text
        except Exception as exc:
            logger.error("[CH] Query error: %s", exc)
            return ""

    async def insert_batch(self, table: str, rows: list[dict]) -> None:
        """Insert rows in JSONEachRow format."""
        if self._client is None or not rows:
            return
        try:
            body = "\n".join(json.dumps(row) for row in rows)
            await self._client.post(
                f"{self._base_url}/",
                params={"query": f"INSERT INTO {self._database}.{table} FORMAT JSONEachRow", "database": self._database},
                content=body.encode(),
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.error("[CH] Insert error table=%s: %s", table, exc)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


class TickWarehouseClickHouse:
    """
    Dual-write TickWarehouse: writes to ClickHouse AND binary fallback.
    All reads delegate to fallback. Failures in ClickHouse are logged only (fire-and-forget).
    """

    def __init__(self, ch_client: ClickHouseClient, fallback) -> None:
        self._ch = ch_client
        self._fallback = fallback
        self._batch: list[dict] = []
        self._batch_size = 500
        self._flush_interval_sec = 2.0
        self._flush_task: asyncio.Task | None = None
        self._connected = False

    async def open(self) -> None:
        # Open fallback first
        if hasattr(self._fallback, "open"):
            await self._fallback.open()
        # Attempt ClickHouse connection (non-fatal if unavailable)
        self._connected = await self._ch.connect()
        if self._connected:
            # Create database if needed
            await self._ch.execute("CREATE DATABASE IF NOT EXISTS nexus")
            await self._ch.execute(_TICKS_DDL)
            await self._ch.execute(_OHLCV_DDL)
        else:
            logger.warning("[CH] ClickHouse unavailable — using binary fallback only")
        # Start periodic flush loop
        self._flush_task = asyncio.create_task(self._flush_loop(), name="ch_flush")

    async def write(self, tick) -> None:
        """Write tick to fallback (always) and batch for ClickHouse."""
        if hasattr(self._fallback, "write"):
            await self._fallback.write(tick)
        if self._connected:
            row = {
                "ts": int(getattr(tick, "timestamp_ns", 0) // 1_000),  # microseconds
                "symbol": getattr(tick, "symbol", ""),
                "price": float(getattr(tick, "price", getattr(tick, "last", 0.0))),
                "bid": float(getattr(tick, "bid", 0.0)),
                "ask": float(getattr(tick, "ask", 0.0)),
                "volume": int(getattr(tick, "volume", 0)),
                "side": str(getattr(tick, "side", "")),
            }
            self._batch.append(row)
            if len(self._batch) >= self._batch_size:
                asyncio.create_task(self._flush_to_clickhouse())

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_sec)
            if self._batch:
                await self._flush_to_clickhouse()

    async def _flush_to_clickhouse(self) -> None:
        if not self._batch or not self._connected:
            return
        batch = self._batch[:]
        self._batch.clear()
        try:
            await self._ch.insert_batch("ticks", batch)
        except Exception as exc:
            logger.error("[CH] Flush failed: %s", exc)

    async def close(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
        if self._batch and self._connected:
            await self._flush_to_clickhouse()
        await self._ch.close()
        if hasattr(self._fallback, "close"):
            await self._fallback.close()

    # Delegate all reads to fallback
    def read(self, *args, **kwargs):
        return self._fallback.read(*args, **kwargs) if hasattr(self._fallback, "read") else []

    def read_bars(self, *args, **kwargs):
        return self._fallback.read_bars(*args, **kwargs) if hasattr(self._fallback, "read_bars") else []

    def resample(self, *args, **kwargs):
        return self._fallback.resample(*args, **kwargs) if hasattr(self._fallback, "resample") else []

    def replay(self, *args, **kwargs):
        return self._fallback.replay(*args, **kwargs) if hasattr(self._fallback, "replay") else iter([])
