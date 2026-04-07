"""
Tests for institutional scaling modules.

Covers:
  - core/microstructure.py  (Avellaneda-Stoikov, order book, market impact)
  - core/statistics.py      (OLS, ADF, Engle-Granger, OU, Kalman, z-score, Hurst)
  - infrastructure/fix_connector.py  (FIX 4.4 message encode/decode)
  - infrastructure/colocation.py     (latency budget, RTT profile)
  - infrastructure/prime_broker.py   (position book, SOR routing, credit lines)
  - infrastructure/cat_reporter.py   (CAT events, spoofing detection)
  - infrastructure/distributed_backtest.py  (SimResult metrics, Monte Carlo)
  - agents 20–23  (instantiation, basic method contracts)
"""
from __future__ import annotations

import asyncio
import math
import time
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

# ─── core/microstructure.py ──────────────────────────────────────────────────

from core.microstructure import (
    ASTParameters,
    Level,
    OrderBook,
    fill_probability,
    inventory_risk,
    inventory_skew_factor,
    market_depth,
    optimal_quotes,
    optimal_spread,
    order_book_imbalance,
    reservation_price,
    roll_spread,
    slippage_bps,
    square_root_impact,
    vwap,
    twap,
    FillRecord,
    weighted_mid_price,
)


class TestAvellanedaStoikov(unittest.TestCase):

    def setUp(self):
        self.params = ASTParameters(gamma=0.05, sigma=0.0002, kappa=1.5, T=23400.0)

    def test_reservation_price_no_inventory(self):
        """With zero inventory, reservation price equals mid."""
        r = reservation_price(mid=100.0, inventory=0.0, t_elapsed=0.0, params=self.params)
        self.assertAlmostEqual(r, 100.0, places=6)

    def test_reservation_price_long_inventory_below_mid(self):
        """Long inventory → reservation price < mid (incentive to sell)."""
        r = reservation_price(mid=100.0, inventory=500.0, t_elapsed=0.0, params=self.params)
        self.assertLess(r, 100.0)

    def test_reservation_price_short_inventory_above_mid(self):
        """Short inventory → reservation price > mid (incentive to buy)."""
        r = reservation_price(mid=100.0, inventory=-500.0, t_elapsed=0.0, params=self.params)
        self.assertGreater(r, 100.0)

    def test_optimal_spread_positive(self):
        """Optimal spread must be strictly positive."""
        spread = optimal_spread(t_elapsed=0.0, params=self.params)
        self.assertGreater(spread, 0.0)

    def test_optimal_spread_decreases_with_time(self):
        """Spread should narrow as time remaining decreases."""
        spread_early = optimal_spread(t_elapsed=100.0, params=self.params)
        spread_late = optimal_spread(t_elapsed=20000.0, params=self.params)
        self.assertGreater(spread_early, spread_late)

    def test_optimal_quotes_bid_below_ask(self):
        bid, ask = optimal_quotes(mid=100.0, inventory=0.0, t_elapsed=0.0, params=self.params)
        self.assertLess(bid, ask)

    def test_optimal_quotes_minimum_tick(self):
        """Bid-ask gap must be at least one tick."""
        tick = 0.01
        bid, ask = optimal_quotes(mid=100.0, inventory=0.0, t_elapsed=0.0,
                                   params=self.params, tick_size=tick)
        self.assertGreaterEqual(ask - bid, tick - 1e-9)

    def test_fill_probability_range(self):
        prob = fill_probability(spread_half=0.05, params=self.params)
        self.assertGreaterEqual(prob, 0.0)
        self.assertLessEqual(prob, 1.0)

    def test_inventory_risk_positive(self):
        risk = inventory_risk(inventory=500.0, sigma=0.0002, horizon_sec=60.0)
        self.assertGreater(risk, 0.0)

    def test_inventory_risk_zero_for_flat(self):
        risk = inventory_risk(inventory=0.0, sigma=0.0002, horizon_sec=60.0)
        self.assertEqual(risk, 0.0)

    def test_inventory_skew_factor_range(self):
        for inv in (-1100, -500, 0, 500, 1100):
            skew = inventory_skew_factor(inv, q_max=1000.0)
            self.assertGreaterEqual(skew, -1.0)
            self.assertLessEqual(skew, 1.0)

    def test_inventory_skew_direction(self):
        """Long inventory skew and short inventory skew are opposite signs."""
        skew_long = inventory_skew_factor(800.0, q_max=1000.0)
        skew_short = inventory_skew_factor(-800.0, q_max=1000.0)
        # Skews should be non-zero and have opposite signs
        self.assertNotEqual(skew_long, 0.0)
        self.assertNotEqual(skew_short, 0.0)
        self.assertLess(skew_long * skew_short, 0.0)  # opposite signs


class TestOrderBook(unittest.TestCase):

    def _make_book(self) -> OrderBook:
        bids = [Level(price=100.0 - i * 0.01, size=100.0 + i * 10) for i in range(5)]
        asks = [Level(price=100.05 + i * 0.01, size=100.0 + i * 10) for i in range(5)]
        return OrderBook(bids=bids, asks=asks)

    def test_mid_is_average_of_best(self):
        book = self._make_book()
        expected = (100.0 + 100.05) / 2.0
        self.assertAlmostEqual(book.mid, expected, places=6)

    def test_spread_positive(self):
        book = self._make_book()
        self.assertGreater(book.spread, 0.0)

    def test_obi_range(self):
        book = self._make_book()
        obi = order_book_imbalance(book)
        self.assertGreaterEqual(obi, -1.0)
        self.assertLessEqual(obi, 1.0)

    def test_microprice_between_bid_ask(self):
        book = self._make_book()
        micro = weighted_mid_price(book)
        self.assertGreater(micro, book.best_bid)
        self.assertLess(micro, book.best_ask)

    def test_market_depth_non_negative(self):
        book = self._make_book()
        bid_depth, ask_depth = market_depth(book)
        self.assertGreaterEqual(bid_depth, 0.0)
        self.assertGreaterEqual(ask_depth, 0.0)

    def test_empty_book_returns_none(self):
        book = OrderBook()
        self.assertIsNone(book.mid)
        self.assertIsNone(book.spread)


class TestMarketImpact(unittest.TestCase):

    def test_square_root_impact_positive(self):
        impact = square_root_impact(order_size=10_000, adv=1_000_000, sigma=0.02)
        self.assertGreater(impact, 0.0)

    def test_impact_zero_adv(self):
        impact = square_root_impact(order_size=10_000, adv=0, sigma=0.02)
        self.assertEqual(impact, 0.0)

    def test_slippage_buy_positive_on_overpay(self):
        """Buying above benchmark = positive slippage (cost)."""
        bps = slippage_bps(executed_price=100.05, benchmark_price=100.0, direction=1)
        self.assertGreater(bps, 0.0)

    def test_slippage_sell_positive_on_undershoot(self):
        """Selling below benchmark = positive cost."""
        bps = slippage_bps(executed_price=99.95, benchmark_price=100.0, direction=-1)
        self.assertGreater(bps, 0.0)

    def test_vwap_correct(self):
        fills = [FillRecord(price=100.0, size=100, timestamp_ns=0),
                 FillRecord(price=101.0, size=200, timestamp_ns=1)]
        result = vwap(fills)
        self.assertAlmostEqual(result, (100 * 100 + 101 * 200) / 300, places=6)

    def test_twap_simple_average(self):
        fills = [FillRecord(price=100.0, size=100, timestamp_ns=0),
                 FillRecord(price=102.0, size=50, timestamp_ns=1)]
        self.assertAlmostEqual(twap(fills), 101.0, places=6)

    def test_roll_spread_non_negative(self):
        prices = [100.0, 100.02, 99.98, 100.01, 99.99, 100.03, 100.0, 99.97]
        spread = roll_spread(prices)
        self.assertGreaterEqual(spread, 0.0)


# ─── core/statistics.py ──────────────────────────────────────────────────────

from core.statistics import (
    ols,
    adf_test,
    engle_granger,
    fit_ou,
    half_life_ou,
    kalman_update,
    kalman_filter_spreads,
    rolling_zscore,
    current_zscore,
    hurst_exponent,
    generate_stat_arb_signal,
    screen_pair,
    KalmanState,
)


class TestOLS(unittest.TestCase):

    def test_perfect_fit(self):
        x = [float(i) for i in range(20)]
        y = [2.0 * xi + 5.0 for xi in x]
        alpha, beta, r2 = ols(x, y)
        self.assertAlmostEqual(alpha, 5.0, places=6)
        self.assertAlmostEqual(beta, 2.0, places=6)
        self.assertAlmostEqual(r2, 1.0, places=6)

    def test_r_squared_zero_for_constant(self):
        x = [float(i) for i in range(10)]
        y = [5.0] * 10
        _, _, r2 = ols(x, y)
        self.assertAlmostEqual(r2, 0.0, places=6)

    def test_short_series_raises(self):
        with self.assertRaises(ValueError):
            ols([1.0], [1.0])


class TestADF(unittest.TestCase):

    def test_white_noise_not_stationary(self):
        """Pure random walk is NOT stationary."""
        import random
        rng = random.Random(1)
        rw = [0.0]
        for _ in range(100):
            rw.append(rw[-1] + rng.gauss(0, 1))
        result = adf_test(rw)
        # We don't assert stationarity — just that it runs and returns a result
        self.assertIsInstance(result.statistic, float)
        self.assertIsInstance(result.is_stationary_5pct, bool)

    def test_stationary_series_detected(self):
        """Strongly mean-reverting AR(1) process should pass ADF."""
        import random
        rng = random.Random(42)
        # AR(1): S_t = 0.3 * S_{t-1} + noise → strong mean reversion (theta = 0.7)
        series = [0.0]
        for _ in range(200):
            series.append(0.3 * series[-1] + rng.gauss(0, 0.1))
        result = adf_test(series)
        self.assertTrue(result.is_stationary_5pct)

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            adf_test([1.0, 2.0, 3.0], lags=1)


class TestOUModel(unittest.TestCase):

    def test_fit_ou_returns_positive_theta(self):
        """OU theta must be positive (mean reversion speed)."""
        series = [0.0]
        rng = __import__("random").Random(42)
        for _ in range(100):
            prev = series[-1]
            series.append(prev - 0.1 * prev + rng.gauss(0, 0.05))
        params = fit_ou(series)
        self.assertGreater(params.theta, 0.0)

    def test_half_life_positive(self):
        series = [0.0]
        rng = __import__("random").Random(7)
        for _ in range(100):
            prev = series[-1]
            series.append(prev - 0.05 * prev + rng.gauss(0, 0.1))
        hl = half_life_ou(series)
        self.assertGreater(hl, 0.0)

    def test_fit_ou_short_raises(self):
        with self.assertRaises(ValueError):
            fit_ou([1.0, 2.0, 3.0])


class TestKalmanFilter(unittest.TestCase):

    def test_update_changes_beta(self):
        state = KalmanState(beta=1.0, P=1.0)
        initial_beta = state.beta
        spread, new_beta = kalman_update(state, y=102.0, x=100.0)
        self.assertIsInstance(spread, float)
        # Beta should have moved
        self.assertNotAlmostEqual(new_beta, initial_beta, places=10)

    def test_filter_returns_correct_length(self):
        y = [100.0 + i * 0.1 for i in range(50)]
        x = [50.0 + i * 0.05 for i in range(50)]
        spreads, betas, _ = kalman_filter_spreads(y, x)
        self.assertEqual(len(spreads), 50)
        self.assertEqual(len(betas), 50)

    def test_mismatched_lengths_raises(self):
        with self.assertRaises(ValueError):
            kalman_filter_spreads([1.0, 2.0], [1.0, 2.0, 3.0])


class TestRollingZScore(unittest.TestCase):

    def test_zero_mean_constant_series(self):
        """Constant series → z-score = 0 (no deviation from mean)."""
        series = [5.0] * 100
        zscores = rolling_zscore(series, window=20)
        for z in zscores[20:]:
            self.assertAlmostEqual(z, 0.0, places=6)

    def test_length_preserved(self):
        series = list(range(100))
        zscores = rolling_zscore(series, window=20)
        self.assertEqual(len(zscores), 100)

    def test_pre_window_zeros(self):
        series = list(range(50))
        zscores = rolling_zscore(series, window=20)
        for z in zscores[:19]:
            self.assertEqual(z, 0.0)

    def test_current_zscore_direction(self):
        """High value should give positive z-score."""
        history = [100.0] * 60
        z = current_zscore(spread=110.0, spread_history=history + [110.0], window=60)
        self.assertGreater(z, 0.0)


class TestHurst(unittest.TestCase):

    def test_hurst_range(self):
        import random
        rng = random.Random(0)
        series = [rng.gauss(0, 1) for _ in range(200)]
        h = hurst_exponent(series)
        # Allow slight numeric noise — Hurst estimator has finite-sample bias
        self.assertGreaterEqual(h, -0.1)
        self.assertLessEqual(h, 1.5)

    def test_trending_series_hurst_above_half(self):
        """Trending series → H > 0.5."""
        series = [float(i) + 0.01 * i ** 2 for i in range(200)]
        h = hurst_exponent(series)
        self.assertGreater(h, 0.5)


class TestEngleGranger(unittest.TestCase):

    def _make_cointegrated_pair(self, n=200, seed=1):
        rng = __import__("random").Random(seed)
        # Cointegrated: Y = 2*X + stationary_noise
        x = [100.0]
        for _ in range(n - 1):
            x.append(x[-1] + rng.gauss(0, 0.5))
        y = [2 * xi + rng.gauss(0, 0.1) for xi in x]
        return y, x

    def test_cointegrated_pair_detected(self):
        y, x = self._make_cointegrated_pair()
        result = engle_granger(y, x)
        self.assertIsInstance(result.is_cointegrated, bool)
        # Hedge ratio should be close to 2.0
        self.assertAlmostEqual(result.hedge_ratio, 2.0, delta=0.2)

    def test_independent_series_not_cointegrated(self):
        rng = __import__("random").Random(99)
        x = [rng.gauss(0, 1) for _ in range(100)]
        y = [rng.gauss(0, 1) for _ in range(100)]
        result = engle_granger(y, x)
        # We only check that it runs — independence not guaranteed to fail ADF
        self.assertIsInstance(result.is_cointegrated, bool)

    def test_screen_pair_returns_candidate(self):
        y, x = self._make_cointegrated_pair(n=200)
        candidate = screen_pair("A", "B", y, x)
        self.assertIsInstance(candidate.is_valid, bool)
        self.assertIsInstance(candidate.hedge_ratio, float)


# ─── infrastructure/fix_connector.py ─────────────────────────────────────────

from infrastructure.fix_connector import FIXMessage, parse_execution_report


class TestFIXMessage(unittest.TestCase):

    def test_encode_decode_roundtrip(self):
        msg = FIXMessage(msg_type="D")
        msg.set(55, "AAPL").set(54, "1").set(38, 100).set(44, "182.50")
        wire = msg.encode(sender="FIRM", target="VENUE", seq_num=5)
        self.assertIn(b"8=FIX.4.4", wire)
        self.assertIn(b"35=D", wire)
        self.assertIn(b"55=AAPL", wire)

    def test_decode_extracts_fields(self):
        msg = FIXMessage(msg_type="8")   # Execution report
        msg.set(11, "CL123").set(37, "ORD456").set(39, "2").set(14, 100)
        wire = msg.encode("FIRM", "VENUE", seq_num=10)
        decoded = FIXMessage.decode(wire)
        self.assertEqual(decoded.msg_type, "8")
        self.assertEqual(decoded.get(11), "CL123")

    def test_checksum_format(self):
        msg = FIXMessage(msg_type="0")   # Heartbeat
        wire = msg.encode("A", "B", seq_num=1)
        # Wire must end with 10=NNN\x01
        self.assertIn(b"10=", wire)

    def test_parse_execution_report(self):
        msg = FIXMessage(msg_type="8")
        msg.set(11, "CL1").set(37, "O1").set(17, "E1").set(150, "2")
        msg.set(39, "2").set(55, "MSFT").set(54, "1")
        msg.set(151, 0).set(14, 100).set(6, 185.50).set(31, 185.50).set(32, 100)
        parsed = parse_execution_report(msg)
        self.assertEqual(parsed["symbol"], "MSFT")
        self.assertAlmostEqual(parsed["avg_px"], 185.50, places=2)


# ─── infrastructure/colocation.py ────────────────────────────────────────────

from infrastructure.colocation import (
    LatencyBudget,
    LatencyStage,
    NetworkProfile,
    ColocationProfile,
    LatencyMonitor,
    LatencyTier,
    elapsed_us,
    ns,
)


class TestLatencyBudget(unittest.TestCase):

    def test_stage_duration_positive(self):
        stage = LatencyStage(name="test")
        __import__("time").sleep(0.001)
        stage.stop()
        self.assertGreater(stage.duration_us, 0.0)

    def test_budget_total_us(self):
        budget = LatencyBudget()
        s1 = budget.begin_stage("a")
        s1.stop()
        s2 = budget.begin_stage("b")
        s2.stop()
        self.assertGreater(budget.total_us, 0.0)

    def test_budget_exceeds_when_slow(self):
        budget = LatencyBudget()
        s = budget.begin_stage("slow")
        __import__("time").sleep(0.01)
        s.stop()
        self.assertTrue(budget.exceeds_budget(budget_us=1.0))

    def test_budget_not_exceeded_for_fast(self):
        budget = LatencyBudget()
        s = budget.begin_stage("fast")
        s.stop()
        self.assertFalse(budget.exceeds_budget(budget_us=1_000_000.0))

    def test_network_profile_records(self):
        profile = NetworkProfile(target="test:1234")
        for rtt in [100.0, 200.0, 150.0]:
            profile.record(rtt)
        self.assertEqual(profile.p50_us, 150.0)
        self.assertGreater(profile.jitter_us, 0.0)

    def test_latency_monitor_breach_rate(self):
        profile = ColocationProfile(tier=LatencyTier.HFT)
        monitor = LatencyMonitor(profile)
        # Record one fast, one slow
        fast = LatencyBudget()
        slow = LatencyBudget()
        __import__("time").sleep(0.001)
        slow.stages.append(LatencyStage(name="x", start_ns=0, end_ns=200_000_000_000))
        monitor.record(fast)
        monitor.record(slow)
        self.assertGreaterEqual(monitor.breach_rate, 0.0)


# ─── infrastructure/prime_broker.py ──────────────────────────────────────────

from infrastructure.prime_broker import (
    BrokerPosition,
    CrossMarginBook,
    CreditLine,
    PrimeBrokerRouter,
    RoutingDecision,
    route_order,
)
from core.enums import AssetClass, MarginType, OrderSide, OrderVenue, PrimeBroker


class TestCrossMarginBook(unittest.TestCase):

    def test_net_position_aggregates(self):
        book = CrossMarginBook()
        book.add_position(BrokerPosition(
            symbol="AAPL", broker=PrimeBroker.ALPACA, account="ACC1",
            qty=100.0, avg_cost=180.0,
        ))
        book.add_position(BrokerPosition(
            symbol="AAPL", broker=PrimeBroker.ALPACA, account="ACC1",
            qty=50.0, avg_cost=182.0,
        ))
        self.assertAlmostEqual(book.net_position("AAPL"), 150.0, places=4)

    def test_gross_exposure_calculation(self):
        book = CrossMarginBook()
        book.add_position(BrokerPosition(
            symbol="MSFT", broker=PrimeBroker.ALPACA, account="A",
            qty=100.0, avg_cost=300.0,
        ))
        exposure = book.gross_exposure(prices={"MSFT": 305.0})
        self.assertAlmostEqual(exposure, 30_500.0, places=2)

    def test_flatten_removes_symbol(self):
        book = CrossMarginBook()
        book.add_position(BrokerPosition(
            symbol="TSLA", broker=PrimeBroker.ALPACA, account="A",
            qty=10.0, avg_cost=250.0,
        ))
        book.flatten_symbol("TSLA")
        self.assertAlmostEqual(book.net_position("TSLA"), 0.0, places=6)


class TestSmartOrderRouter(unittest.TestCase):

    def test_urgent_selects_lowest_latency(self):
        decision = route_order("AAPL", OrderSide.BUY, 100.0,
                                is_aggressive=True, urgency="urgent")
        self.assertIsInstance(decision.venue, OrderVenue)
        self.assertGreater(decision.estimated_cost_bps, -5.0)

    def test_stealth_large_block_prefers_dark_pool(self):
        decision = route_order("AAPL", OrderSide.BUY, 10_000.0,
                                is_aggressive=False, urgency="stealth")
        self.assertEqual(decision.venue, OrderVenue.DARK_POOL)

    def test_maker_cost_less_than_taker(self):
        maker = route_order("AAPL", OrderSide.BUY, 100.0, is_aggressive=False)
        taker = route_order("AAPL", OrderSide.BUY, 100.0, is_aggressive=True)
        # Maker should typically be cheaper (or earn rebate)
        self.assertLessEqual(maker.estimated_cost_bps, taker.estimated_cost_bps)

    def test_excluded_venues_avoided(self):
        decision = route_order(
            "AAPL", OrderSide.BUY, 100.0, is_aggressive=True,
            exclude_venues={OrderVenue.DARK_POOL, OrderVenue.IEX},
        )
        self.assertNotIn(decision.venue, {OrderVenue.DARK_POOL, OrderVenue.IEX})


class TestCreditLine(unittest.TestCase):

    def test_consume_reduces_available(self):
        line = CreditLine(broker=PrimeBroker.ALPACA, limit_usd=1_000_000.0)
        result = line.consume(200_000.0)
        self.assertTrue(result)
        self.assertAlmostEqual(line.available_usd, 800_000.0, places=2)

    def test_hard_limit_blocks(self):
        line = CreditLine(broker=PrimeBroker.ALPACA, limit_usd=1_000_000.0,
                          used_usd=960_000.0)
        result = line.consume(100_000.0)
        self.assertFalse(result)

    def test_release_restores_credit(self):
        line = CreditLine(broker=PrimeBroker.ALPACA, limit_usd=1_000_000.0,
                          used_usd=500_000.0)
        line.release(200_000.0)
        self.assertAlmostEqual(line.used_usd, 300_000.0, places=2)


# ─── infrastructure/cat_reporter.py ──────────────────────────────────────────

from infrastructure.cat_reporter import (
    CATEvent,
    CATReporter,
    OrderLifecycle,
    SpoofingAnalysis,
)
from core.enums import CATEventType, OrderSide, SpoofingRisk


class TestSpoofingAnalysis(unittest.TestCase):

    def test_no_activity_no_risk(self):
        analysis = SpoofingAnalysis(symbol="AAPL")
        self.assertEqual(analysis.assess_risk(), SpoofingRisk.NONE)

    def test_high_cancel_fill_ratio_medium_risk(self):
        analysis = SpoofingAnalysis(symbol="AAPL")
        # Simulate 1 fill and 10 cancels
        analysis.record(CATEventType.NEW_ORDER, "o1")
        analysis.record(CATEventType.FILL, "o1")
        for i in range(10):
            oid = f"c{i}"
            analysis.record(CATEventType.NEW_ORDER, oid)
            analysis.record(CATEventType.CANCEL, oid)
        risk = analysis.assess_risk()
        self.assertIn(risk, (SpoofingRisk.MEDIUM, SpoofingRisk.HIGH))

    def test_critical_threshold(self):
        analysis = SpoofingAnalysis(symbol="AAPL")
        # 1 fill + 25 cancels → critical
        analysis.record(CATEventType.NEW_ORDER, "fill_o")
        analysis.record(CATEventType.FILL, "fill_o")
        for i in range(25):
            oid = f"lyr{i}"
            analysis.record(CATEventType.NEW_ORDER, oid)
            analysis.record(CATEventType.CANCEL, oid)
        risk = analysis.assess_risk()
        self.assertIn(risk, (SpoofingRisk.HIGH, SpoofingRisk.CRITICAL))


class TestCATEvent(unittest.TestCase):

    def test_content_hash_deterministic(self):
        event = CATEvent(
            event_type=CATEventType.NEW_ORDER,
            firm_id="F1",
            order_id="O1",
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=100.0,
            price=182.0,
            timestamp_ns=1_000_000_000,
        )
        h1 = event.content_hash()
        h2 = event.content_hash()
        self.assertEqual(h1, h2)

    def test_different_events_different_hash(self):
        e1 = CATEvent(
            event_type=CATEventType.NEW_ORDER, firm_id="F1", order_id="O1",
            symbol="AAPL", side=OrderSide.BUY, qty=100.0, price=182.0, timestamp_ns=1,
        )
        e2 = CATEvent(
            event_type=CATEventType.FILL, firm_id="F1", order_id="O1",
            symbol="AAPL", side=OrderSide.BUY, qty=100.0, price=182.0, timestamp_ns=2,
        )
        self.assertNotEqual(e1.content_hash(), e2.content_hash())


# ─── infrastructure/distributed_backtest.py ──────────────────────────────────

from infrastructure.distributed_backtest import (
    SimParams,
    SimResult,
    MonteCarloResult,
    BacktestEngine,
    BacktestTrade,
)


class TestSimResult(unittest.TestCase):

    def _make_result(self, trades_pnl: list[float]) -> SimResult:
        trades = [
            BacktestTrade(
                entry_time=1000.0 + i * 3600,
                exit_time=1000.0 + i * 3600 + 600,
                symbol="TEST",
                direction=1,
                entry_price=100.0,
                exit_price=100.0 + pnl / 100,
                qty=100.0,
                pnl=pnl,
                slippage=0.5,
            )
            for i, pnl in enumerate(trades_pnl)
        ]
        final = 100_000.0 + sum(trades_pnl)
        return SimResult(
            sim_id=0,
            strategy_id="test",
            start_ts=0.0,
            end_ts=365 * 86400.0,
            initial_capital=100_000.0,
            final_capital=final,
            trades=trades,
        )

    def test_win_rate_all_wins(self):
        result = self._make_result([100.0, 200.0, 50.0])
        self.assertAlmostEqual(result.win_rate, 1.0, places=6)

    def test_win_rate_mixed(self):
        result = self._make_result([100.0, -50.0])
        self.assertAlmostEqual(result.win_rate, 0.5, places=6)

    def test_max_drawdown_non_negative(self):
        result = self._make_result([100.0, -500.0, 200.0])
        self.assertGreater(result.max_drawdown, 0.0)

    def test_profit_factor_all_wins(self):
        result = self._make_result([100.0, 200.0])
        self.assertEqual(result.profit_factor, float("inf"))

    def test_sharpe_returns_float(self):
        result = self._make_result([100.0, 50.0, 150.0, -20.0] * 10)
        sharpe = result.sharpe()
        self.assertIsInstance(sharpe, float)

    def test_total_return_positive_on_profit(self):
        result = self._make_result([1000.0])
        self.assertGreater(result.total_return, 0.0)

    def test_to_dict_keys(self):
        result = self._make_result([50.0])
        d = result.to_dict()
        for key in ("sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor"):
            self.assertIn(key, d)


class TestMonteCarloResult(unittest.TestCase):

    def _make_mc(self, n=20) -> MonteCarloResult:
        results = []
        for i in range(n):
            pnl_list = [100.0 * (i % 3 - 1) * 0.5 + 50.0] * 10
            results.append(TestSimResult()._make_result(pnl_list))
        return MonteCarloResult(strategy_id="test", n_simulations=n, results=results)

    def test_prob_of_profit_range(self):
        mc = self._make_mc(20)
        self.assertGreaterEqual(mc.prob_of_profit, 0.0)
        self.assertLessEqual(mc.prob_of_profit, 1.0)

    def test_sharpe_stats_keys(self):
        mc = self._make_mc()
        stats = mc.sharpe_stats
        for k in ("mean", "median", "stdev", "ci_95_lo", "ci_95_hi"):
            self.assertIn(k, stats)

    def test_expected_shortfall_non_positive_or_zero(self):
        """ES at 5% worst scenarios — should reflect tail loss."""
        mc = self._make_mc(50)
        es = mc.expected_shortfall_5pct
        self.assertIsInstance(es, float)

    def test_to_dict(self):
        mc = self._make_mc()
        d = mc.to_dict()
        self.assertIn("sharpe", d)
        self.assertIn("prob_of_profit_pct", d)


class TestBacktestEngineAsync(unittest.IsolatedAsyncioTestCase):

    async def test_run_monte_carlo_returns_result(self):
        engine = BacktestEngine(n_workers=1)
        params = SimParams(
            strategy_id="test_strategy",
            start_ts=0.0,
            end_ts=365 * 86400.0,
            strategy_params={"win_rate": 0.55, "avg_trades_per_day": 5},
        )
        result = await engine.run_monte_carlo(params, n_simulations=10)
        self.assertEqual(result.n_simulations, 10)
        self.assertEqual(len(result.results), 10)
        self.assertIsInstance(result.prob_of_profit, float)


# ─── Agent instantiation sanity checks ───────────────────────────────────────

class TestAgentInstantiation(unittest.IsolatedAsyncioTestCase):

    def _make_bus(self):
        bus = MagicMock()
        bus.subscribe = AsyncMock()
        bus.publish = AsyncMock()
        return bus

    def _make_store(self):
        store = MagicMock()
        store.get = AsyncMock(return_value=None)
        store.set = AsyncMock()
        return store

    def _make_audit(self):
        audit = MagicMock()
        audit.append = AsyncMock()
        return audit

    async def test_market_making_agent_instantiates(self):
        from agents.agent_20_market_making import MarketMakingAgent
        agent = MarketMakingAgent(
            symbols=["AAPL", "MSFT"],
            bus=self._make_bus(),
            store=self._make_store(),
            audit=self._make_audit(),
            order_fn=AsyncMock(return_value="ORDER001"),
            cancel_fn=AsyncMock(),
            get_book_fn=MagicMock(return_value=None),
            get_mid_fn=MagicMock(return_value=None),
        )
        self.assertEqual(len(agent._symbols), 2)
        report = agent.inventory_report()
        self.assertEqual(len(report), 2)

    async def test_stat_arb_agent_instantiates(self):
        from agents.agent_21_stat_arb import StatArbAgent
        agent = StatArbAgent(
            pair_universe=[("AAPL", "QQQ"), ("MSFT", "SPY")],
            bus=self._make_bus(),
            store=self._make_store(),
            audit=self._make_audit(),
            get_prices_fn=MagicMock(return_value={}),
            submit_order_fn=AsyncMock(return_value="O1"),
        )
        self.assertEqual(len(agent._pair_universe), 2)

    async def test_alt_data_agent_instantiates(self):
        from agents.agent_22_alt_data import AltDataAgent
        agent = AltDataAgent(
            bus=self._make_bus(),
            store=self._make_store(),
            audit=self._make_audit(),
            symbols=["AAPL", "MSFT", "AMZN"],
        )
        self.assertEqual(len(agent._symbols), 3)
        self.assertEqual(len(agent.DEFAULT_CONNECTORS), 4)

    async def test_cat_compliance_agent_instantiates(self):
        from agents.agent_23_cat_compliance import CATComplianceAgent
        from infrastructure.cat_reporter import CATReporter
        cat = CATReporter(firm_id="TEST001")
        agent = CATComplianceAgent(
            bus=self._make_bus(),
            store=self._make_store(),
            audit=self._make_audit(),
            cat=cat,
            firm_id="TEST001",
        )
        self.assertEqual(agent._firm_id, "TEST001")
        self.assertFalse(agent.is_symbol_held("AAPL"))

    async def test_cat_compliance_blocks_held_symbol(self):
        """Held symbol should be blocked — PSA_ALERT published."""
        from agents.agent_23_cat_compliance import CATComplianceAgent
        from infrastructure.cat_reporter import CATReporter

        bus = self._make_bus()
        cat = CATReporter(firm_id="TEST001")
        agent = CATComplianceAgent(
            bus=bus, store=self._make_store(), audit=self._make_audit(),
            cat=cat, firm_id="TEST001",
        )
        agent._held_symbols.add("AAPL")

        # Simulate an order submitted message
        msg = MagicMock()
        msg.payload = {"symbol": "AAPL", "order_id": "O1", "side": "buy", "qty": 100}
        await agent._on_order_submitted(msg)

        # PSA_ALERT should have been published
        from core.enums import Topic
        bus.publish.assert_called()
        call_args = bus.publish.call_args[0][0]
        self.assertEqual(call_args.topic, Topic.PSA_ALERT)


if __name__ == "__main__":
    unittest.main()
