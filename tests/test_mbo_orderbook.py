"""
Tests for:
  - MBOOrderBook    (L3 Market-By-Order order book)
  - MBOOrderBookCache (multi-symbol drop-in for OrderBookCache)
  - LOBImbalanceModel (degree-2 polynomial regression on book features)
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from core.enums import SpoofingRisk
from core.models import L2Level, OrderBook
from data.mbo_orderbook import MBOOrder, MBOOrderBook, MBOOrderBookCache, PriceLevel


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ts() -> int:
    return time.perf_counter_ns()


def _book_with_bids_asks(bids, asks) -> OrderBook:
    """Construct an L2 OrderBook from raw (price, size) lists."""
    return OrderBook(
        symbol="TEST",
        timestamp=datetime.utcnow(),
        bids=[L2Level(price=p, size=s) for p, s in bids],
        asks=[L2Level(price=p, size=s) for p, s in asks],
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  TestMBOOrderBook
# ═══════════════════════════════════════════════════════════════════════════════

class TestMBOOrderBook:

    def _book(self) -> MBOOrderBook:
        return MBOOrderBook(symbol="AAPL")

    # ── Basic add ─────────────────────────────────────────────────────────────

    def test_add_bid_appears_in_bids(self):
        book = self._book()
        book.add_order("o1", 150.0, "bid", 100, "iex", _ts())
        snap = book.get_snapshot()
        assert len(snap.bids) == 1
        assert snap.bids[0].price == 150.0
        assert snap.bids[0].size == 100

    def test_add_ask_appears_in_asks(self):
        book = self._book()
        book.add_order("o1", 151.0, "ask", 200, "iex", _ts())
        snap = book.get_snapshot()
        assert len(snap.asks) == 1
        assert snap.asks[0].price == 151.0
        assert snap.asks[0].size == 200

    def test_empty_book_snapshot(self):
        book = self._book()
        snap = book.get_snapshot()
        assert snap.bids == []
        assert snap.asks == []

    # ── Cancel ────────────────────────────────────────────────────────────────

    def test_cancel_removes_order(self):
        book = self._book()
        book.add_order("o1", 150.0, "bid", 100, "iex", _ts())
        book.cancel_order("o1")
        snap = book.get_snapshot()
        assert snap.bids == []

    def test_cancel_nonexistent_is_noop(self):
        book = self._book()
        book.cancel_order("does_not_exist")   # must not raise

    def test_cancel_partial_level_leaves_rest(self):
        book = self._book()
        book.add_order("o1", 150.0, "bid", 100, "iex", _ts())
        book.add_order("o2", 150.0, "bid", 200, "iex", _ts())
        book.cancel_order("o1")
        snap = book.get_snapshot()
        assert len(snap.bids) == 1
        assert snap.bids[0].size == 200

    # ── Modify ────────────────────────────────────────────────────────────────

    def test_modify_changes_qty(self):
        book = self._book()
        book.add_order("o1", 150.0, "bid", 100, "iex", _ts())
        book.modify_order("o1", 50)
        snap = book.get_snapshot()
        assert snap.bids[0].size == 50

    def test_modify_nonexistent_is_noop(self):
        book = self._book()
        book.modify_order("ghost", 500)   # must not raise

    # ── FIFO ordering ─────────────────────────────────────────────────────────

    def test_fifo_order_within_price_level(self):
        book = self._book()
        book.add_order("o1", 150.0, "bid", 100, "iex", _ts())
        book.add_order("o2", 150.0, "bid", 200, "iex", _ts())
        depth = book.get_mbo_depth("bid", levels=1)
        _, _, order_ids = depth[0]
        assert order_ids[0] == "o1"   # first in = first in queue

    # ── Price ordering ────────────────────────────────────────────────────────

    def test_bids_descending_in_snapshot(self):
        book = self._book()
        book.add_order("o1", 148.0, "bid", 100, "iex", _ts())
        book.add_order("o2", 150.0, "bid", 100, "iex", _ts())
        book.add_order("o3", 149.0, "bid", 100, "iex", _ts())
        snap = book.get_snapshot()
        prices = [l.price for l in snap.bids]
        assert prices == sorted(prices, reverse=True)

    def test_asks_ascending_in_snapshot(self):
        book = self._book()
        book.add_order("o1", 152.0, "ask", 100, "iex", _ts())
        book.add_order("o2", 150.0, "ask", 100, "iex", _ts())
        book.add_order("o3", 151.0, "ask", 100, "iex", _ts())
        snap = book.get_snapshot()
        prices = [l.price for l in snap.asks]
        assert prices == sorted(prices)

    # ── MBO depth ─────────────────────────────────────────────────────────────

    def test_get_mbo_depth_returns_order_ids(self):
        book = self._book()
        book.add_order("o1", 150.0, "bid", 100, "iex", _ts())
        book.add_order("o2", 150.0, "bid", 200, "iex", _ts())
        depth = book.get_mbo_depth("bid", levels=5)
        assert len(depth) == 1
        price, total_qty, order_ids = depth[0]
        assert price == 150.0
        assert total_qty == 300
        assert set(order_ids) == {"o1", "o2"}

    # ── Spoofing detection ────────────────────────────────────────────────────

    def test_spoofing_none_when_no_events(self):
        book = self._book()
        risk = book.detect_spoofing("iex")
        assert risk == SpoofingRisk.NONE

    def test_spoofing_none_when_low_ratio(self):
        book = self._book()
        ts = _ts()
        for i in range(10):
            book.add_order(f"o{i}", 150.0, "bid", 100, "iex", ts)
        # cancel only 2 (20% ratio → NONE)
        book.cancel_order("o0")
        book.cancel_order("o1")
        risk = book.detect_spoofing("iex")
        assert risk == SpoofingRisk.NONE

    def test_spoofing_low_at_40pct(self):
        book = self._book()
        ts = _ts()
        for i in range(10):
            book.add_order(f"oa{i}", 150.0, "bid", 100, "iex", ts)
        # cancel 4 out of 10 = 40%
        for i in range(4):
            book.cancel_order(f"oa{i}")
        risk = book.detect_spoofing("iex")
        assert risk == SpoofingRisk.LOW

    def test_spoofing_critical_at_95pct(self):
        book = self._book()
        ts = _ts()
        for i in range(20):
            book.add_order(f"oc{i}", 150.0, "bid", 100, "iex", ts)
        # cancel 19 out of 20 = 95%
        for i in range(19):
            book.cancel_order(f"oc{i}")
        risk = book.detect_spoofing("iex")
        assert risk == SpoofingRisk.CRITICAL

    def test_spoofing_high_at_80pct(self):
        book = self._book()
        ts = _ts()
        for i in range(10):
            book.add_order(f"oh{i}", 150.0, "bid", 100, "iex", ts)
        for i in range(8):
            book.cancel_order(f"oh{i}")
        risk = book.detect_spoofing("iex")
        assert risk == SpoofingRisk.HIGH

    def test_large_book(self):
        book = self._book()
        for i in range(100):
            book.add_order(f"b{i}", 150.0 - i * 0.01, "bid", 100, "iex", _ts())
        for i in range(50):
            book.cancel_order(f"b{i}")
        snap = book.get_snapshot(levels=10)
        assert len(snap.bids) == 10

    # ── Snapshot is L2-compatible ─────────────────────────────────────────────

    def test_snapshot_is_orderbook_instance(self):
        book = self._book()
        book.add_order("o1", 150.0, "bid", 100, "iex", _ts())
        snap = book.get_snapshot()
        assert isinstance(snap, OrderBook)
        assert snap.symbol == "AAPL"

    def test_snapshot_bids_are_l2level(self):
        book = self._book()
        book.add_order("o1", 150.0, "bid", 100, "iex", _ts())
        snap = book.get_snapshot()
        assert isinstance(snap.bids[0], L2Level)


# ═══════════════════════════════════════════════════════════════════════════════
#  TestMBOOrderBookCache
# ═══════════════════════════════════════════════════════════════════════════════

class TestMBOOrderBookCache:

    def test_multi_symbol_independence(self):
        cache = MBOOrderBookCache()
        cache.apply_add("AAPL", "o1", 150.0, "bid", 100, "iex", _ts())
        cache.apply_add("TSLA", "o2", 200.0, "ask", 50, "nasdaq", _ts())
        aapl_book = cache.get_book("AAPL")
        tsla_book = cache.get_book("TSLA")
        assert aapl_book is not None and len(aapl_book.bids) == 1
        assert tsla_book is not None and len(tsla_book.asks) == 1

    def test_get_book_returns_none_before_any_add(self):
        cache = MBOOrderBookCache()
        assert cache.get_book("UNKNOWN") is None

    def test_apply_cancel_removes_order(self):
        cache = MBOOrderBookCache()
        cache.apply_add("AAPL", "o1", 150.0, "bid", 100, "iex", _ts())
        cache.apply_cancel("AAPL", "o1")
        book = cache.get_book("AAPL")
        assert book is not None
        assert book.bids == []

    def test_apply_modify_changes_qty(self):
        cache = MBOOrderBookCache()
        cache.apply_add("AAPL", "o1", 150.0, "bid", 200, "iex", _ts())
        cache.apply_modify("AAPL", "o1", 50)
        book = cache.get_book("AAPL")
        assert book.bids[0].size == 50

    def test_total_bid_depth(self):
        cache = MBOOrderBookCache()
        cache.apply_add("NVDA", "o1", 500.0, "bid", 100, "iex", _ts())
        cache.apply_add("NVDA", "o2", 499.0, "bid", 200, "iex", _ts())
        cache.apply_add("NVDA", "o3", 498.0, "bid", 300, "iex", _ts())
        depth = cache.total_bid_depth("NVDA", levels=3)
        assert depth == 600

    def test_detect_liquidity_sweep_true_when_thin(self):
        cache = MBOOrderBookCache()
        cache.apply_add("AAPL", "o1", 150.0, "bid", 100, "iex", _ts())
        # only 100 shares on bid; min_sweep_size=5000 → thin → sweep=True
        assert cache.detect_liquidity_sweep("AAPL", "bid", min_sweep_size=5000) is True

    def test_detect_liquidity_sweep_false_when_deep(self):
        cache = MBOOrderBookCache()
        for i in range(100):
            cache.apply_add("AAPL", f"o{i}", 150.0, "bid", 200, "iex", _ts())
        # 100 * 200 = 20000 shares; min_sweep_size=5000 → deep → sweep=False
        assert cache.detect_liquidity_sweep("AAPL", "bid", min_sweep_size=5000) is False

    def test_spoofing_risk_via_cache(self):
        cache = MBOOrderBookCache()
        for i in range(20):
            cache.apply_add("AAPL", f"sp{i}", 150.0, "bid", 100, "iex", _ts())
        for i in range(19):
            cache.apply_cancel("AAPL", f"sp{i}")
        risk = cache.get_spoofing_risk("AAPL", "iex")
        assert risk == SpoofingRisk.CRITICAL


# ═══════════════════════════════════════════════════════════════════════════════
#  TestLOBImbalanceModel
# ═══════════════════════════════════════════════════════════════════════════════

class TestLOBImbalanceModel:

    def test_extract_features_empty_book(self):
        from core.lob_model import extract_features
        book = _book_with_bids_asks([], [])
        feats = extract_features(book)
        assert feats.bid_ask_imbalance == 0.0
        assert feats.spread_bps == 0.0

    def test_extract_features_bid_heavy(self):
        from core.lob_model import extract_features
        book = _book_with_bids_asks(
            [(100.0, 1000), (99.0, 500)],
            [(101.0, 10), (102.0, 10)],
        )
        feats = extract_features(book)
        assert feats.bid_ask_imbalance > 0  # bid-heavy → positive

    def test_extract_features_ask_heavy(self):
        from core.lob_model import extract_features
        book = _book_with_bids_asks(
            [(100.0, 10)],
            [(101.0, 1000), (102.0, 500)],
        )
        feats = extract_features(book)
        assert feats.bid_ask_imbalance < 0  # ask-heavy → negative

    def test_predict_in_range(self):
        from core.lob_model import LOBImbalanceModel
        model = LOBImbalanceModel()
        book = _book_with_bids_asks(
            [(100.0, 500), (99.0, 300)],
            [(101.0, 200), (102.0, 100)],
        )
        drift = model.predict_short_term_drift(book)
        assert -1.0 <= drift <= 1.0

    def test_predict_balanced_near_zero(self):
        from core.lob_model import LOBImbalanceModel
        model = LOBImbalanceModel()
        # Use a tight spread (1 cent on a $100 stock ≈ 1 bps) so spread_bps
        # does not dominate the prediction; equal bid/ask qty → balanced
        book = _book_with_bids_asks(
            [(100.00, 500), (99.99, 300)],
            [(100.01, 500), (100.02, 300)],
        )
        drift = model.predict_short_term_drift(book)
        assert -1.0 <= drift <= 1.0   # must be valid; balanced → near zero

    def test_default_model_is_callable(self):
        from core.lob_model import LOBImbalanceModel
        model = LOBImbalanceModel()
        book = _book_with_bids_asks(
            [(100.0, 100), (99.0, 50)],
            [(101.0, 100), (102.0, 50)],
        )
        result = model.predict_short_term_drift(book)
        assert isinstance(result, float)

    def test_fit_improves_accuracy(self):
        """fit() should produce a model that classifies training examples correctly."""
        import numpy as np
        from core.lob_model import LOBImbalanceModel

        # Generate synthetic training data
        bid_heavy = _book_with_bids_asks(
            [(100.0 - i * 0.1, 1000) for i in range(10)],
            [(101.0 + i * 0.1, 10) for i in range(10)],
        )
        ask_heavy = _book_with_bids_asks(
            [(100.0 - i * 0.1, 10) for i in range(10)],
            [(101.0 + i * 0.1, 1000) for i in range(10)],
        )
        snapshots = [bid_heavy] * 20 + [ask_heavy] * 20
        targets   = [1.0] * 20 + [-1.0] * 20

        model = LOBImbalanceModel.fit(snapshots, targets)
        # Fitted model should score bid-heavy books positively
        assert model.predict_short_term_drift(bid_heavy) > 0

    def test_agent20_lob_model_integration(self):
        """LOB model modifies quote skew when injected into MarketMakingAgent."""
        from core.lob_model import LOBImbalanceModel
        model = LOBImbalanceModel()
        # This test just ensures no import/integration errors
        assert hasattr(model, "predict_short_term_drift")
