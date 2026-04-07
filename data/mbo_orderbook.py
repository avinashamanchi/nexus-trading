"""
L3 Market-By-Order order book with per-venue spoofing detection.

MBOOrderBook: per-symbol book with O(log n) add/cancel, O(1) modify.
MBOOrderBookCache: multi-symbol cache, drop-in for OrderBookCache.
"""
from __future__ import annotations
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from sortedcontainers import SortedDict

from core.enums import SpoofingRisk
from core.models import L2Level, OrderBook


@dataclass
class MBOOrder:
    order_id: str
    price: float
    side: str   # "bid" or "ask"
    qty: int
    venue: str
    ts_ns: int


@dataclass
class PriceLevel:
    price: float
    orders: deque = field(default_factory=deque)  # deque[MBOOrder] — FIFO
    total_qty: int = 0  # cached sum


class MBOOrderBook:
    def __init__(self, symbol: str, max_depth: int = 50) -> None:
        self.symbol = symbol
        self._max_depth = max_depth
        # bids: negated price key → descending order
        self._bids: SortedDict = SortedDict()   # {-price: PriceLevel}
        # asks: positive price key → ascending order
        self._asks: SortedDict = SortedDict()   # {+price: PriceLevel}
        # O(1) lookup by order_id
        self._orders: dict[str, MBOOrder] = {}
        # Per-venue add/cancel tracking for spoofing detection
        # {venue: deque[(ts_ns, event_type)]} where event_type in ("add","cancel")
        self._venue_events: dict[str, deque] = {}

    def _get_or_create_level(self, side: str, price: float) -> PriceLevel:
        if side == "bid":
            key = -price
            if key not in self._bids:
                self._bids[key] = PriceLevel(price=price)
            return self._bids[key]
        else:
            key = price
            if key not in self._asks:
                self._asks[key] = PriceLevel(price=price)
            return self._asks[key]

    def add_order(self, order_id: str, price: float, side: str, qty: int, venue: str, ts_ns: int) -> None:
        order = MBOOrder(order_id=order_id, price=price, side=side, qty=qty, venue=venue, ts_ns=ts_ns)
        self._orders[order_id] = order
        level = self._get_or_create_level(side, price)
        level.orders.append(order)
        level.total_qty += qty
        # Track venue event
        events = self._venue_events.setdefault(venue, deque())
        events.append((ts_ns, "add"))

    def modify_order(self, order_id: str, new_qty: int) -> None:
        if order_id not in self._orders:
            return
        order = self._orders[order_id]
        level = self._get_or_create_level(order.side, order.price)
        level.total_qty += new_qty - order.qty
        order.qty = new_qty

    def cancel_order(self, order_id: str) -> None:
        if order_id not in self._orders:
            return
        order = self._orders.pop(order_id)
        if order.side == "bid":
            key = -order.price
            book = self._bids
        else:
            key = order.price
            book = self._asks
        if key in book:
            level = book[key]
            # Remove order from FIFO queue
            try:
                level.orders.remove(order)
            except ValueError:
                pass
            level.total_qty -= order.qty
            if level.total_qty <= 0 or not level.orders:
                del book[key]
        # Track venue event
        events = self._venue_events.setdefault(order.venue, deque())
        events.append((time.perf_counter_ns(), "cancel"))

    def get_snapshot(self, levels: int = 10) -> OrderBook:
        """Return L2-compatible OrderBook snapshot."""
        bid_levels = []
        for neg_price, level in list(self._bids.items())[:levels]:
            bid_levels.append(L2Level(price=level.price, size=level.total_qty))
        ask_levels = []
        for price, level in list(self._asks.items())[:levels]:
            ask_levels.append(L2Level(price=level.price, size=level.total_qty))
        return OrderBook(
            symbol=self.symbol,
            timestamp=datetime.utcnow(),
            bids=bid_levels,
            asks=ask_levels,
        )

    def get_mbo_depth(self, side: str, levels: int = 10) -> list[tuple[float, int, list[str]]]:
        """Return true L3 depth: [(price, total_qty, [order_ids...])]"""
        book = self._bids if side == "bid" else self._asks
        result = []
        for key, level in list(book.items())[:levels]:
            order_ids = [o.order_id for o in level.orders]
            result.append((level.price, level.total_qty, order_ids))
        return result

    def detect_spoofing(self, venue: str, window_sec: float = 60.0) -> SpoofingRisk:
        """Detect spoofing via add/cancel ratio in rolling window."""
        events = self._venue_events.get(venue, deque())
        cutoff_ns = time.perf_counter_ns() - int(window_sec * 1e9)
        # Prune old events
        while events and events[0][0] < cutoff_ns:
            events.popleft()
        if not events:
            return SpoofingRisk.NONE
        adds = sum(1 for _, t in events if t == "add")
        cancels = sum(1 for _, t in events if t == "cancel")
        if adds == 0:
            return SpoofingRisk.NONE
        ratio = cancels / adds
        if ratio >= 0.95:
            return SpoofingRisk.CRITICAL
        if ratio >= 0.80:
            return SpoofingRisk.HIGH
        if ratio >= 0.60:
            return SpoofingRisk.MEDIUM
        if ratio >= 0.40:
            return SpoofingRisk.LOW
        return SpoofingRisk.NONE


class MBOOrderBookCache:
    """Multi-symbol cache; drop-in replacement for OrderBookCache."""

    def __init__(self, max_depth: int = 50) -> None:
        self._books: dict[str, MBOOrderBook] = {}
        self._max_depth = max_depth

    def _get_book(self, symbol: str) -> MBOOrderBook:
        if symbol not in self._books:
            self._books[symbol] = MBOOrderBook(symbol, self._max_depth)
        return self._books[symbol]

    def apply_add(self, symbol: str, order_id: str, price: float, side: str, qty: int, venue: str, ts_ns: int) -> None:
        self._get_book(symbol).add_order(order_id, price, side, qty, venue, ts_ns)

    def apply_modify(self, symbol: str, order_id: str, new_qty: int) -> None:
        self._get_book(symbol).modify_order(order_id, new_qty)

    def apply_cancel(self, symbol: str, order_id: str) -> None:
        self._get_book(symbol).cancel_order(order_id)

    def get_book(self, symbol: str) -> OrderBook | None:
        if symbol not in self._books:
            return None
        return self._books[symbol].get_snapshot()

    def total_bid_depth(self, symbol: str, levels: int = 5) -> int:
        if symbol not in self._books:
            return 0
        book = self._books[symbol]
        return sum(level.total_qty for level in list(book._bids.values())[:levels])

    def total_ask_depth(self, symbol: str, levels: int = 5) -> int:
        if symbol not in self._books:
            return 0
        book = self._books[symbol]
        return sum(level.total_qty for level in list(book._asks.values())[:levels])

    def detect_liquidity_sweep(self, symbol: str, direction: str, min_sweep_size: int = 5000) -> bool:
        if symbol not in self._books:
            return False
        if direction == "bid":
            depth = self.total_bid_depth(symbol, levels=3)
        else:
            depth = self.total_ask_depth(symbol, levels=3)
        return depth < min_sweep_size

    def get_spoofing_risk(self, symbol: str, venue: str) -> SpoofingRisk:
        if symbol not in self._books:
            return SpoofingRisk.NONE
        return self._books[symbol].detect_spoofing(venue)
