"""
Agent 26 — Enterprise Risk Management (Value at Risk)

Implements Historical and Monte Carlo VaR across a multi-asset portfolio.
Required for SEC Rule 15c3-1 (Net Capital) and Basel III internal models.

Historical VaR: Sort actual past P&L scenarios, take the 1% worst case.
Monte Carlo VaR: Simulate 10,000 correlated market crashes via Cholesky
  decomposition of the asset covariance matrix. Each scenario:
    1. Draw correlated shocks from multivariate normal distribution
    2. Apply to current portfolio positions
    3. Compute scenario P&L
  Sort 10,000 P&Ls, 99% VaR = 1st percentile loss.

Stress testing: Apply named shock scenarios:
  - "2008_GFC": equities -37%, credit spreads +800 bps, VIX +150%
  - "2020_COVID": equities -34%, rates -100 bps, credit +500 bps
  - "1987_BLACK_MONDAY": single-day equities -22%
  - "FLASH_CRASH_2010": 5-minute equities -9%, instant recovery
  - "VOLMAGEDDON_2018": VIX +200% overnight, short-vol products blow up
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np


# ─── Asset Classification ──────────────────────────────────────────────────────

class AssetClass(str, Enum):
    EQUITY = "equity"
    FX = "fx"
    OPTIONS = "options"
    FIXED_INCOME = "fixed_income"
    FUTURES = "futures"


# ─── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    asset_class: AssetClass
    qty: int            # positive = long, negative = short
    current_price: float
    cost_basis: float

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.qty * (self.current_price - self.cost_basis)


@dataclass
class VaRResult:
    confidence_level: float         # e.g. 0.99
    horizon_days: int               # e.g. 1
    var_usd: float                  # VaR dollar amount (positive = potential loss)
    cvar_usd: float                 # Conditional VaR (Expected Shortfall) — mean of worst 1%
    portfolio_value_usd: float
    var_pct: float                  # VaR / portfolio_value
    method: str                     # "historical" or "monte_carlo"
    num_scenarios: int
    worst_scenario_usd: float       # absolute worst scenario
    percentile_distribution: list[float]  # 100 percentile values for histogram


@dataclass
class StressScenario:
    name: str
    equity_shock: float     # e.g. -0.37 for -37%
    rates_shock_bps: float  # e.g. +800 for credit spreads (positive = spreads widen)
    fx_shock: float         # e.g. -0.10 for USD weakening 10%
    vol_shock: float        # e.g. +1.5 for VIX +150%
    description: str


# ─── Stress Scenario Library ───────────────────────────────────────────────────

STRESS_SCENARIOS: dict[str, StressScenario] = {
    "2008_GFC": StressScenario(
        name="2008_GFC",
        equity_shock=-0.37,
        rates_shock_bps=800.0,
        fx_shock=-0.15,
        vol_shock=1.50,
        description="2008 Global Financial Crisis",
    ),
    "2020_COVID": StressScenario(
        name="2020_COVID",
        equity_shock=-0.34,
        rates_shock_bps=-100.0,
        fx_shock=0.05,
        vol_shock=2.00,
        description="2020 COVID crash",
    ),
    "1987_BLACK_MONDAY": StressScenario(
        name="1987_BLACK_MONDAY",
        equity_shock=-0.22,
        rates_shock_bps=50.0,
        fx_shock=0.0,
        vol_shock=5.00,
        description="Black Monday 1987 single-day crash",
    ),
    "FLASH_CRASH_2010": StressScenario(
        name="FLASH_CRASH_2010",
        equity_shock=-0.09,
        rates_shock_bps=0.0,
        fx_shock=0.0,
        vol_shock=0.50,
        description="2010 Flash Crash (5-min event)",
    ),
    "VOLMAGEDDON_2018": StressScenario(
        name="VOLMAGEDDON_2018",
        equity_shock=-0.04,
        rates_shock_bps=0.0,
        fx_shock=0.0,
        vol_shock=2.00,
        description="Volmageddon 2018 (VIX spike, XIV blowup)",
    ),
}


# ─── VaR Engine ────────────────────────────────────────────────────────────────

class EnterpriseVaREngine:
    """
    Historical and Monte Carlo VaR computation for multi-asset portfolios.

    Historical VaR replays actual past daily returns across the portfolio to
    build an empirical P&L distribution without assuming any parametric form.

    Monte Carlo VaR draws correlated shocks from a multivariate normal using
    Cholesky decomposition of the sample covariance matrix estimated from
    historical returns. This captures cross-asset correlation (e.g., equity
    and credit both sell off together in a risk-off event).

    Both methods report:
      - VaR at the chosen confidence level (default 99%)
      - CVaR / Expected Shortfall (mean of the worst tail)
      - Full percentile distribution for risk reporting
    """

    def __init__(
        self,
        confidence_level: float = 0.99,
        horizon_days: int = 1,
        num_mc_scenarios: int = 10_000,
        random_seed: Optional[int] = 42,
    ):
        self._confidence = confidence_level
        self._horizon = horizon_days
        self._num_scenarios = num_mc_scenarios
        self._rng = np.random.default_rng(random_seed)
        self._positions: dict[str, Position] = {}
        self._historical_returns: dict[str, np.ndarray] = {}  # symbol → daily returns

    # ─── Position Management ───────────────────────────────────────────────────

    def add_position(self, position: Position) -> None:
        """Add or update a portfolio position."""
        self._positions[position.symbol] = position

    def set_historical_returns(self, symbol: str, returns: np.ndarray) -> None:
        """
        Set historical daily returns for a symbol (numpy array of floats, e.g.
        [-0.02, 0.01, ...]). Used for historical VaR simulation and for
        estimating the covariance matrix used by Monte Carlo VaR.
        """
        self._historical_returns[symbol] = np.asarray(returns, dtype=float)

    def portfolio_value(self) -> float:
        """Total portfolio value as sum of absolute market values."""
        return sum(abs(p.market_value) for p in self._positions.values())

    # ─── Scenario P&L Calculation ──────────────────────────────────────────────

    def _scenario_pnl(self, shocks: dict[str, float]) -> float:
        """
        Compute portfolio P&L for a given set of per-symbol return shocks.

        Args:
            shocks: mapping of symbol → fractional return, e.g. {"AAPL": -0.05}

        Returns:
            Signed dollar P&L (negative = loss, positive = gain).
        """
        pnl = 0.0
        for symbol, position in self._positions.items():
            shock = shocks.get(symbol, 0.0)
            pnl += position.qty * position.current_price * shock
        return pnl

    # ─── Historical VaR ───────────────────────────────────────────────────────

    def historical_var(self) -> VaRResult:
        """
        Historical simulation VaR using stored return history.

        For each historical date, compute portfolio P&L, then take the
        (1 - confidence_level) quantile as VaR.

        If no historical returns are loaded, a synthetic 252-day history
        is generated using Gaussian returns (vol = 1.5% / day) so the
        engine always returns a valid result.
        """
        if not self._historical_returns:
            n = 252
            for symbol in self._positions:
                self._historical_returns[symbol] = self._rng.normal(0, 0.015, n)

        # Use minimum common history length across all symbols
        min_len = min(len(r) for r in self._historical_returns.values())

        scenario_pnls: list[float] = []
        for i in range(min_len):
            shocks = {sym: float(self._historical_returns[sym][i])
                      for sym in self._historical_returns
                      if sym in self._positions}
            scenario_pnls.append(self._scenario_pnl(shocks))

        return self._compute_var_result(np.array(scenario_pnls), method="historical")

    # ─── Monte Carlo VaR ──────────────────────────────────────────────────────

    def monte_carlo_var(self) -> VaRResult:
        """
        Monte Carlo VaR using correlated normal shocks via Cholesky decomposition.

        Algorithm:
          1. Build covariance matrix from historical returns (or use a default
             with 1.5% daily vol and 30% pairwise correlation).
          2. Scale by horizon_days (square-root-of-time rule under i.i.d. returns).
          3. Cholesky-decompose the covariance matrix.
          4. Draw num_scenarios independent standard normal vectors and multiply
             by the Cholesky factor to obtain correlated shocks.
          5. Compute portfolio P&L for each scenario.
          6. Return the (1 - confidence_level) tail statistics.
        """
        symbols = list(self._positions.keys())
        n = len(symbols)

        if n == 0:
            # Empty portfolio: return zero VaR trivially
            return VaRResult(
                confidence_level=self._confidence,
                horizon_days=self._horizon,
                var_usd=0.0,
                cvar_usd=0.0,
                portfolio_value_usd=0.0,
                var_pct=0.0,
                method="monte_carlo",
                num_scenarios=self._num_scenarios,
                worst_scenario_usd=0.0,
                percentile_distribution=[0.0] * 100,
            )

        if len(self._historical_returns) >= 2:
            # Build sample covariance matrix from stored return history
            window = 252
            data = np.column_stack([
                self._historical_returns.get(s, self._rng.normal(0, 0.015, window))[:window]
                for s in symbols
            ])
            cov = np.cov(data.T) * self._horizon
            if cov.ndim == 0:
                # Single-asset edge case from np.cov
                cov = np.array([[float(cov) * self._horizon]])
        else:
            # Default covariance: 1.5% daily vol, 30% pairwise correlation
            vols = np.full(n, 0.015)
            corr = np.full((n, n), 0.30)
            np.fill_diagonal(corr, 1.0)
            cov = np.outer(vols, vols) * corr * self._horizon

        # Cholesky decomposition — add small ridge for numerical stability
        try:
            L = np.linalg.cholesky(cov + np.eye(n) * 1e-10)
        except np.linalg.LinAlgError:
            # Fallback: diagonal (independent assets)
            L = np.diag(np.sqrt(np.maximum(np.diag(cov), 1e-10)))

        # Generate correlated shocks: shape (num_scenarios, n_assets)
        z = self._rng.standard_normal((self._num_scenarios, n))
        correlated_shocks = z @ L.T

        # Compute portfolio P&L for each simulated scenario
        scenario_pnls: list[float] = []
        for s_idx in range(self._num_scenarios):
            shocks = {sym: float(correlated_shocks[s_idx, i])
                      for i, sym in enumerate(symbols)}
            scenario_pnls.append(self._scenario_pnl(shocks))

        return self._compute_var_result(np.array(scenario_pnls), method="monte_carlo")

    # ─── VaR Statistics ───────────────────────────────────────────────────────

    def _compute_var_result(self, pnls: np.ndarray, method: str) -> VaRResult:
        """
        Derive VaR statistics from an array of scenario P&Ls.

        VaR is the (1 - confidence_level) quantile of the loss distribution.
        CVaR (Expected Shortfall) is the mean of all scenarios worse than VaR.
        Both are returned as positive numbers representing potential losses.
        """
        sorted_pnls = np.sort(pnls)
        n = len(sorted_pnls)

        # Index of the VaR quantile
        tail_count = max(1, int(n * (1 - self._confidence)))
        var_idx = tail_count - 1          # last index in the worst-tail slice

        var_usd = float(-sorted_pnls[var_idx])
        # CVaR = mean of all scenarios at or worse than VaR
        cvar_usd = float(-np.mean(sorted_pnls[: var_idx + 1]))

        port_val = self.portfolio_value()

        # Build 100-value percentile distribution for histogram / risk reporting
        percentiles = np.percentile(sorted_pnls, np.linspace(0, 100, 100)).tolist()

        return VaRResult(
            confidence_level=self._confidence,
            horizon_days=self._horizon,
            var_usd=max(0.0, var_usd),
            cvar_usd=max(0.0, cvar_usd),
            portfolio_value_usd=port_val,
            var_pct=var_usd / port_val if port_val > 0 else 0.0,
            method=method,
            num_scenarios=n,
            worst_scenario_usd=float(-sorted_pnls[0]),
            percentile_distribution=percentiles,
        )

    # ─── Stress Testing ───────────────────────────────────────────────────────

    def stress_test(self, scenario_name: str) -> dict:
        """
        Apply a named stress scenario to the current portfolio.

        Asset-class-specific shocks are applied:
          - Equity: direct equity_shock
          - FX: fx_shock
          - Options: leveraged (5× equity shock × vol multiplier)
          - Fixed Income: duration-weighted rate shock (assumes 7-year duration)
          - Futures: half the equity shock (partial beta)

        Returns a dict with total P&L, per-asset breakdown, and portfolio value.
        """
        scenario = STRESS_SCENARIOS[scenario_name]
        per_asset: dict[str, float] = {}
        total_pnl = 0.0

        for symbol, pos in self._positions.items():
            if pos.asset_class == AssetClass.EQUITY:
                shock = scenario.equity_shock
            elif pos.asset_class == AssetClass.FX:
                shock = scenario.fx_shock
            elif pos.asset_class == AssetClass.OPTIONS:
                # Options are leveraged: equity move amplified by vol expansion
                shock = scenario.equity_shock * 5.0 * scenario.vol_shock
            elif pos.asset_class == AssetClass.FIXED_INCOME:
                # Approximate: modified_duration × Δrate  (assume 7-yr duration)
                shock = -scenario.rates_shock_bps / 10_000.0 * 7.0
            else:
                # FUTURES: partial equity beta (0.5)
                shock = scenario.equity_shock * 0.5

            asset_pnl = pos.qty * pos.current_price * shock
            per_asset[symbol] = round(asset_pnl, 2)
            total_pnl += asset_pnl

        port_val = self.portfolio_value()
        return {
            "scenario": scenario_name,
            "description": scenario.description,
            "total_pnl_usd": round(total_pnl, 2),
            "portfolio_value_usd": round(port_val, 2),
            "pnl_pct": round(total_pnl / port_val * 100, 2) if port_val > 0 else 0.0,
            "per_asset": per_asset,
        }

    def run_all_stress_tests(self) -> dict[str, dict]:
        """Run all defined stress scenarios. Returns {scenario_name: result}."""
        return {name: self.stress_test(name) for name in STRESS_SCENARIOS}

    # ─── Summary ──────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """
        Quick portfolio risk summary — runs Monte Carlo VaR.
        Suitable for dashboard display and daily risk reporting.
        """
        mc = self.monte_carlo_var()
        return {
            "portfolio_value_usd": round(mc.portfolio_value_usd, 2),
            "num_positions": len(self._positions),
            "confidence_level": self._confidence,
            "horizon_days": self._horizon,
            "mc_var_usd": round(mc.var_usd, 2),
            "mc_cvar_usd": round(mc.cvar_usd, 2),
            "mc_var_pct": round(mc.var_pct * 100, 2),
        }
