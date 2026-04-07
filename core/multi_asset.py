"""
core/multi_asset.py — Multi-Asset Position Models

Provides instrument specifications and P&L/margin calculations for:
  - FX (spot and forward): pip value, carry, swap points
  - Futures: contract specs, roll mechanics, basis, tick value
  - Options: Black-Scholes (European) pricing and Greeks (delta, gamma, vega, theta, rho)
  - Multi-asset portfolio: cross-asset margin aggregation, net Greeks

All math is self-contained with no external dependencies beyond the stdlib.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal


# ─── Constants ─────────────────────────────────────────────────────────────────

_SQRT_2PI = math.sqrt(2 * math.pi)
_DAYS_PER_YEAR = 252.0
_CALENDAR_DAYS_PER_YEAR = 365.25


# ─── Black-Scholes helpers ─────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Abramowitz & Stegun approximation — max error 7.5e-8."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1d2(S: float, K: float, r: float, q: float, sigma: float, T: float) -> tuple[float, float]:
    """
    Compute d1 and d2 for Black-Scholes / Black-76.

    Args:
        S:     Spot price (or forward for Black-76).
        K:     Strike price.
        r:     Risk-free rate (continuous, annualised).
        q:     Dividend yield / foreign rate (continuous, annualised).
        sigma: Implied volatility (annualised).
        T:     Time to expiry in years (must be > 0).

    Returns:
        (d1, d2)
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        raise ValueError(f"Invalid BS inputs: S={S} K={K} r={r} q={q} sigma={sigma} T={T}")
    log_ratio = math.log(S / K)
    vol_sqrt_T = sigma * math.sqrt(T)
    d1 = (log_ratio + (r - q + 0.5 * sigma ** 2) * T) / vol_sqrt_T
    d2 = d1 - vol_sqrt_T
    return d1, d2


# ══════════════════════════════════════════════════════════════════════════════
#  FX (Foreign Exchange)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FXSpec:
    """
    Instrument specification for a currency pair.

    Attributes:
        base:         Base currency (e.g. 'EUR').
        quote:        Quote currency (e.g. 'USD').
        pip_size:     Size of one pip (e.g. 0.0001 for EUR/USD, 0.01 for USD/JPY).
        lot_size:     Standard lot size in base currency units (usually 100_000).
        min_lot:      Minimum tradeable lot (e.g. 0.01 = micro lot).
        margin_rate:  Fractional margin requirement (e.g. 0.02 = 50:1 leverage).
    """
    base: str
    quote: str
    pip_size: float = 0.0001
    lot_size: int = 100_000
    min_lot: float = 0.01
    margin_rate: float = 0.02   # 50:1 leverage by default

    @property
    def pair(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass
class FXPosition:
    """
    Open FX spot position.

    Attributes:
        spec:        Instrument spec.
        lots:        Signed lot size (+ve = long base, -ve = short base).
        entry_rate:  Entry mid-rate.
        current_rate: Current mid-rate.
        swap_rate_pa: Annual swap/rollover rate in quote currency per lot (can be negative).
        days_held:   Number of calendar days position has been open.
    """
    spec: FXSpec
    lots: float
    entry_rate: float
    current_rate: float
    swap_rate_pa: float = 0.0
    days_held: int = 0

    # ── Core calculations ──────────────────────────────────────────────────

    def pip_value(self) -> float:
        """
        Value of one pip move in quote currency for the entire position.

        pip_value = lots × lot_size × pip_size
        """
        return self.lots * self.spec.lot_size * self.spec.pip_size

    def unrealized_pnl(self) -> float:
        """
        Unrealised P&L in quote currency.

        pnl = (current_rate - entry_rate) × lots × lot_size
        """
        return (self.current_rate - self.entry_rate) * self.lots * self.spec.lot_size

    def accrued_swap(self) -> float:
        """
        Accrued rollover/swap in quote currency.

        swap = swap_rate_pa × days_held / 365.25 × lots
        """
        return self.swap_rate_pa * self.days_held / _CALENDAR_DAYS_PER_YEAR * self.lots

    def total_pnl(self) -> float:
        """Total P&L including swap accrual."""
        return self.unrealized_pnl() + self.accrued_swap()

    def notional(self) -> float:
        """Gross notional in quote currency at current rate."""
        return abs(self.lots) * self.spec.lot_size * self.current_rate

    def required_margin(self) -> float:
        """Required margin in quote currency."""
        return self.notional() * self.spec.margin_rate

    def pips_pnl(self) -> float:
        """P&L expressed in pips."""
        if self.spec.pip_size == 0:
            return 0.0
        return (self.current_rate - self.entry_rate) / self.spec.pip_size * math.copysign(1, self.lots)


def fx_forward_rate(spot: float, r_base: float, r_quote: float, T: float) -> float:
    """
    Interest rate parity forward rate.

    F = S × e^((r_quote - r_base) × T)

    Args:
        spot:    Spot rate (base/quote).
        r_base:  Base currency risk-free rate (continuous, annualised).
        r_quote: Quote currency risk-free rate.
        T:       Time to delivery in years.
    """
    return spot * math.exp((r_quote - r_base) * T)


def fx_swap_points(spot: float, r_base: float, r_quote: float, T: float) -> float:
    """
    Forward swap points (forward premium/discount) in pips.

    swap_points = (F - S) / pip_size  (using pip_size=0.0001)
    """
    fwd = fx_forward_rate(spot, r_base, r_quote, T)
    return (fwd - spot) / 0.0001


# ══════════════════════════════════════════════════════════════════════════════
#  Futures
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FuturesSpec:
    """
    Contract specification for an exchange-traded futures contract.

    Attributes:
        symbol:          Root symbol (e.g. 'ES').
        name:            Full name (e.g. 'E-mini S&P 500').
        exchange:        Exchange (e.g. 'CME').
        tick_size:       Minimum price increment.
        tick_value:      Dollar value of one tick move.
        contract_size:   Underlying units per contract.
        currency:        Settlement currency.
        initial_margin:  Exchange initial margin per contract (USD).
        maint_margin:    Exchange maintenance margin per contract (USD).
        expiry_months:   Valid expiry month codes (e.g. ['H','M','U','Z']).
    """
    symbol: str
    name: str
    exchange: str
    tick_size: float
    tick_value: float
    contract_size: float
    currency: str = "USD"
    initial_margin: float = 0.0
    maint_margin: float = 0.0
    expiry_months: list[str] = field(default_factory=lambda: ["H", "M", "U", "Z"])

    def ticks_to_dollars(self, ticks: float) -> float:
        """Convert a price move expressed in ticks to dollar P&L."""
        return ticks * self.tick_value

    def dollars_to_ticks(self, dollars: float) -> float:
        """Convert a dollar move to number of ticks."""
        if self.tick_value == 0:
            raise ZeroDivisionError("tick_value is zero")
        return dollars / self.tick_value


# Standard contract library ─────────────────────────────────────────────────

FUTURES_SPECS: dict[str, FuturesSpec] = {
    "ES": FuturesSpec(
        symbol="ES", name="E-mini S&P 500", exchange="CME",
        tick_size=0.25, tick_value=12.50, contract_size=50,
        initial_margin=12_300, maint_margin=11_000,
    ),
    "NQ": FuturesSpec(
        symbol="NQ", name="E-mini NASDAQ-100", exchange="CME",
        tick_size=0.25, tick_value=5.00, contract_size=20,
        initial_margin=15_500, maint_margin=14_000,
    ),
    "RTY": FuturesSpec(
        symbol="RTY", name="E-mini Russell 2000", exchange="CME",
        tick_size=0.10, tick_value=10.00, contract_size=50,
        initial_margin=7_000, maint_margin=6_300,
    ),
    "CL": FuturesSpec(
        symbol="CL", name="WTI Crude Oil", exchange="NYMEX",
        tick_size=0.01, tick_value=10.00, contract_size=1_000,
        expiry_months=["F","G","H","J","K","M","N","Q","U","V","X","Z"],
        initial_margin=5_000, maint_margin=4_500,
    ),
    "GC": FuturesSpec(
        symbol="GC", name="Gold", exchange="COMEX",
        tick_size=0.10, tick_value=10.00, contract_size=100,
        expiry_months=["G","J","M","Q","V","Z"],
        initial_margin=8_200, maint_margin=7_400,
    ),
    "ZB": FuturesSpec(
        symbol="ZB", name="30-Year T-Bond", exchange="CBOT",
        tick_size=1/32, tick_value=31.25, contract_size=100_000,
        initial_margin=3_800, maint_margin=3_500,
    ),
    "6E": FuturesSpec(
        symbol="6E", name="Euro FX Futures", exchange="CME",
        tick_size=0.00005, tick_value=6.25, contract_size=125_000, currency="EUR",
        initial_margin=2_400, maint_margin=2_200,
    ),
    "BTC": FuturesSpec(
        symbol="BTC", name="Bitcoin Futures", exchange="CME",
        tick_size=5.00, tick_value=25.00, contract_size=5,
        expiry_months=["F","G","H","J","K","M","N","Q","U","V","X","Z"],
        initial_margin=40_000, maint_margin=36_000,
    ),
}


@dataclass
class FuturesPosition:
    """
    Open futures position.

    Attributes:
        spec:            Contract spec.
        contracts:       Signed number of contracts (+ve = long, -ve = short).
        entry_price:     Fill price at entry.
        current_price:   Current mark price.
        expiry_code:     Expiry month+year code (e.g. 'Z25').
    """
    spec: FuturesSpec
    contracts: int
    entry_price: float
    current_price: float
    expiry_code: str = ""

    def unrealized_pnl(self) -> float:
        """
        Dollar P&L.

        pnl = (current - entry) / tick_size × tick_value × contracts
        """
        ticks = (self.current_price - self.entry_price) / self.spec.tick_size
        return ticks * self.spec.tick_value * self.contracts

    def dollar_delta(self) -> float:
        """
        Dollar sensitivity to a 1-point move in the underlying.

        delta = contract_size × contracts
        """
        return self.spec.contract_size * self.contracts

    def notional(self) -> float:
        """Gross notional at current price."""
        return abs(self.contracts) * self.spec.contract_size * self.current_price

    def basis(self, spot_price: float) -> float:
        """Futures basis = futures - spot."""
        return self.current_price - spot_price

    def required_margin(self) -> float:
        """Exchange initial margin for the position."""
        return abs(self.contracts) * self.spec.initial_margin

    def roll_pnl(self, near_price: float, far_price: float) -> float:
        """
        P&L impact of rolling from near to far expiry.

        Positive = rolling into backwardation (profit), negative = contango (cost).
        """
        return (near_price - far_price) * self.contracts * self.spec.contract_size / self.spec.tick_size * self.spec.tick_value / self.spec.contract_size


# ══════════════════════════════════════════════════════════════════════════════
#  Options (European Black-Scholes)
# ══════════════════════════════════════════════════════════════════════════════

OptionRight = Literal["call", "put"]


@dataclass
class OptionSpec:
    """
    Specification for a vanilla European option.

    Attributes:
        underlying:  Underlying symbol.
        strike:      Strike price.
        expiry_days: Calendar days to expiry (must be > 0 at creation).
        right:       'call' or 'put'.
        multiplier:  Contract multiplier (e.g. 100 for US equity options).
        style:       'european' (BS pricing) or 'american' (approximation).
    """
    underlying: str
    strike: float
    expiry_days: int
    right: OptionRight
    multiplier: int = 100
    style: Literal["european", "american"] = "european"

    @property
    def T(self) -> float:
        """Time to expiry in calendar years."""
        return self.expiry_days / _CALENDAR_DAYS_PER_YEAR


@dataclass
class BSResult:
    """
    Full Black-Scholes pricing result.

    All dollar-denominated values are per-share (not per-contract).
    """
    price: float           # option premium
    delta: float           # ∂V/∂S
    gamma: float           # ∂²V/∂S²
    vega: float            # ∂V/∂σ per 1pp vol move
    theta: float           # ∂V/∂t per calendar day
    rho: float             # ∂V/∂r per 1pp rate move
    d1: float
    d2: float
    intrinsic: float       # max(S-K, 0) for call / max(K-S, 0) for put
    time_value: float      # price - intrinsic


def black_scholes(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    right: OptionRight = "call",
    q: float = 0.0,
) -> BSResult:
    """
    European option pricing via Black-Scholes-Merton.

    Args:
        S:     Spot price of the underlying.
        K:     Strike price.
        T:     Time to expiry in years.
        r:     Risk-free rate (continuous, annualised).
        sigma: Implied volatility (annualised).
        right: 'call' or 'put'.
        q:     Continuous dividend yield (annualised).

    Returns:
        BSResult with price and all first-order Greeks.
    """
    d1, d2 = _d1d2(S, K, r, q, sigma, T)
    sqrt_T = math.sqrt(T)

    discount = math.exp(-r * T)
    div_disc = math.exp(-q * T)

    if right == "call":
        price = S * div_disc * _norm_cdf(d1) - K * discount * _norm_cdf(d2)
        delta = div_disc * _norm_cdf(d1)
        intrinsic = max(S - K, 0.0)
        rho = K * T * discount * _norm_cdf(d2) / 100.0
    else:
        price = K * discount * _norm_cdf(-d2) - S * div_disc * _norm_cdf(-d1)
        delta = -div_disc * _norm_cdf(-d1)
        intrinsic = max(K - S, 0.0)
        rho = -K * T * discount * _norm_cdf(-d2) / 100.0

    # Greeks shared between call and put
    pdf_d1 = _norm_pdf(d1)
    gamma = div_disc * pdf_d1 / (S * sigma * sqrt_T)
    vega = S * div_disc * pdf_d1 * sqrt_T / 100.0        # per 1pp vol move
    # Theta: decay per calendar day
    theta_annual = (
        -(S * div_disc * pdf_d1 * sigma) / (2 * sqrt_T)
        - r * K * discount * (_norm_cdf(d2) if right == "call" else -_norm_cdf(-d2))
        + q * S * div_disc * (_norm_cdf(d1) if right == "call" else -_norm_cdf(-d1))
    )
    theta = theta_annual / _CALENDAR_DAYS_PER_YEAR

    return BSResult(
        price=price,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        rho=rho,
        d1=d1,
        d2=d2,
        intrinsic=intrinsic,
        time_value=price - intrinsic,
    )


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    right: OptionRight = "call",
    q: float = 0.0,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float:
    """
    Newton-Raphson implied volatility solver.

    Args:
        market_price: Observed option market price.
        S, K, T, r, q, right: Same as black_scholes().
        tol:          Convergence tolerance on |price_diff|.
        max_iter:     Maximum Newton iterations.

    Returns:
        Implied volatility (annualised, as a fraction).

    Raises:
        ValueError if the algorithm fails to converge.
    """
    # Initial guess: Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2 * math.pi / T) * market_price / S

    for _ in range(max_iter):
        result = black_scholes(S, K, T, r, sigma, right, q)
        diff = result.price - market_price
        if abs(diff) < tol:
            return sigma
        # vega in per-unit terms (undo the /100 scaling)
        vega_raw = result.vega * 100.0
        if vega_raw < 1e-10:
            break
        sigma -= diff / vega_raw
        sigma = max(sigma, 1e-6)

    raise ValueError(
        f"IV solver did not converge for market_price={market_price} S={S} K={K} T={T}"
    )


@dataclass
class OptionPosition:
    """
    Open option position.

    Attributes:
        spec:       Option specification.
        contracts:  Signed number of contracts (+ve = long, -ve = short).
        entry_premium: Premium paid/received per share at entry.
        spot:       Current underlying spot price.
        r:          Current risk-free rate.
        sigma:      Current implied volatility.
    """
    spec: OptionSpec
    contracts: int
    entry_premium: float
    spot: float
    r: float
    sigma: float
    q: float = 0.0

    def _bs(self) -> BSResult:
        return black_scholes(
            self.spot, self.spec.strike, self.spec.T,
            self.r, self.sigma, self.spec.right, self.q,
        )

    def mark_to_market(self) -> float:
        """Current premium per share."""
        return self._bs().price

    def unrealized_pnl(self) -> float:
        """
        Total unrealised P&L.

        pnl = (current_premium - entry_premium) × contracts × multiplier
        long contracts pay premium, short receive.
        """
        current = self.mark_to_market()
        return (current - self.entry_premium) * self.contracts * self.spec.multiplier

    def portfolio_delta(self) -> float:
        """Dollar delta (sensitivity to $1 move in spot)."""
        return self._bs().delta * self.contracts * self.spec.multiplier

    def portfolio_gamma(self) -> float:
        """Dollar gamma per $1 move in spot."""
        return self._bs().gamma * self.contracts * self.spec.multiplier

    def portfolio_vega(self) -> float:
        """Dollar vega per 1pp vol move."""
        return self._bs().vega * self.contracts * self.spec.multiplier

    def portfolio_theta(self) -> float:
        """Dollar theta per calendar day."""
        return self._bs().theta * self.contracts * self.spec.multiplier

    def required_margin(self) -> float:
        """
        CBOE Regulation T margin for short options (long = premium paid).

        Short option margin = 20% of underlying value + premium - OTM amount
        (simplified approach; broker-specific rules vary).
        """
        if self.contracts >= 0:
            # Long option: max loss = premium paid
            return self.entry_premium * abs(self.contracts) * self.spec.multiplier
        # Short option: 20% of spot + premium received - OTM amount
        otm = max(0.0, self.spec.strike - self.spot) if self.spec.right == "call" else max(0.0, self.spot - self.spec.strike)
        margin_per_share = 0.20 * self.spot + self.mark_to_market() - otm
        return max(margin_per_share, 0.10 * self.spec.strike) * abs(self.contracts) * self.spec.multiplier


# ══════════════════════════════════════════════════════════════════════════════
#  Multi-Asset Portfolio Aggregation
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PortfolioGreeks:
    """Aggregate Greek exposures across all option positions."""
    net_delta: float = 0.0
    net_gamma: float = 0.0
    net_vega: float = 0.0
    net_theta: float = 0.0
    total_pnl: float = 0.0
    total_margin: float = 0.0
    total_notional: float = 0.0


class MultiAssetPortfolio:
    """
    Cross-asset portfolio aggregator.

    Holds equity positions (shares × price), FX, futures, and options.
    Computes aggregate P&L, margin, and net Greeks in a single currency (USD).

    Attributes:
        base_currency: Reporting currency (default 'USD').
    """

    def __init__(self, base_currency: str = "USD") -> None:
        self.base_currency = base_currency
        self._fx: list[FXPosition] = []
        self._futures: list[FuturesPosition] = []
        self._options: list[OptionPosition] = []
        # Simple equity: {symbol: (shares, entry_price, current_price)}
        self._equity: dict[str, tuple[int, float, float]] = {}

    # ── Position Management ────────────────────────────────────────────────

    def add_equity(self, symbol: str, shares: int, entry: float, current: float) -> None:
        self._equity[symbol] = (shares, entry, current)

    def add_fx(self, pos: FXPosition) -> None:
        self._fx.append(pos)

    def add_futures(self, pos: FuturesPosition) -> None:
        self._futures.append(pos)

    def add_option(self, pos: OptionPosition) -> None:
        self._options.append(pos)

    def remove_equity(self, symbol: str) -> None:
        self._equity.pop(symbol, None)

    # ── P&L ───────────────────────────────────────────────────────────────

    def equity_pnl(self) -> float:
        return sum(
            (cur - entry) * shares
            for shares, entry, cur in self._equity.values()
        )

    def fx_pnl(self) -> float:
        return sum(p.total_pnl() for p in self._fx)

    def futures_pnl(self) -> float:
        return sum(p.unrealized_pnl() for p in self._futures)

    def options_pnl(self) -> float:
        return sum(p.unrealized_pnl() for p in self._options)

    def total_pnl(self) -> float:
        return self.equity_pnl() + self.fx_pnl() + self.futures_pnl() + self.options_pnl()

    # ── Notional ──────────────────────────────────────────────────────────

    def equity_notional(self) -> float:
        return sum(abs(shares) * cur for shares, _, cur in self._equity.values())

    def fx_notional(self) -> float:
        return sum(p.notional() for p in self._fx)

    def futures_notional(self) -> float:
        return sum(p.notional() for p in self._futures)

    def options_notional(self) -> float:
        return sum(abs(p.contracts) * p.spec.multiplier * p.spot for p in self._options)

    def gross_notional(self) -> float:
        return (
            self.equity_notional()
            + self.fx_notional()
            + self.futures_notional()
            + self.options_notional()
        )

    # ── Margin ────────────────────────────────────────────────────────────

    def equity_margin(self, reg_t_rate: float = 0.50) -> float:
        """Reg-T initial margin: 50% of long market value, 150% of short."""
        margin = 0.0
        for shares, _, cur in self._equity.values():
            notional = shares * cur
            if notional > 0:
                margin += notional * reg_t_rate
            else:
                margin += abs(notional) * 1.50  # short margin requirement
        return margin

    def fx_margin(self) -> float:
        return sum(p.required_margin() for p in self._fx)

    def futures_margin(self) -> float:
        return sum(p.required_margin() for p in self._futures)

    def options_margin(self) -> float:
        return sum(p.required_margin() for p in self._options)

    def total_margin(self) -> float:
        """Sum of all margin requirements across asset classes."""
        return (
            self.equity_margin()
            + self.fx_margin()
            + self.futures_margin()
            + self.options_margin()
        )

    # ── Greeks (options only) ─────────────────────────────────────────────

    def aggregate_greeks(self) -> PortfolioGreeks:
        g = PortfolioGreeks()
        for pos in self._options:
            g.net_delta += pos.portfolio_delta()
            g.net_gamma += pos.portfolio_gamma()
            g.net_vega += pos.portfolio_vega()
            g.net_theta += pos.portfolio_theta()
        g.total_pnl = self.total_pnl()
        g.total_margin = self.total_margin()
        g.total_notional = self.gross_notional()
        return g

    # ── Summary ───────────────────────────────────────────────────────────

    def summary(self) -> dict:
        greeks = self.aggregate_greeks()
        return {
            "base_currency": self.base_currency,
            "pnl": {
                "equity": round(self.equity_pnl(), 2),
                "fx": round(self.fx_pnl(), 2),
                "futures": round(self.futures_pnl(), 2),
                "options": round(self.options_pnl(), 2),
                "total": round(self.total_pnl(), 2),
            },
            "notional": {
                "equity": round(self.equity_notional(), 2),
                "fx": round(self.fx_notional(), 2),
                "futures": round(self.futures_notional(), 2),
                "options": round(self.options_notional(), 2),
                "gross": round(self.gross_notional(), 2),
            },
            "margin": {
                "equity": round(self.equity_margin(), 2),
                "fx": round(self.fx_margin(), 2),
                "futures": round(self.futures_margin(), 2),
                "options": round(self.options_margin(), 2),
                "total": round(self.total_margin(), 2),
            },
            "greeks": {
                "net_delta": round(greeks.net_delta, 4),
                "net_gamma": round(greeks.net_gamma, 6),
                "net_vega": round(greeks.net_vega, 4),
                "net_theta": round(greeks.net_theta, 4),
            },
            "positions": {
                "equity_count": len(self._equity),
                "fx_count": len(self._fx),
                "futures_count": len(self._futures),
                "options_count": len(self._options),
            },
        }
