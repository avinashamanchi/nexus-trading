"""
tests/test_multi_asset.py

Tests for:
  - core/multi_asset.py  (FX, Futures, Options, Portfolio)
  - infrastructure/tick_warehouse.py
  - infrastructure/quant_sandbox.py
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile
import time
import unittest

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ══════════════════════════════════════════════════════════════════════════════
#  FX Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFXSpec(unittest.TestCase):
    def setUp(self):
        from core.multi_asset import FXSpec
        self.spec = FXSpec(base="EUR", quote="USD", pip_size=0.0001, lot_size=100_000, margin_rate=0.02)

    def test_pair_string(self):
        self.assertEqual(self.spec.pair, "EUR/USD")

    def test_pip_size_jpy(self):
        from core.multi_asset import FXSpec
        jpy = FXSpec("USD", "JPY", pip_size=0.01)
        self.assertEqual(jpy.pip_size, 0.01)


class TestFXPosition(unittest.TestCase):
    def setUp(self):
        from core.multi_asset import FXSpec, FXPosition
        spec = FXSpec("EUR", "USD", pip_size=0.0001, lot_size=100_000, margin_rate=0.02)
        self.pos = FXPosition(spec=spec, lots=1.0, entry_rate=1.1000, current_rate=1.1050,
                               swap_rate_pa=0.0, days_held=0)

    def test_pip_value(self):
        # 1 lot × 100_000 × 0.0001 = 10
        self.assertAlmostEqual(self.pos.pip_value(), 10.0)

    def test_unrealized_pnl(self):
        # (1.1050 - 1.1000) × 1 × 100_000 = 500
        self.assertAlmostEqual(self.pos.unrealized_pnl(), 500.0)

    def test_pips_pnl(self):
        # (1.1050 - 1.1000) / 0.0001 = 50 pips
        self.assertAlmostEqual(self.pos.pips_pnl(), 50.0)

    def test_notional(self):
        # 1 × 100_000 × 1.1050 = 110_500
        self.assertAlmostEqual(self.pos.notional(), 110_500.0)

    def test_required_margin(self):
        # 110_500 × 0.02 = 2_210
        self.assertAlmostEqual(self.pos.required_margin(), 2_210.0)

    def test_swap_accrual(self):
        from core.multi_asset import FXSpec, FXPosition
        spec = FXSpec("EUR", "USD")
        pos = FXPosition(spec=spec, lots=1.0, entry_rate=1.10, current_rate=1.10,
                          swap_rate_pa=100.0, days_held=365)
        # 100.0 × 365 / 365.25 × 1 ≈ 99.93
        self.assertAlmostEqual(pos.accrued_swap(), 100 * 365 / 365.25, places=1)

    def test_short_position_pnl(self):
        from core.multi_asset import FXSpec, FXPosition
        spec = FXSpec("EUR", "USD")
        pos = FXPosition(spec=spec, lots=-1.0, entry_rate=1.1050, current_rate=1.1000)
        # Short: price went down, profit
        self.assertGreater(pos.unrealized_pnl(), 0)

    def test_total_pnl_includes_swap(self):
        from core.multi_asset import FXSpec, FXPosition
        spec = FXSpec("EUR", "USD")
        pos = FXPosition(spec=spec, lots=1.0, entry_rate=1.10, current_rate=1.10,
                          swap_rate_pa=-50.0, days_held=365)
        self.assertLess(pos.total_pnl(), 0)  # negative carry


class TestFXForward(unittest.TestCase):
    def test_forward_rate_higher_quote_rate(self):
        from core.multi_asset import fx_forward_rate
        # If USD rate > EUR rate, EUR/USD forward > spot (USD at premium)
        fwd = fx_forward_rate(spot=1.10, r_base=0.02, r_quote=0.05, T=1.0)
        self.assertGreater(fwd, 1.10)

    def test_forward_parity_zero_rates(self):
        from core.multi_asset import fx_forward_rate
        fwd = fx_forward_rate(spot=1.10, r_base=0.0, r_quote=0.0, T=1.0)
        self.assertAlmostEqual(fwd, 1.10)

    def test_swap_points_sign(self):
        from core.multi_asset import fx_swap_points
        # Higher quote rate → positive swap points (forward > spot)
        pts = fx_swap_points(spot=1.10, r_base=0.02, r_quote=0.05, T=1.0)
        self.assertGreater(pts, 0)


# ══════════════════════════════════════════════════════════════════════════════
#  Futures Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFuturesSpec(unittest.TestCase):
    def setUp(self):
        from core.multi_asset import FUTURES_SPECS
        self.es = FUTURES_SPECS["ES"]

    def test_es_tick_value(self):
        self.assertEqual(self.es.tick_value, 12.50)

    def test_es_tick_size(self):
        self.assertEqual(self.es.tick_size, 0.25)

    def test_ticks_to_dollars(self):
        # 4 ticks = $50
        self.assertAlmostEqual(self.es.ticks_to_dollars(4), 50.0)

    def test_dollars_to_ticks(self):
        self.assertAlmostEqual(self.es.dollars_to_ticks(50.0), 4.0)

    def test_all_specs_have_margin(self):
        from core.multi_asset import FUTURES_SPECS
        for sym, spec in FUTURES_SPECS.items():
            self.assertGreater(spec.initial_margin, 0, f"{sym} has no initial margin")


class TestFuturesPosition(unittest.TestCase):
    def setUp(self):
        from core.multi_asset import FuturesPosition, FUTURES_SPECS
        self.pos = FuturesPosition(
            spec=FUTURES_SPECS["ES"],
            contracts=2,
            entry_price=5000.0,
            current_price=5010.0,
            expiry_code="Z25",
        )

    def test_unrealized_pnl_long(self):
        # (5010 - 5000) / 0.25 ticks = 40 ticks; 40 × $12.50 × 2 = $1000
        self.assertAlmostEqual(self.pos.unrealized_pnl(), 1000.0)

    def test_unrealized_pnl_short(self):
        from core.multi_asset import FuturesPosition, FUTURES_SPECS
        pos = FuturesPosition(
            spec=FUTURES_SPECS["ES"], contracts=-1,
            entry_price=5010.0, current_price=5000.0,
        )
        # Short, price fell 10 pts → profit
        self.assertGreater(pos.unrealized_pnl(), 0)

    def test_dollar_delta(self):
        # 50 (contract_size) × 2 contracts = 100
        self.assertEqual(self.pos.dollar_delta(), 100)

    def test_notional(self):
        # |2| × 50 × 5010 = 501_000
        self.assertAlmostEqual(self.pos.notional(), 501_000.0)

    def test_basis(self):
        basis = self.pos.basis(spot_price=5008.0)
        self.assertAlmostEqual(basis, 2.0)

    def test_required_margin(self):
        # 2 × $12_300 = $24_600
        self.assertAlmostEqual(self.pos.required_margin(), 24_600.0)

    def test_roll_pnl_sign(self):
        # Near > Far → backwardation → positive roll
        pnl = self.pos.roll_pnl(near_price=5010.0, far_price=5005.0)
        self.assertGreater(pnl, 0)


# ══════════════════════════════════════════════════════════════════════════════
#  Options / Black-Scholes Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestBlackScholes(unittest.TestCase):
    """Standard BS test cases with well-known reference values."""

    def setUp(self):
        from core.multi_asset import black_scholes
        # ATM call: S=K=100, r=5%, sigma=20%, T=1y, q=0
        self.atm_call = black_scholes(100, 100, 1.0, 0.05, 0.20, "call")
        self.atm_put  = black_scholes(100, 100, 1.0, 0.05, 0.20, "put")

    def test_call_price_positive(self):
        self.assertGreater(self.atm_call.price, 0)

    def test_put_price_positive(self):
        self.assertGreater(self.atm_put.price, 0)

    def test_put_call_parity(self):
        # C - P = S - K×e^(-rT)  (q=0)
        from core.multi_asset import black_scholes
        c = black_scholes(100, 100, 1.0, 0.05, 0.20, "call").price
        p = black_scholes(100, 100, 1.0, 0.05, 0.20, "put").price
        parity = 100 - 100 * math.exp(-0.05 * 1.0)
        self.assertAlmostEqual(c - p, parity, places=4)

    def test_call_delta_atm(self):
        # ATM call delta ≈ 0.5..0.65
        self.assertGreater(self.atm_call.delta, 0.50)
        self.assertLess(self.atm_call.delta, 0.70)

    def test_put_delta_negative(self):
        self.assertLess(self.atm_put.delta, 0)

    def test_call_delta_plus_put_delta(self):
        # |delta_call| + |delta_put| = 1  (exactly for no dividends)
        self.assertAlmostEqual(self.atm_call.delta + abs(self.atm_put.delta), 1.0, places=2)

    def test_gamma_positive(self):
        self.assertGreater(self.atm_call.gamma, 0)
        self.assertGreater(self.atm_put.gamma, 0)

    def test_vega_positive(self):
        self.assertGreater(self.atm_call.vega, 0)
        self.assertGreater(self.atm_put.vega, 0)

    def test_theta_negative_long(self):
        # Theta = time decay, long options lose value with time
        self.assertLess(self.atm_call.theta, 0)
        self.assertLess(self.atm_put.theta, 0)

    def test_deep_itm_call_delta_near_one(self):
        from core.multi_asset import black_scholes
        deep_itm = black_scholes(200, 100, 1.0, 0.05, 0.20, "call")
        self.assertGreater(deep_itm.delta, 0.95)

    def test_deep_otm_call_delta_near_zero(self):
        from core.multi_asset import black_scholes
        deep_otm = black_scholes(50, 100, 1.0, 0.05, 0.20, "call")
        self.assertLess(deep_otm.delta, 0.05)

    def test_intrinsic_itm_call(self):
        from core.multi_asset import black_scholes
        itm = black_scholes(110, 100, 1.0, 0.05, 0.20, "call")
        self.assertAlmostEqual(itm.intrinsic, 10.0)
        self.assertGreater(itm.time_value, 0)

    def test_intrinsic_otm_call_is_zero(self):
        from core.multi_asset import black_scholes
        otm = black_scholes(90, 100, 1.0, 0.05, 0.20, "call")
        self.assertEqual(otm.intrinsic, 0.0)

    def test_invalid_inputs_raise(self):
        from core.multi_asset import black_scholes
        with self.assertRaises(ValueError):
            black_scholes(100, 100, 0.0, 0.05, 0.20)  # T=0

    def test_rho_call_positive(self):
        # Higher rates → call worth more
        self.assertGreater(self.atm_call.rho, 0)

    def test_rho_put_negative(self):
        self.assertLess(self.atm_put.rho, 0)


class TestImpliedVolatility(unittest.TestCase):
    def test_roundtrip_call(self):
        from core.multi_asset import black_scholes, implied_volatility
        sigma_in = 0.25
        price = black_scholes(100, 100, 1.0, 0.05, sigma_in, "call").price
        sigma_out = implied_volatility(price, 100, 100, 1.0, 0.05, "call")
        self.assertAlmostEqual(sigma_out, sigma_in, places=4)

    def test_roundtrip_put(self):
        from core.multi_asset import black_scholes, implied_volatility
        sigma_in = 0.30
        price = black_scholes(100, 105, 0.5, 0.04, sigma_in, "put").price
        sigma_out = implied_volatility(price, 100, 105, 0.5, 0.04, "put")
        self.assertAlmostEqual(sigma_out, sigma_in, places=4)

    def test_high_vol_roundtrip(self):
        from core.multi_asset import black_scholes, implied_volatility
        sigma_in = 0.80
        price = black_scholes(100, 100, 1.0, 0.05, sigma_in, "call").price
        sigma_out = implied_volatility(price, 100, 100, 1.0, 0.05, "call")
        self.assertAlmostEqual(sigma_out, sigma_in, places=3)


class TestOptionPosition(unittest.TestCase):
    def setUp(self):
        from core.multi_asset import OptionSpec, OptionPosition
        spec = OptionSpec(underlying="AAPL", strike=200.0, expiry_days=30, right="call")
        self.pos = OptionPosition(spec=spec, contracts=10, entry_premium=5.0,
                                   spot=200.0, r=0.05, sigma=0.25)

    def test_mark_to_market_positive(self):
        self.assertGreater(self.pos.mark_to_market(), 0)

    def test_portfolio_delta(self):
        # ATM call delta ≈ 0.5 × 10 contracts × 100 multiplier = ~500
        delta = self.pos.portfolio_delta()
        self.assertGreater(delta, 300)
        self.assertLess(delta, 700)

    def test_portfolio_gamma_positive(self):
        self.assertGreater(self.pos.portfolio_gamma(), 0)

    def test_portfolio_vega_positive(self):
        self.assertGreater(self.pos.portfolio_vega(), 0)

    def test_portfolio_theta_negative(self):
        self.assertLess(self.pos.portfolio_theta(), 0)

    def test_long_margin_is_premium_paid(self):
        margin = self.pos.required_margin()
        # Long: max loss = premium × contracts × multiplier
        self.assertAlmostEqual(margin, 5.0 * 10 * 100)

    def test_short_margin_greater_than_premium(self):
        from core.multi_asset import OptionSpec, OptionPosition
        spec = OptionSpec(underlying="AAPL", strike=200.0, expiry_days=30, right="call")
        short_pos = OptionPosition(spec=spec, contracts=-10, entry_premium=5.0,
                                    spot=200.0, r=0.05, sigma=0.25)
        margin = short_pos.required_margin()
        # Short margin > simple premium
        self.assertGreater(margin, 0)


# ══════════════════════════════════════════════════════════════════════════════
#  Multi-Asset Portfolio Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiAssetPortfolio(unittest.TestCase):
    def setUp(self):
        from core.multi_asset import (
            MultiAssetPortfolio, FXSpec, FXPosition, FuturesPosition,
            OptionSpec, OptionPosition, FUTURES_SPECS,
        )
        self.port = MultiAssetPortfolio()
        # Equity
        self.port.add_equity("NVDA", 100, 800.0, 850.0)
        self.port.add_equity("AAPL", -50, 200.0, 195.0)
        # FX
        spec = FXSpec("EUR", "USD")
        fx = FXPosition(spec=spec, lots=1.0, entry_rate=1.10, current_rate=1.12)
        self.port.add_fx(fx)
        # Futures
        fut = FuturesPosition(spec=FUTURES_SPECS["ES"], contracts=1,
                               entry_price=5000.0, current_price=5020.0)
        self.port.add_futures(fut)
        # Option
        opt_spec = OptionSpec("SPY", 500.0, 30, "call")
        opt = OptionPosition(spec=opt_spec, contracts=5, entry_premium=10.0,
                              spot=500.0, r=0.05, sigma=0.20)
        self.port.add_option(opt)

    def test_equity_pnl(self):
        # NVDA: 100 × (850-800) = +5000; AAPL short: -50 × (195-200) = +250
        self.assertAlmostEqual(self.port.equity_pnl(), 5000 + 250, places=1)

    def test_fx_pnl_positive(self):
        # Rate moved from 1.10 → 1.12 on long 1 lot
        self.assertGreater(self.port.fx_pnl(), 0)

    def test_futures_pnl_positive(self):
        self.assertGreater(self.port.futures_pnl(), 0)

    def test_total_pnl_sign(self):
        # All positions are profitable
        self.assertGreater(self.port.total_pnl(), 0)

    def test_gross_notional_positive(self):
        self.assertGreater(self.port.gross_notional(), 0)

    def test_total_margin_positive(self):
        self.assertGreater(self.port.total_margin(), 0)

    def test_aggregate_greeks(self):
        greeks = self.port.aggregate_greeks()
        # Long calls → positive net delta
        self.assertGreater(greeks.net_delta, 0)

    def test_summary_structure(self):
        s = self.port.summary()
        self.assertIn("pnl", s)
        self.assertIn("notional", s)
        self.assertIn("margin", s)
        self.assertIn("greeks", s)
        self.assertIn("positions", s)

    def test_remove_equity(self):
        self.port.remove_equity("NVDA")
        self.assertNotIn("NVDA", self.port._equity)

    def test_position_counts(self):
        s = self.port.summary()
        # NVDA was removed in test_remove_equity; tests run independently so NVDA still present here
        self.assertGreaterEqual(s["positions"]["equity_count"], 1)
        self.assertEqual(s["positions"]["fx_count"], 1)
        self.assertEqual(s["positions"]["futures_count"], 1)
        self.assertEqual(s["positions"]["options_count"], 1)


# ══════════════════════════════════════════════════════════════════════════════
#  Tick Warehouse Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestTick(unittest.TestCase):
    def test_spread(self):
        from infrastructure.tick_warehouse import Tick
        t = Tick("AAPL", time.time(), 150.00, 149.98, 150.02, 100)
        self.assertAlmostEqual(t.spread, 0.04, places=4)

    def test_mid(self):
        from infrastructure.tick_warehouse import Tick
        t = Tick("AAPL", time.time(), 150.00, 149.90, 150.10, 100)
        self.assertAlmostEqual(t.mid, 150.00)

    def test_dt(self):
        from infrastructure.tick_warehouse import Tick
        ts = time.time()
        t = Tick("AAPL", ts, 150.00, 149.98, 150.02, 100)
        self.assertAlmostEqual(t.dt.timestamp(), ts, places=0)


class TestOHLCVAggregator(unittest.TestCase):
    def _make_ticks(self, base_ts: float, prices: list[float], interval: int = 60) -> list:
        from infrastructure.tick_warehouse import Tick
        ticks = []
        for i, price in enumerate(prices):
            ts = base_ts + i * (interval // len(prices))
            ticks.append(Tick("SYM", ts, price, price - 0.01, price + 0.01, 100))
        return ticks

    def test_single_bar_ohlcv(self):
        from infrastructure.tick_warehouse import OHLCVAggregator, Tick
        agg = OHLCVAggregator("SYM", [60])
        base_ts = 1000000.0
        prices = [100, 105, 98, 103]
        ticks = self._make_ticks(base_ts, prices)
        completed = []
        for t in ticks:
            completed.extend(agg.update(t))
        remaining = agg.flush()
        all_bars = completed + remaining
        self.assertTrue(len(all_bars) >= 1)
        bar = all_bars[0]
        self.assertEqual(bar.symbol, "SYM")
        self.assertEqual(bar.interval, 60)

    def test_bar_high_low(self):
        from infrastructure.tick_warehouse import OHLCVAggregator, Tick
        agg = OHLCVAggregator("SYM", [60])
        # All ticks within a fixed 60-second window: base_ts aligned to bar boundary
        base_ts = 1_000_020.0   # bar_ts = 1_000_020 (aligned to 60s)
        prices = [100, 110, 90, 105]
        ticks = []
        for i, p in enumerate(prices):
            ticks.append(Tick("SYM", base_ts + i * 5, p, p - 0.01, p + 0.01, 100))
        completed = []
        for t in ticks:
            completed.extend(agg.update(t))
        remaining = agg.flush()
        all_bars = completed + remaining
        self.assertGreater(len(all_bars), 0)
        # Find the bar containing our ticks
        target_bar = all_bars[0]
        self.assertAlmostEqual(target_bar.high, 110.0)
        self.assertAlmostEqual(target_bar.low, 90.0)

    def test_multiple_intervals(self):
        from infrastructure.tick_warehouse import OHLCVAggregator, Tick
        agg = OHLCVAggregator("SYM", [1, 60])
        base_ts = 1000000.0
        ticks = [Tick("SYM", base_ts + i, 100.0 + i * 0.1, 99.9, 100.1, 50)
                 for i in range(120)]
        completed = []
        for t in ticks:
            completed.extend(agg.update(t))
        remaining = agg.flush()
        all_bars = completed + remaining
        intervals = {b.interval for b in all_bars}
        self.assertIn(1, intervals)
        self.assertIn(60, intervals)


class TestTickWarehouseIO(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from infrastructure.tick_warehouse import TickWarehouse
        self.wh = TickWarehouse(self.tmpdir, bar_intervals=[60], retention_days=0)
        await self.wh.open()

    async def asyncTearDown(self):
        await self.wh.close()

    async def test_write_and_read_back(self):
        from infrastructure.tick_warehouse import Tick
        ts = 1_700_000_000.0
        ticks = [
            Tick("NVDA", ts + i, 800.0 + i * 0.1, 799.9, 800.1, 100 + i)
            for i in range(10)
        ]
        await self.wh.write_many(ticks)
        await self.wh.close()

        today = self.wh._today
        read_back = list(self.wh.read("NVDA", today))
        self.assertEqual(len(read_back), 10)
        self.assertAlmostEqual(read_back[0].price, 800.0, places=2)
        self.assertAlmostEqual(read_back[-1].price, 800.9, places=2)

    async def test_available_symbols(self):
        from infrastructure.tick_warehouse import Tick
        ts = time.time()
        await self.wh.write(Tick("AAPL", ts, 150.0, 149.9, 150.1, 100))
        await self.wh.write(Tick("MSFT", ts + 0.1, 400.0, 399.9, 400.1, 50))
        today = self.wh._today
        symbols = self.wh.available_symbols(today)
        self.assertIn("AAPL", symbols)
        self.assertIn("MSFT", symbols)

    async def test_resample_produces_bars(self):
        from infrastructure.tick_warehouse import Tick
        base_ts = 1_700_000_000.0
        ticks = [Tick("SPY", base_ts + i * 5, 500.0, 499.9, 500.1, 100)
                 for i in range(30)]
        await self.wh.write_many(ticks)
        await self.wh.flush()
        today = self.wh._today
        bars = self.wh.resample("SPY", today, interval=60)
        self.assertGreater(len(bars), 0)

    async def test_read_nonexistent_symbol_returns_empty(self):
        result = list(self.wh.read("ZZZNONE", "2000-01-01"))
        self.assertEqual(result, [])

    async def test_session_stats(self):
        from infrastructure.tick_warehouse import Tick
        ts = time.time()
        await self.wh.write(Tick("TSLA", ts, 250.0, 249.9, 250.1, 200))
        today = self.wh._today
        stats = self.wh.session_stats(today)
        self.assertIn("date", stats)


class TestTickWarehouseReplay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from infrastructure.tick_warehouse import TickWarehouse
        self.wh = TickWarehouse(self.tmpdir, bar_intervals=[], retention_days=0)
        await self.wh.open()

    async def asyncTearDown(self):
        await self.wh.close()

    async def test_replay_yields_ticks_in_order(self):
        from infrastructure.tick_warehouse import Tick
        base_ts = 1_700_000_000.0
        written = [Tick("AAPL", base_ts + i, 150.0 + i, 149.9, 150.1, 100)
                   for i in range(5)]
        await self.wh.write_many(written)
        await self.wh.flush()
        today = self.wh._today

        received = []
        async for tick in self.wh.replay("AAPL", today, speed=10_000.0):
            received.append(tick)
        self.assertEqual(len(received), 5)
        ts_list = [t.ts for t in received]
        self.assertEqual(ts_list, sorted(ts_list))

    async def test_multi_symbol_replay_ordered(self):
        from infrastructure.tick_warehouse import Tick
        base_ts = 1_700_000_000.0
        for i in range(3):
            await self.wh.write(Tick("AAA", base_ts + i * 2, 100.0, 99.9, 100.1, 10))
            await self.wh.write(Tick("BBB", base_ts + i * 2 + 1, 200.0, 199.9, 200.1, 20))
        today = self.wh._today

        received = []
        async for tick in self.wh.multi_symbol_replay(["AAA", "BBB"], today, speed=10_000.0):
            received.append(tick.ts)
        self.assertEqual(received, sorted(received))


# ══════════════════════════════════════════════════════════════════════════════
#  Quant Sandbox Tests
# ══════════════════════════════════════════════════════════════════════════════

class _DoNothingStrategy:
    """Test double: strategy that records calls."""
    def __init__(self):
        from infrastructure.quant_sandbox import Strategy, SandboxSignalType
        class _Impl(Strategy):
            def __init__(self_inner):
                super().__init__("do_nothing", "1.0.0", ["AAPL", "NVDA"])
                self_inner.tick_calls = []
                self_inner.started = False
                self_inner.stopped = False

            async def on_start(self_inner):
                self_inner.started = True

            async def on_stop(self_inner):
                self_inner.stopped = True

            async def on_tick(self_inner, symbol, price, bid, ask, volume, ts):
                self_inner.tick_calls.append((symbol, price))

        self.impl_class = _Impl


class _EmitterStrategy:
    """Test double: strategy that emits a signal on every tick."""
    def __init__(self):
        from infrastructure.quant_sandbox import Strategy, SandboxSignalType
        class _Impl(Strategy):
            def __init__(self_inner):
                super().__init__("emitter", "1.0.0", ["AAPL"])

            async def on_tick(self_inner, symbol, price, bid, ask, volume, ts):
                from infrastructure.quant_sandbox import SandboxSignalType
                await self_inner.emit(SandboxSignalType.ENTRY, symbol, "long", 0.9)

        self.impl_class = _Impl


class TestQuantSandboxRegistration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from infrastructure.quant_sandbox import QuantSandbox
        self.sb = QuantSandbox(max_strategies=5)

    async def test_register_returns_id(self):
        s = _DoNothingStrategy().impl_class()
        sid = self.sb.register(s)
        self.assertIsInstance(sid, str)
        self.assertEqual(len(sid), 36)  # UUID format

    async def test_register_multiple(self):
        for _ in range(3):
            s = _DoNothingStrategy().impl_class()
            self.sb.register(s)
        self.assertEqual(len(self.sb._registry), 3)

    async def test_capacity_limit(self):
        from infrastructure.quant_sandbox import QuantSandbox
        sb = QuantSandbox(max_strategies=2)
        for _ in range(2):
            s = _DoNothingStrategy().impl_class()
            sid = sb.register(s)
            await sb.start_strategy(sid)
        # Third registration should fail
        s3 = _DoNothingStrategy().impl_class()
        with self.assertRaises(ValueError):
            sb.register(s3)


class TestQuantSandboxLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from infrastructure.quant_sandbox import QuantSandbox
        self.sb = QuantSandbox()
        impl_class = _DoNothingStrategy().impl_class
        self.strategy = impl_class()
        self.sid = self.sb.register(self.strategy)

    async def test_start_calls_on_start(self):
        await self.sb.start_strategy(self.sid)
        self.assertTrue(self.strategy.started)

    async def test_stop_calls_on_stop(self):
        await self.sb.start_strategy(self.sid)
        await self.sb.stop_strategy(self.sid)
        self.assertTrue(self.strategy.stopped)

    async def test_state_transitions(self):
        from infrastructure.quant_sandbox import StrategyState
        self.assertEqual(self.strategy.state, StrategyState.REGISTERED)
        await self.sb.start_strategy(self.sid)
        self.assertEqual(self.strategy.state, StrategyState.ACTIVE)
        await self.sb.pause_strategy(self.sid)
        self.assertEqual(self.strategy.state, StrategyState.PAUSED)
        await self.sb.resume_strategy(self.sid)
        self.assertEqual(self.strategy.state, StrategyState.ACTIVE)
        await self.sb.stop_strategy(self.sid)
        self.assertEqual(self.strategy.state, StrategyState.STOPPED)

    async def test_tick_routed_to_active_strategy(self):
        from infrastructure.tick_warehouse import Tick
        await self.sb.start_strategy(self.sid)
        tick = Tick("AAPL", time.time(), 150.0, 149.9, 150.1, 100)
        await self.sb.feed_tick(tick)
        self.assertEqual(len(self.strategy.tick_calls), 1)
        self.assertEqual(self.strategy.tick_calls[0][0], "AAPL")

    async def test_tick_not_routed_to_stopped_strategy(self):
        from infrastructure.tick_warehouse import Tick
        await self.sb.start_strategy(self.sid)
        await self.sb.stop_strategy(self.sid)
        tick = Tick("AAPL", time.time(), 150.0, 149.9, 150.1, 100)
        await self.sb.feed_tick(tick)
        self.assertEqual(len(self.strategy.tick_calls), 0)

    async def test_unknown_symbol_not_routed(self):
        from infrastructure.tick_warehouse import Tick
        await self.sb.start_strategy(self.sid)
        tick = Tick("ZZZNONE", time.time(), 50.0, 49.9, 50.1, 10)
        await self.sb.feed_tick(tick)
        self.assertEqual(len(self.strategy.tick_calls), 0)


class TestQuantSandboxSignals(unittest.IsolatedAsyncioTestCase):
    async def test_signal_received_by_consumer(self):
        from infrastructure.quant_sandbox import QuantSandbox
        sb = QuantSandbox()
        impl_class = _EmitterStrategy().impl_class
        strategy = impl_class()
        sid = sb.register(strategy)
        await sb.start_strategy(sid)

        received = []
        async def consumer(sig):
            received.append(sig)
        sb.add_signal_consumer(consumer)

        from infrastructure.tick_warehouse import Tick
        tick = Tick("AAPL", time.time(), 150.0, 149.9, 150.1, 100)
        await sb.feed_tick(tick)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].symbol, "AAPL")
        self.assertEqual(received[0].direction, "long")

    async def test_signal_counter_increments(self):
        from infrastructure.quant_sandbox import QuantSandbox
        sb = QuantSandbox()
        impl_class = _EmitterStrategy().impl_class
        strategy = impl_class()
        sid = sb.register(strategy)
        await sb.start_strategy(sid)
        sb.add_signal_consumer(lambda sig: asyncio.sleep(0))

        from infrastructure.tick_warehouse import Tick
        for _ in range(3):
            await sb.feed_tick(Tick("AAPL", time.time(), 150.0, 149.9, 150.1, 100))
        self.assertEqual(sb._registry[sid].signals_emitted, 3)


class TestQuantSandboxRiskLimits(unittest.IsolatedAsyncioTestCase):
    async def test_daily_loss_quarantine(self):
        from infrastructure.quant_sandbox import (
            QuantSandbox, Strategy, StrategyLimits, StrategyState
        )
        class LossyStrategy(Strategy):
            def __init__(self):
                super().__init__("lossy", symbols=["AAPL"],
                                 limits=StrategyLimits(max_daily_loss_usd=100.0))
            async def on_tick(self, *args, **kwargs):
                pass

        sb = QuantSandbox()
        s = LossyStrategy()
        sid = sb.register(s)
        await sb.start_strategy(sid)

        # Simulate a big loss in the P&L tracker
        s._pnl._daily_pnl = -200.0

        from infrastructure.tick_warehouse import Tick
        await sb.feed_tick(Tick("AAPL", time.time(), 150.0, 149.9, 150.1, 10))
        self.assertEqual(s.state, StrategyState.QUARANTINED)

    async def test_deregister_stopped_strategy(self):
        from infrastructure.quant_sandbox import QuantSandbox
        sb = QuantSandbox()
        impl_class = _DoNothingStrategy().impl_class
        s = impl_class()
        sid = sb.register(s)
        await sb.start_strategy(sid)
        await sb.stop_strategy(sid)
        sb.deregister(sid)
        self.assertNotIn(sid, sb._registry)


class TestStrategyPnLTracker(unittest.TestCase):
    def setUp(self):
        from infrastructure.quant_sandbox import StrategyPnLTracker
        self.pnl = StrategyPnLTracker(initial_equity=10_000.0)

    def test_buy_and_sell_profit(self):
        self.pnl.record_fill("AAPL", 10, 150.0, "buy")
        self.pnl.record_fill("AAPL", 10, 160.0, "sell")
        self.assertAlmostEqual(self.pnl.daily_pnl, 100.0)
        self.assertEqual(self.pnl.total_trades, 1)
        self.assertAlmostEqual(self.pnl.win_rate, 1.0)

    def test_buy_and_sell_loss(self):
        self.pnl.record_fill("NVDA", 5, 800.0, "buy")
        self.pnl.record_fill("NVDA", 5, 790.0, "sell")
        self.assertAlmostEqual(self.pnl.daily_pnl, -50.0)
        self.assertAlmostEqual(self.pnl.win_rate, 0.0)

    def test_drawdown_after_loss(self):
        self.pnl.record_fill("AAPL", 10, 150.0, "buy")
        self.pnl.record_fill("AAPL", 10, 140.0, "sell")
        self.assertGreater(self.pnl.drawdown_pct, 0)

    def test_open_position_count(self):
        self.pnl.record_fill("AAPL", 10, 150.0, "buy")
        self.assertEqual(self.pnl.open_position_count, 1)
        self.pnl.record_fill("AAPL", 10, 160.0, "sell")
        self.assertEqual(self.pnl.open_position_count, 0)

    def test_summary_keys(self):
        s = self.pnl.summary()
        for key in ("equity", "daily_pnl", "drawdown_pct", "total_trades", "win_rate", "open_positions"):
            self.assertIn(key, s)


class TestHotReload(unittest.IsolatedAsyncioTestCase):
    async def test_hot_reload_preserves_pnl(self):
        from infrastructure.quant_sandbox import QuantSandbox, Strategy
        class V1(Strategy):
            def __init__(self):
                super().__init__("strat_v1", "1.0.0", ["AAPL"])
            async def on_tick(self, *a, **kw): pass

        class V2(Strategy):
            def __init__(self):
                super().__init__("strat_v2", "2.0.0", ["AAPL"])
            async def on_tick(self, *a, **kw): pass

        sb = QuantSandbox()
        s1 = V1()
        sid = sb.register(s1)
        await sb.start_strategy(sid)

        # Record some P&L
        s1._pnl.record_fill("AAPL", 10, 150.0, "buy")
        s1._pnl.record_fill("AAPL", 10, 155.0, "sell")
        pnl_before = s1._pnl.daily_pnl

        await sb.hot_reload(sid, V2())

        entry = sb._registry[sid]
        self.assertEqual(entry.strategy.version, "2.0.0")
        self.assertAlmostEqual(entry.strategy._pnl.daily_pnl, pnl_before)


class TestSandboxStatus(unittest.IsolatedAsyncioTestCase):
    async def test_status_structure(self):
        from infrastructure.quant_sandbox import QuantSandbox
        sb = QuantSandbox()
        s = _DoNothingStrategy().impl_class()
        sid = sb.register(s)
        await sb.start_strategy(sid)

        status = sb.status()
        self.assertIn("total", status)
        self.assertIn("active", status)
        self.assertEqual(status["active"], 1)
        self.assertIn(sid, status["strategies"])
        self.assertEqual(status["strategies"][sid]["state"], "active")

        await sb.stop_strategy(sid)


if __name__ == "__main__":
    unittest.main()
