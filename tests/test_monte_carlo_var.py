"""
Tests for Agent 26 — Enterprise VaR Engine.

Covers two test classes:
  TestEnterpriseVaREngine — VaR computation (historical and Monte Carlo),
    portfolio value, confidence levels, CVaR ≥ VaR invariant, empty portfolio.
  TestStressScenarios — named stress scenario library, per-asset breakdowns,
    GFC loss for long-equity portfolio, run_all_stress_tests coverage.

Also covers portfolio stress shock (same file, TestStressScenarios).

All tests are synchronous (no asyncio).
"""
from __future__ import annotations

import numpy as np
import pytest

from agents.agent_26_enterprise_risk import (
    AssetClass,
    EnterpriseVaREngine,
    Position,
    StressScenario,
    STRESS_SCENARIOS,
    VaRResult,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _engine(num_scenarios: int = 1_000) -> EnterpriseVaREngine:
    """Return an engine with deterministic seed and small scenario count for speed."""
    return EnterpriseVaREngine(
        confidence_level=0.99,
        horizon_days=1,
        num_mc_scenarios=num_scenarios,
        random_seed=42,
    )


def _with_positions(engine: EnterpriseVaREngine) -> EnterpriseVaREngine:
    """Add two standard equity positions to the engine."""
    engine.add_position(
        Position("AAPL", AssetClass.EQUITY, qty=10_000, current_price=150.0, cost_basis=140.0)
    )
    engine.add_position(
        Position("MSFT", AssetClass.EQUITY, qty=5_000, current_price=300.0, cost_basis=280.0)
    )
    return engine


def _with_returns(engine: EnterpriseVaREngine) -> EnterpriseVaREngine:
    """Add 252-day synthetic return history for both symbols."""
    rng = np.random.default_rng(99)
    engine.set_historical_returns("AAPL", rng.normal(0.0005, 0.018, 252))
    engine.set_historical_returns("MSFT", rng.normal(0.0004, 0.016, 252))
    return engine


# ─── TestEnterpriseVaREngine ─────────────────────────────────────────────────

class TestEnterpriseVaREngine:
    """Core VaR engine: value calculations, method invariants, result shape."""

    def test_portfolio_value_sum_of_abs_market_values(self):
        """portfolio_value() equals sum of |qty × price| for all positions."""
        eng = _with_positions(_engine())
        expected = abs(10_000 * 150.0) + abs(5_000 * 300.0)
        assert eng.portfolio_value() == pytest.approx(expected)

    def test_var_positive_for_long_portfolio(self):
        """Monte Carlo VaR must be a positive number for a long portfolio."""
        eng = _with_positions(_engine())
        result = eng.monte_carlo_var()
        assert result.var_usd > 0

    def test_historical_var_returns_var_result(self):
        """historical_var() returns a VaRResult instance."""
        eng = _with_positions(_engine())
        result = eng.historical_var()
        assert isinstance(result, VaRResult)

    def test_monte_carlo_var_returns_var_result(self):
        """monte_carlo_var() returns a VaRResult instance."""
        eng = _with_positions(_engine())
        result = eng.monte_carlo_var()
        assert isinstance(result, VaRResult)

    def test_mc_var_num_scenarios_correct(self):
        """result.num_scenarios matches the engine's num_mc_scenarios setting."""
        n = 500
        eng = _with_positions(EnterpriseVaREngine(num_mc_scenarios=n, random_seed=0))
        result = eng.monte_carlo_var()
        assert result.num_scenarios == n

    def test_var_confidence_level_stored(self):
        """Confidence level in VaRResult matches what was set on construction."""
        eng = _with_positions(EnterpriseVaREngine(confidence_level=0.95, random_seed=1))
        result = eng.monte_carlo_var()
        assert result.confidence_level == pytest.approx(0.95)

    def test_cvar_ge_var(self):
        """CVaR (Expected Shortfall) is always >= VaR by definition."""
        eng = _with_positions(_engine())
        result = eng.monte_carlo_var()
        assert result.cvar_usd >= result.var_usd - 1e-9   # tolerance for float rounding

    def test_var_pct_between_0_and_1(self):
        """var_pct is a fraction in [0, 1] (can exceed 1 only for extreme leverage)."""
        eng = _with_positions(_engine())
        result = eng.monte_carlo_var()
        # For a normal un-levered long-only portfolio, var_pct should be small
        assert 0.0 <= result.var_pct <= 1.0

    def test_percentile_distribution_has_100_values(self):
        """percentile_distribution always contains exactly 100 entries."""
        eng = _with_positions(_engine())
        result = eng.monte_carlo_var()
        assert len(result.percentile_distribution) == 100

    def test_empty_portfolio_var_zero(self):
        """An engine with no positions returns zero VaR and zero portfolio value."""
        eng = _engine()
        result = eng.monte_carlo_var()
        assert result.var_usd == 0.0
        assert result.portfolio_value_usd == 0.0

    def test_large_portfolio_var_larger_than_small(self):
        """Doubling position sizes should approximately double VaR."""
        small_eng = _engine()
        small_eng.add_position(
            Position("AAPL", AssetClass.EQUITY, qty=1_000, current_price=150.0, cost_basis=140.0)
        )
        large_eng = _engine()
        large_eng.add_position(
            Position("AAPL", AssetClass.EQUITY, qty=10_000, current_price=150.0, cost_basis=140.0)
        )
        assert large_eng.monte_carlo_var().var_usd > small_eng.monte_carlo_var().var_usd

    def test_historical_var_uses_actual_returns(self):
        """historical_var() with loaded return history produces a positive VaR."""
        eng = _with_returns(_with_positions(_engine()))
        result = eng.historical_var()
        assert result.var_usd > 0
        assert result.method == "historical"

    def test_worst_scenario_ge_var(self):
        """Worst scenario loss is always at least as bad as VaR."""
        eng = _with_positions(_engine())
        result = eng.monte_carlo_var()
        # worst_scenario_usd can be negative (profit) in trivially calm markets;
        # but var_usd ≤ worst_scenario_usd when portfolio has real risk.
        # We just verify worst >= var (both are "positive = loss" conventions).
        assert result.worst_scenario_usd >= result.var_usd - 1e-9

    def test_mc_var_method_label_is_monte_carlo(self):
        """VaRResult.method is 'monte_carlo' for Monte Carlo runs."""
        eng = _with_positions(_engine())
        result = eng.monte_carlo_var()
        assert result.method == "monte_carlo"

    def test_historical_var_method_label_is_historical(self):
        """VaRResult.method is 'historical' for historical runs."""
        eng = _with_positions(_engine())
        result = eng.historical_var()
        assert result.method == "historical"

    def test_add_position_updates_portfolio_value(self):
        """Adding a new position increases portfolio_value()."""
        eng = _engine()
        before = eng.portfolio_value()
        eng.add_position(
            Position("TSLA", AssetClass.EQUITY, qty=500, current_price=200.0, cost_basis=180.0)
        )
        assert eng.portfolio_value() > before

    def test_short_position_contributes_to_portfolio_value(self):
        """Short positions (negative qty) contribute |market_value| to portfolio value."""
        eng = _engine()
        eng.add_position(
            Position("SPY", AssetClass.EQUITY, qty=-1_000, current_price=450.0, cost_basis=460.0)
        )
        assert eng.portfolio_value() == pytest.approx(1_000 * 450.0)


# ─── TestStressScenarios ──────────────────────────────────────────────────────

class TestStressScenarios:
    """Named stress scenario application and portfolio shock calculations."""

    def test_gfc_scenario_exists(self):
        """2008_GFC scenario is present in the STRESS_SCENARIOS dictionary."""
        assert "2008_GFC" in STRESS_SCENARIOS

    def test_all_scenarios_in_stress_scenarios_dict(self):
        """All five canonical stress scenarios are present."""
        expected = {
            "2008_GFC",
            "2020_COVID",
            "1987_BLACK_MONDAY",
            "FLASH_CRASH_2010",
            "VOLMAGEDDON_2018",
        }
        assert expected.issubset(STRESS_SCENARIOS.keys())

    def test_stress_test_returns_dict_with_required_keys(self):
        """stress_test() output contains all required top-level keys."""
        eng = _with_positions(_engine())
        result = eng.stress_test("2008_GFC")
        required = {"scenario", "description", "total_pnl_usd", "portfolio_value_usd", "pnl_pct", "per_asset"}
        assert required.issubset(result.keys())

    def test_gfc_stress_causes_loss_for_long_equity(self):
        """A long equity portfolio loses money under the 2008 GFC scenario."""
        eng = _with_positions(_engine())
        result = eng.stress_test("2008_GFC")
        assert result["total_pnl_usd"] < 0

    def test_run_all_stress_tests_returns_all_names(self):
        """run_all_stress_tests() returns a result for every scenario name."""
        eng = _with_positions(_engine())
        results = eng.run_all_stress_tests()
        assert set(results.keys()) == set(STRESS_SCENARIOS.keys())

    def test_stress_per_asset_breakdown(self):
        """per_asset dict in stress result contains an entry for each position."""
        eng = _with_positions(_engine())
        result = eng.stress_test("2020_COVID")
        assert "AAPL" in result["per_asset"]
        assert "MSFT" in result["per_asset"]

    def test_summary_has_required_keys(self):
        """summary() returns all expected keys for dashboard display."""
        eng = _with_positions(_engine())
        s = eng.summary()
        required = {
            "portfolio_value_usd", "num_positions", "confidence_level",
            "horizon_days", "mc_var_usd", "mc_cvar_usd", "mc_var_pct",
        }
        assert required.issubset(s.keys())

    def test_covid_stress_causes_loss_for_long_equity(self):
        """A long equity portfolio also loses money under the 2020 COVID scenario."""
        eng = _with_positions(_engine())
        result = eng.stress_test("2020_COVID")
        assert result["total_pnl_usd"] < 0

    def test_stress_scenario_name_in_result(self):
        """stress_test() echoes the scenario name back in the result dict."""
        eng = _with_positions(_engine())
        result = eng.stress_test("1987_BLACK_MONDAY")
        assert result["scenario"] == "1987_BLACK_MONDAY"

    def test_stress_portfolio_value_matches_engine(self):
        """portfolio_value_usd in stress result matches engine.portfolio_value()."""
        eng = _with_positions(_engine())
        expected = eng.portfolio_value()
        result = eng.stress_test("FLASH_CRASH_2010")
        assert result["portfolio_value_usd"] == pytest.approx(expected, rel=1e-6)

    def test_options_position_stress_shock_amplified(self):
        """Options positions receive an amplified shock under GFC (5× equity × vol)."""
        eng = _engine()
        eng.add_position(
            Position("SPY_CALL", AssetClass.OPTIONS, qty=100, current_price=10.0, cost_basis=8.0)
        )
        result = eng.stress_test("2008_GFC")
        # GFC: equity_shock=-0.37, vol_shock=1.50 → options shock = -0.37*5*1.50 = -2.775
        expected_pnl = 100 * 10.0 * (-0.37 * 5.0 * 1.50)
        assert result["per_asset"]["SPY_CALL"] == pytest.approx(expected_pnl, rel=1e-4)

    def test_fixed_income_stress_shock_rate_based(self):
        """Fixed income positions are shocked via duration (7yr) × rate change."""
        eng = _engine()
        eng.add_position(
            Position("TLT", AssetClass.FIXED_INCOME, qty=1_000, current_price=100.0, cost_basis=98.0)
        )
        result = eng.stress_test("2008_GFC")
        # GFC rates_shock_bps = +800; shock = -800/10_000 * 7 = -0.56
        expected_pnl = 1_000 * 100.0 * (-800.0 / 10_000.0 * 7.0)
        assert result["per_asset"]["TLT"] == pytest.approx(expected_pnl, rel=1e-4)

    def test_volmageddon_scenario_present_and_has_vol_shock(self):
        """VOLMAGEDDON_2018 scenario has a vol_shock of 2.0 (200% VIX spike)."""
        scenario = STRESS_SCENARIOS["VOLMAGEDDON_2018"]
        assert isinstance(scenario, StressScenario)
        assert scenario.vol_shock == pytest.approx(2.0)
