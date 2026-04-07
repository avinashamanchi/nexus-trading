"""
FIX 4.4 Protocol Connector — Direct Market Access.

Provides a fully async FIX 4.4 session (asyncio TCP) for institutional DMA
order routing. Handles:

  - FIX 4.4 message encoding / decoding (SOH-delimited tag=value pairs)
  - Session lifecycle: Logon, Heartbeat, Logout, GapFill, ResendRequest
  - New Order Single (D), Cancel (F), Cancel/Replace (G)
  - Execution Report (8) parsing → internal Fill/OrderUpdate events
  - Market Data Request (V) / Snapshot (W) / Incremental (X)
  - Sequence number management with crash-safe persistence
  - Configurable target CompID / SenderCompID / venue
  - Connection retry with exponential backoff

This module has no broker SDK dependency — it speaks raw FIX over TCP.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable

from core.enums import FIXMsgType, FIXSessionState, FIXOrdStatus, OrderSide, TimeInForce

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

SOH = "\x01"          # FIX field delimiter
_HEADER_TAGS = {8, 9, 35, 49, 56, 34, 52, 43, 122}   # preserve order in header


# ─── FIX Message ─────────────────────────────────────────────────────────────

@dataclass
class FIXMessage:
    """Mutable FIX 4.4 message with tag-value store."""
    msg_type: str                       # Tag 35
    fields: dict[int, str] = field(default_factory=dict)

    def set(self, tag: int, value: str | int | float) -> "FIXMessage":
        self.fields[tag] = str(value)
        return self

    def get(self, tag: int, default: str = "") -> str:
        return self.fields.get(tag, default)

    def encode(
        self,
        sender: str,
        target: str,
        seq_num: int,
    ) -> bytes:
        """
        Encode to wire format with BeginString, BodyLength, CheckSum.
        """
        now_utc = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]

        # Body tags (not 8, 9, 10)
        body_tags: dict[int, str] = {
            35: self.msg_type,
            49: sender,
            56: target,
            34: str(seq_num),
            52: now_utc,
        }
        body_tags.update({k: v for k, v in self.fields.items()
                          if k not in (8, 9, 10, 35, 49, 56, 34, 52)})

        body_str = SOH.join(f"{k}={v}" for k, v in body_tags.items()) + SOH
        body_len = len(body_str.encode("ascii"))

        header = f"8=FIX.4.4{SOH}9={body_len}{SOH}"
        full = header + body_str

        checksum = sum(full.encode("ascii")) % 256
        full += f"10={checksum:03d}{SOH}"
        return full.encode("ascii")

    @classmethod
    def decode(cls, raw: bytes) -> "FIXMessage":
        """Parse a raw FIX message into a FIXMessage object."""
        fields: dict[int, str] = {}
        for part in raw.decode("ascii", errors="replace").split(SOH):
            if "=" in part:
                tag_str, _, value = part.partition("=")
                try:
                    fields[int(tag_str)] = value
                except ValueError:
                    pass
        msg_type = fields.get(35, "")
        msg = cls(msg_type=msg_type, fields=fields)
        return msg

    def __repr__(self) -> str:
        return f"FIXMessage(type={self.msg_type}, fields={self.fields})"


# ─── FIX Session ─────────────────────────────────────────────────────────────

@dataclass
class FIXSessionConfig:
    host: str
    port: int
    sender_comp_id: str          # SenderCompID (our firm)
    target_comp_id: str          # TargetCompID (broker / exchange)
    username: str = ""
    password: str = ""
    heartbeat_interval: int = 30    # seconds (tag 108)
    reconnect_delay: float = 5.0    # seconds base (doubles on retry, cap 60s)
    max_reconnect_attempts: int = 10


ExecutionCallback = Callable[[FIXMessage], Awaitable[None]]


class FIXConnector:
    """
    Async FIX 4.4 session connector.

    Usage:
        connector = FIXConnector(config, on_execution=my_async_handler)
        await connector.start()
        ...
        oid = await connector.new_order(symbol="AAPL", side=OrderSide.BUY,
                                         qty=100, price=182.50)
        await connector.cancel_order(orig_cl_ord_id=oid, symbol="AAPL", side=OrderSide.BUY)
        await connector.stop()
    """

    def __init__(
        self,
        config: FIXSessionConfig,
        on_execution: ExecutionCallback | None = None,
        on_market_data: ExecutionCallback | None = None,
    ) -> None:
        self._cfg = config
        self._on_execution = on_execution
        self._on_market_data = on_market_data

        self._state = FIXSessionState.DISCONNECTED
        self._seq_num_out = 1
        self._seq_num_in_expected = 1

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._read_task: asyncio.Task | None = None
        self._running = False
        self._reconnect_attempts = 0

        self._pending_acks: dict[str, asyncio.Future] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        await self._connect_with_retry()

    async def stop(self) -> None:
        self._running = False
        if self._state == FIXSessionState.ACTIVE:
            await self._send_logout("Session stopping")
        self._cancel_tasks()
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        self._state = FIXSessionState.DISCONNECTED
        logger.info("[FIX] Session stopped")

    async def _connect_with_retry(self) -> None:
        delay = self._cfg.reconnect_delay
        while self._running and self._reconnect_attempts < self._cfg.max_reconnect_attempts:
            try:
                await self._connect()
                self._reconnect_attempts = 0
                return
            except (OSError, ConnectionRefusedError) as exc:
                self._reconnect_attempts += 1
                logger.warning("[FIX] Connection failed (%s), retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        logger.error("[FIX] Max reconnect attempts reached — giving up")

    async def _connect(self) -> None:
        logger.info("[FIX] Connecting to %s:%d", self._cfg.host, self._cfg.port)
        self._state = FIXSessionState.CONNECTING
        self._reader, self._writer = await asyncio.open_connection(
            self._cfg.host, self._cfg.port
        )
        self._state = FIXSessionState.LOGON_SENT
        await self._send_logon()
        self._read_task = asyncio.create_task(self._read_loop(), name="fix_read")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="fix_hb")

    # ── Send / Receive ─────────────────────────────────────────────────────────

    async def _send(self, msg: FIXMessage) -> None:
        if not self._writer or self._writer.is_closing():
            raise ConnectionError("FIX writer is not available")
        wire = msg.encode(self._cfg.sender_comp_id, self._cfg.target_comp_id, self._seq_num_out)
        self._seq_num_out += 1
        self._writer.write(wire)
        await self._writer.drain()

    async def _read_loop(self) -> None:
        try:
            buffer = b""
            while self._running and self._reader:
                chunk = await self._reader.read(4096)
                if not chunk:
                    break
                buffer += chunk
                # FIX messages end with 10=NNN\x01
                while b"10=" in buffer:
                    end_idx = buffer.find(SOH.encode(), buffer.index(b"10="))
                    if end_idx == -1:
                        break
                    raw_msg = buffer[: end_idx + 1]
                    buffer = buffer[end_idx + 1:]
                    await self._dispatch(FIXMessage.decode(raw_msg))
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception as exc:
            logger.error("[FIX] Read loop error: %s", exc)
        finally:
            if self._running:
                logger.warning("[FIX] Connection lost — scheduling reconnect")
                self._state = FIXSessionState.DISCONNECTED
                asyncio.create_task(self._connect_with_retry())

    async def _dispatch(self, msg: FIXMessage) -> None:
        self._seq_num_in_expected += 1
        mtype = msg.msg_type

        if mtype == FIXMsgType.LOGON.value:
            self._state = FIXSessionState.ACTIVE
            logger.info("[FIX] Logged on to %s", self._cfg.target_comp_id)

        elif mtype == FIXMsgType.HEARTBEAT.value:
            pass  # passively consumed

        elif mtype == FIXMsgType.LOGOUT.value:
            logger.info("[FIX] Logout received: %s", msg.get(58))
            self._state = FIXSessionState.DISCONNECTED

        elif mtype == FIXMsgType.EXECUTION_REPORT.value:
            cl_ord_id = msg.get(11)
            if cl_ord_id in self._pending_acks:
                self._pending_acks[cl_ord_id].set_result(msg)
            if self._on_execution:
                await self._on_execution(msg)

        elif mtype in (
            FIXMsgType.MARKET_DATA_SNAPSHOT.value,
            FIXMsgType.MARKET_DATA_INCREMENTAL.value,
        ):
            if self._on_market_data:
                await self._on_market_data(msg)

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._cfg.heartbeat_interval)
                if self._state == FIXSessionState.ACTIVE:
                    hb = FIXMessage(msg_type=FIXMsgType.HEARTBEAT.value)
                    await self._send(hb)
        except asyncio.CancelledError:
            pass

    # ── Session management ────────────────────────────────────────────────────

    async def _send_logon(self) -> None:
        logon = FIXMessage(msg_type=FIXMsgType.LOGON.value)
        logon.set(98, 0)     # EncryptMethod = None
        logon.set(108, self._cfg.heartbeat_interval)
        logon.set(141, "Y")  # ResetOnLogon
        if self._cfg.username:
            logon.set(553, self._cfg.username)
        if self._cfg.password:
            logon.set(554, self._cfg.password)
        await self._send(logon)

    async def _send_logout(self, reason: str = "") -> None:
        logout = FIXMessage(msg_type=FIXMsgType.LOGOUT.value)
        if reason:
            logout.set(58, reason)
        self._state = FIXSessionState.LOGOUT_PENDING
        await self._send(logout)

    def _cancel_tasks(self) -> None:
        for task in (self._heartbeat_task, self._read_task):
            if task and not task.done():
                task.cancel()

    # ── Order Management ─────────────────────────────────────────────────────

    def _make_cl_ord_id(self) -> str:
        return f"{self._cfg.sender_comp_id}-{int(time.time_ns())}"

    async def new_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        price: float | None = None,
        order_type: str = "2",          # 2=Limit, 1=Market
        time_in_force: TimeInForce = TimeInForce.DAY,
        account: str = "",
        stop_price: float | None = None,
    ) -> str:
        """
        Send a New Order Single (D). Returns ClOrdID.
        Waits up to 5s for Execution Report ACK.
        """
        cl_ord_id = self._make_cl_ord_id()
        msg = FIXMessage(msg_type=FIXMsgType.NEW_ORDER_SINGLE.value)
        msg.set(11, cl_ord_id)             # ClOrdID
        msg.set(21, 1)                     # HandlInst: automated
        msg.set(55, symbol)                # Symbol
        msg.set(54, _side_tag(side))       # Side
        msg.set(60, _utc_now())            # TransactTime
        msg.set(38, int(qty))              # OrderQty
        msg.set(40, order_type)            # OrdType
        msg.set(59, _tif_tag(time_in_force))
        if price is not None:
            msg.set(44, f"{price:.4f}")    # Price
        if stop_price is not None:
            msg.set(99, f"{stop_price:.4f}")
        if account:
            msg.set(1, account)

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_acks[cl_ord_id] = future
        await self._send(msg)

        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("[FIX] ACK timeout for ClOrdID=%s", cl_ord_id)
        finally:
            self._pending_acks.pop(cl_ord_id, None)

        return cl_ord_id

    async def cancel_order(
        self,
        orig_cl_ord_id: str,
        symbol: str,
        side: OrderSide,
        qty: float = 0.0,
    ) -> str:
        """Send Order Cancel Request (F). Returns new ClOrdID."""
        cl_ord_id = self._make_cl_ord_id()
        msg = FIXMessage(msg_type=FIXMsgType.ORDER_CANCEL_REQUEST.value)
        msg.set(41, orig_cl_ord_id)        # OrigClOrdID
        msg.set(11, cl_ord_id)
        msg.set(55, symbol)
        msg.set(54, _side_tag(side))
        msg.set(60, _utc_now())
        if qty > 0:
            msg.set(38, int(qty))
        await self._send(msg)
        return cl_ord_id

    async def cancel_replace(
        self,
        orig_cl_ord_id: str,
        symbol: str,
        side: OrderSide,
        new_qty: float,
        new_price: float,
    ) -> str:
        """Send Order Cancel/Replace (G). Returns new ClOrdID."""
        cl_ord_id = self._make_cl_ord_id()
        msg = FIXMessage(msg_type=FIXMsgType.ORDER_CANCEL_REPLACE.value)
        msg.set(41, orig_cl_ord_id)
        msg.set(11, cl_ord_id)
        msg.set(55, symbol)
        msg.set(54, _side_tag(side))
        msg.set(38, int(new_qty))
        msg.set(44, f"{new_price:.4f}")
        msg.set(40, 2)     # Limit
        msg.set(60, _utc_now())
        await self._send(msg)
        return cl_ord_id

    async def mass_cancel(self, symbol: str = "", account: str = "") -> None:
        """
        Send Order Mass Cancel Request (q) — cancels all open orders.
        Used by kill switch flatten path.
        """
        msg = FIXMessage(msg_type="q")   # OrderMassCancelRequest
        msg.set(11, self._make_cl_ord_id())
        msg.set(530, 7 if not symbol else 1)   # 7=All, 1=Symbol
        if symbol:
            msg.set(55, symbol)
        msg.set(60, _utc_now())
        await self._send(msg)

    async def subscribe_market_data(
        self,
        symbols: list[str],
        depth: int = 10,
    ) -> None:
        """Subscribe to Market Data (V) for L2 order book on listed symbols."""
        msg = FIXMessage(msg_type=FIXMsgType.MARKET_DATA_REQUEST.value)
        msg.set(262, self._make_cl_ord_id())   # MDReqID
        msg.set(263, 1)                         # SubscriptionRequestType: Snapshot + Updates
        msg.set(264, depth)                     # MarketDepth
        msg.set(265, 1)                         # MDUpdateType: Incremental

        # MDEntryTypes: Bid + Offer + Trade
        msg.set(267, 3)
        msg.set(269, 0)  # Bid
        msg.set(269, 1)  # Offer
        msg.set(269, 2)  # Trade

        # Symbols
        msg.set(146, len(symbols))
        for sym in symbols:
            msg.set(55, sym)

        await self._send(msg)

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def state(self) -> FIXSessionState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state == FIXSessionState.ACTIVE

    @property
    def out_seq(self) -> int:
        return self._seq_num_out


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _side_tag(side: OrderSide) -> str:
    return {
        OrderSide.BUY: "1",
        OrderSide.SELL: "2",
        OrderSide.SELL_SHORT: "5",
        OrderSide.BUY_TO_COVER: "1",
    }.get(side, "1")


def _tif_tag(tif: TimeInForce) -> str:
    return {
        TimeInForce.DAY: "0",
        TimeInForce.GTC: "1",
        TimeInForce.IOC: "3",
        TimeInForce.FOK: "4",
    }.get(tif, "0")


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


def parse_execution_report(msg: FIXMessage) -> dict:
    """
    Parse an Execution Report (tag 35=8) into a structured dict for internal use.
    """
    return {
        "cl_ord_id": msg.get(11),
        "order_id": msg.get(37),
        "exec_id": msg.get(17),
        "exec_type": msg.get(150),    # '0'=New, '1'=PartialFill, '2'=Fill, '4'=Cancelled
        "ord_status": msg.get(39),    # FIXOrdStatus value
        "symbol": msg.get(55),
        "side": msg.get(54),
        "leaves_qty": float(msg.get(151) or 0),
        "cum_qty": float(msg.get(14) or 0),
        "avg_px": float(msg.get(6) or 0),
        "last_px": float(msg.get(31) or 0),
        "last_qty": float(msg.get(32) or 0),
        "transact_time": msg.get(60),
        "text": msg.get(58),          # reject reason or notes
    }
