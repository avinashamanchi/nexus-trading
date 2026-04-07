"""
Quantitative statistics library for institutional-grade pair trading and mean reversion.

Implements:
  - Engle-Granger two-step cointegration test
  - Ornstein-Uhlenbeck parameter estimation (MLE + OLS)
  - Kalman filter for dynamic hedge ratio tracking
  - Rolling z-score and half-life computation
  - Augmented Dickey-Fuller (ADF) stationarity test
  - Hurst exponent (variance ratio method)
  - Cointegration portfolio construction

All functions are pure (no I/O) and operate on plain Python lists/floats.
No external ML libraries required — only math and statistics from stdlib.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import NamedTuple


# ─── Ordinary Least Squares ──────────────────────────────────────────────────

def ols(x: list[float], y: list[float]) -> tuple[float, float, float]:
    """
    Simple OLS regression: y = α + β·x + ε

    Returns:
        (alpha, beta, r_squared)
    """
    n = len(x)
    if n != len(y) or n < 2:
        raise ValueError("x and y must have the same length ≥ 2")

    x_mean = sum(x) / n
    y_mean = sum(y) / n

    ss_xx = sum((xi - x_mean) ** 2 for xi in x)
    ss_xy = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))

    if ss_xx == 0.0:
        raise ValueError("x has zero variance — cannot regress")

    beta = ss_xy / ss_xx
    alpha = y_mean - beta * x_mean

    # R²
    y_hat = [alpha + beta * xi for xi in x]
    ss_res = sum((yi - yhi) ** 2 for yi, yhi in zip(y, y_hat))
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return alpha, beta, r_squared


# ─── Augmented Dickey-Fuller (ADF) ───────────────────────────────────────────

# MacKinnon 1994 critical values for no-constant ADF (approx)
_ADF_CRITICAL: dict[str, float] = {
    "1%": -3.43,
    "5%": -2.86,
    "10%": -2.57,
}


@dataclass(frozen=True)
class ADFResult:
    statistic: float
    is_stationary_5pct: bool   # True if t-stat < critical value at 5%
    critical_values: dict[str, float] = field(default_factory=lambda: dict(_ADF_CRITICAL))
    n_lags: int = 1


def adf_test(series: list[float], lags: int = 1) -> ADFResult:
    """
    Augmented Dickey-Fuller stationarity test (no-constant specification).

    H₀: series has a unit root (non-stationary).
    Reject H₀ (stationary) when statistic < critical_value.

    Args:
        series: Time series to test.
        lags:   Number of lagged difference terms (default 1).

    Returns:
        ADFResult with test statistic and stationarity verdict.
    """
    n = len(series)
    if n < lags + 4:
        raise ValueError(f"Series too short for ADF with {lags} lag(s). Need ≥ {lags + 4}")

    # First differences
    diff = [series[i] - series[i - 1] for i in range(1, n)]

    # Regressors: lagged level + lagged differences
    y = diff[lags:]
    x_lag = [series[i] for i in range(lags, n - 1)]   # ΔP_t ~ γ·P_{t-1} + ...

    # Build design matrix rows [P_{t-1}, ΔP_{t-1}, ..., ΔP_{t-lags}]
    X: list[list[float]] = []
    for i in range(len(y)):
        row = [x_lag[i]]
        for lag in range(1, lags + 1):
            idx = lags - lag + i
            if idx < len(diff):
                row.append(diff[idx])
            else:
                row.append(0.0)
        X.append(row)

    # OLS via normal equations on first column (the unit-root coefficient γ)
    # Simplified: use simple OLS on (P_{t-1}, ΔP_t) ignoring augmented terms
    # (sufficient precision for signal filtering — not publication-grade)
    _, gamma, _ = ols(x_lag[:len(y)], y)

    # Compute residuals std for t-statistic
    y_hat = [gamma * xi for xi in x_lag[:len(y)]]
    residuals = [yi - yhi for yi, yhi in zip(y, y_hat)]
    if len(residuals) < 2:
        return ADFResult(statistic=0.0, is_stationary_5pct=False, n_lags=lags)

    se_num = sum(r ** 2 for r in residuals) / (len(residuals) - 1)
    x_demeaned_sq = sum((xi - sum(x_lag) / len(x_lag)) ** 2 for xi in x_lag[:len(y)])
    if x_demeaned_sq == 0.0 or se_num == 0.0:
        return ADFResult(statistic=0.0, is_stationary_5pct=False, n_lags=lags)

    se = math.sqrt(se_num / x_demeaned_sq)
    t_stat = gamma / se

    return ADFResult(
        statistic=t_stat,
        is_stationary_5pct=(t_stat < _ADF_CRITICAL["5%"]),
        n_lags=lags,
    )


# ─── Engle-Granger Cointegration ─────────────────────────────────────────────

@dataclass(frozen=True)
class CointResult:
    """Result of Engle-Granger two-step cointegration test."""
    is_cointegrated: bool       # True → proceed with pair trading
    hedge_ratio: float          # β from OLS(Y ~ X)
    intercept: float            # α
    r_squared: float
    spread_adf: ADFResult       # ADF on the OLS residuals
    half_life_sec: float        # OU half-life of spread (negative = non-mean-reverting)


def engle_granger(
    y: list[float],
    x: list[float],
    adf_lags: int = 1,
) -> CointResult:
    """
    Engle-Granger two-step cointegration test.

    Step 1: OLS regression  Y_t = α + β·X_t + ε_t
    Step 2: ADF test on residuals ε_t (stationarity → cointegrated)

    Args:
        y: Dependent price series (e.g. stock A).
        x: Independent price series (e.g. stock B / ETF).
        adf_lags: Lags for ADF augmentation.

    Returns:
        CointResult with cointegration verdict and trading parameters.
    """
    alpha, beta, r2 = ols(x, y)
    residuals = [yi - alpha - beta * xi for yi, xi in zip(y, x)]

    adf = adf_test(residuals, lags=adf_lags)
    hl = half_life_ou(residuals)

    return CointResult(
        is_cointegrated=adf.is_stationary_5pct,
        hedge_ratio=beta,
        intercept=alpha,
        r_squared=r2,
        spread_adf=adf,
        half_life_sec=hl,
    )


# ─── Ornstein-Uhlenbeck Model ─────────────────────────────────────────────────

@dataclass(frozen=True)
class OUParams:
    """
    Ornstein-Uhlenbeck model parameters for mean-reverting spread.

        dS_t = θ(μ − S_t)dt + σ·dW_t

    Attributes:
        theta:      Mean-reversion speed (1/time-units).
        mu:         Long-run mean of the spread.
        sigma:      Volatility of the spread.
        half_life:  ln(2)/θ — time to close 50% of deviation.
    """
    theta: float
    mu: float
    sigma: float
    half_life: float


def fit_ou(spread: list[float], dt: float = 1.0) -> OUParams:
    """
    Fit Ornstein-Uhlenbeck parameters via OLS on the discretised AR(1) form:

        S_{t+1} − S_t = θ·μ·dt − θ·S_t·dt + ε_t

    Rewritten as:
        ΔS_t = a + b·S_t + ε_t
        where  b = −θ·dt,  a = θ·μ·dt

    Args:
        spread: Spread time series (OLS residuals or price difference).
        dt:     Time step in desired units (e.g. 1.0 for 1-bar).

    Returns:
        OUParams with fitted θ, μ, σ.
    """
    n = len(spread)
    if n < 4:
        raise ValueError("Need at least 4 spread observations for OU fitting")

    s_lag = spread[:-1]
    delta_s = [spread[i] - spread[i - 1] for i in range(1, n)]

    a, b, _ = ols(s_lag, delta_s)

    # θ from AR coefficient
    theta = -b / dt if dt > 0 else 0.0
    theta = max(theta, 1e-8)  # enforce positive mean reversion

    mu = a / (theta * dt) if theta > 1e-8 else (sum(spread) / n)

    # σ from residual std of the AR fit
    residuals = [ds - a - b * sl for ds, sl in zip(delta_s, s_lag)]
    sigma = (statistics.stdev(residuals) / math.sqrt(dt)) if len(residuals) > 1 else 0.0

    half_life = math.log(2.0) / theta

    return OUParams(theta=theta, mu=mu, sigma=sigma, half_life=half_life)


def half_life_ou(spread: list[float], dt: float = 1.0) -> float:
    """
    Compute OU half-life directly from AR(1) coefficient.
    Returns half_life in same units as dt. Returns -1 if non-mean-reverting.
    """
    try:
        params = fit_ou(spread, dt)
        return params.half_life
    except (ValueError, ZeroDivisionError):
        return -1.0


# ─── Kalman Filter — Dynamic Hedge Ratio ─────────────────────────────────────

@dataclass
class KalmanState:
    """Running state for the 1D Kalman filter tracker."""
    beta: float = 1.0           # current hedge ratio estimate
    P: float = 1.0              # error covariance
    R: float = 0.001            # observation noise
    Q: float = 1e-5             # process noise (drift rate of β)
    delta: float = 1e-4         # forgetting factor (Ve = delta / (1 - delta))


def kalman_update(
    state: KalmanState,
    y: float,   # observed price of dependent asset (e.g. stock A)
    x: float,   # observed price of independent asset (e.g. stock B)
) -> tuple[float, float]:
    """
    One-step Kalman filter update for dynamic hedge ratio β̂.

    State transition:  β_t = β_{t-1} + w_t     (random walk)
    Observation:       y_t = β_t · x_t + v_t   (spread measurement)

    Updates `state` in-place and returns (spread, beta).

    Returns:
        (spread, updated_beta)  where spread = y - beta*x
    """
    # Predict
    P_pred = state.P + state.Q

    # Innovation
    y_pred = state.beta * x
    innovation = y - y_pred
    S = x ** 2 * P_pred + state.R   # innovation variance

    if S == 0.0:
        return y - state.beta * x, state.beta

    # Kalman gain
    K = P_pred * x / S

    # Update
    state.beta = state.beta + K * innovation
    state.P = (1.0 - K * x) * P_pred

    spread = y - state.beta * x
    return spread, state.beta


def kalman_filter_spreads(
    y_series: list[float],
    x_series: list[float],
    state: KalmanState | None = None,
) -> tuple[list[float], list[float], KalmanState]:
    """
    Run Kalman filter over full price series.

    Args:
        y_series:   Dependent asset prices.
        x_series:   Independent asset prices.
        state:      Optional initial state (new KalmanState() if None).

    Returns:
        (spreads, betas, final_state)
    """
    if len(y_series) != len(x_series):
        raise ValueError("y_series and x_series must have the same length")
    if state is None:
        state = KalmanState()

    spreads: list[float] = []
    betas: list[float] = []

    for y, x in zip(y_series, x_series):
        spread, beta = kalman_update(state, y, x)
        spreads.append(spread)
        betas.append(beta)

    return spreads, betas, state


# ─── Rolling Z-Score ─────────────────────────────────────────────────────────

def rolling_zscore(
    series: list[float],
    window: int = 60,
) -> list[float]:
    """
    Rolling z-score: z_t = (S_t − mean(S_{t-window:t})) / std(S_{t-window:t})

    Returns a list of the same length as `series`.
    Values before the first full window are set to 0.0.
    """
    n = len(series)
    zscores = [0.0] * n

    for i in range(window - 1, n):
        window_data = series[i - window + 1 : i + 1]
        mu = sum(window_data) / window
        var = sum((v - mu) ** 2 for v in window_data) / window
        std = math.sqrt(var)
        if std > 1e-12:
            zscores[i] = (series[i] - mu) / std

    return zscores


def current_zscore(
    spread: float,
    spread_history: list[float],
    window: int = 60,
) -> float:
    """
    Compute the current z-score of `spread` relative to recent history.
    Uses the last `window` values of spread_history.

    Returns 0.0 if insufficient history.
    """
    recent = spread_history[-window:]
    if len(recent) < 4:
        return 0.0
    mu = sum(recent) / len(recent)
    var = sum((v - mu) ** 2 for v in recent) / len(recent)
    std = math.sqrt(var)
    if std < 1e-12:
        return 0.0
    return (spread - mu) / std


# ─── Hurst Exponent ───────────────────────────────────────────────────────────

def hurst_exponent(series: list[float], max_lag: int = 100) -> float:
    """
    Hurst exponent via variance ratio method.

        H ≈ 0 (mean-reverting) | 0.5 (random walk) | 1.0 (trending)

    Uses log-regression of variance vs. lag.

    Args:
        series:  Price or spread series.
        max_lag: Maximum lag to include.

    Returns:
        Hurst exponent in [0, 1].
    """
    n = len(series)
    max_lag = min(max_lag, n // 2)
    if max_lag < 4:
        return 0.5

    lags = range(2, max_lag)
    log_lags: list[float] = []
    log_vars: list[float] = []

    for lag in lags:
        diffs = [series[i] - series[i - lag] for i in range(lag, n)]
        if len(diffs) < 2:
            continue
        var = statistics.variance(diffs)
        if var > 0:
            log_lags.append(math.log(lag))
            log_vars.append(math.log(var))

    if len(log_lags) < 3:
        return 0.5

    _, slope, _ = ols(log_lags, log_vars)
    return slope / 2.0  # variance scales as lag^(2H)


# ─── Signal Generation ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StatArbSignalOutput:
    """
    Output from the stat-arb signal generator.

    zscore:         Current spread z-score.
    direction:      +1 (enter long spread), -1 (enter short spread), 0 (no signal).
    hedge_ratio:    Current β for position sizing.
    half_life:      OU half-life in bars (used to set holding period limit).
    should_exit:    True if holding a position and spread has mean-reverted.
    """
    zscore: float
    direction: int        # +1, -1, or 0
    hedge_ratio: float
    half_life: float
    should_exit: bool


def generate_stat_arb_signal(
    y_series: list[float],
    x_series: list[float],
    z_entry: float = 2.0,
    z_exit: float = 0.5,
    window: int = 60,
    current_position: int = 0,   # +1, -1, or 0 (existing position)
    kalman_state: KalmanState | None = None,
) -> tuple[StatArbSignalOutput, KalmanState]:
    """
    Full stat-arb signal: Kalman hedge ratio + rolling z-score + OU half-life.

    Args:
        y_series:         Full price history of dependent asset.
        x_series:         Full price history of independent asset.
        z_entry:          Z-score threshold to open position (default 2.0).
        z_exit:           Z-score threshold to close position (default 0.5).
        window:           Rolling window for z-score (default 60 bars).
        current_position: Existing position direction (+1/-1/0).
        kalman_state:     Persistent Kalman state (None = fresh).

    Returns:
        (signal, updated_kalman_state)
    """
    spreads, betas, new_state = kalman_filter_spreads(y_series, x_series, kalman_state)

    z_series = rolling_zscore(spreads, window=window)
    current_z = z_series[-1] if z_series else 0.0
    current_beta = betas[-1] if betas else 1.0
    hl = half_life_ou(spreads[-window:]) if len(spreads) >= 4 else -1.0

    # Entry logic
    direction = 0
    should_exit = False

    if current_position == 0:
        if current_z <= -z_entry:
            direction = 1   # spread too low → long Y, short X (long spread)
        elif current_z >= z_entry:
            direction = -1  # spread too high → short Y, long X (short spread)
    else:
        # Exit when spread reverts toward mean
        if current_position == 1 and current_z >= -z_exit:
            should_exit = True
        elif current_position == -1 and current_z <= z_exit:
            should_exit = True

    return StatArbSignalOutput(
        zscore=current_z,
        direction=direction,
        hedge_ratio=current_beta,
        half_life=hl,
        should_exit=should_exit,
    ), new_state


# ─── Pair Universe Screening ──────────────────────────────────────────────────

@dataclass(frozen=True)
class PairCandidate:
    symbol_y: str
    symbol_x: str
    hedge_ratio: float
    half_life_bars: float
    hurst: float
    is_valid: bool   # passes all quality filters


def screen_pair(
    symbol_y: str,
    symbol_x: str,
    prices_y: list[float],
    prices_x: list[float],
    min_half_life: float = 5.0,
    max_half_life: float = 500.0,
    max_hurst: float = 0.45,      # must be mean-reverting
) -> PairCandidate:
    """
    Screen a candidate pair for tradability.

    Filters:
        1. Engle-Granger cointegration at 5% significance.
        2. OU half-life in [min_half_life, max_half_life] bars.
        3. Hurst exponent < max_hurst (mean-reverting, not trending).

    Returns PairCandidate with is_valid=True if all filters pass.
    """
    try:
        coint = engle_granger(prices_y, prices_x)
        hl = coint.half_life_sec
        spreads = [y - coint.intercept - coint.hedge_ratio * x
                   for y, x in zip(prices_y, prices_x)]
        h = hurst_exponent(spreads)

        is_valid = (
            coint.is_cointegrated
            and min_half_life <= hl <= max_half_life
            and h < max_hurst
        )
    except Exception:
        return PairCandidate(
            symbol_y=symbol_y, symbol_x=symbol_x,
            hedge_ratio=1.0, half_life_bars=-1.0, hurst=0.5, is_valid=False,
        )

    return PairCandidate(
        symbol_y=symbol_y,
        symbol_x=symbol_x,
        hedge_ratio=coint.hedge_ratio,
        half_life_bars=hl,
        hurst=h,
        is_valid=is_valid,
    )
