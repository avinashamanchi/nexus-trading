"""
Nightly Market Data Pipeline — yfinance integration

Downloads free end-of-day data for the universe and runs the
regime + signal pipeline. Results written to db/daily_signals.json
so the dashboard shows "today's signals" even in shadow mode.

Runs nightly at 18:00 ET (after market close) via:
  python -m data.market_data --run-nightly

Or directly:
  python data/market_data.py
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SIGNALS_DB_PATH = Path(__file__).parent.parent / "db" / "daily_signals.json"

UNIVERSE_SYMBOLS = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AMD",
    # Broad market ETFs
    "SPY", "QQQ", "IWM", "GLD", "TLT", "UNG",
    # Financials
    "JPM", "BAC", "GS", "V", "MA",
    # Energy
    "XOM", "CVX", "COP",
    # Healthcare
    "UNH", "JNJ", "LLY",
]


@dataclass
class SignalRow:
    symbol: str
    close: float
    volume: int
    momentum_20d: float    # 20-day price momentum %
    vol_20d: float         # 20-day realized volatility (annualized)
    rsi_14: float          # RSI(14)
    score: float           # composite score: momentum * (1/vol) * rsi_factor
    signal: str            # "LONG" | "SHORT" | "NEUTRAL"
    regime_compatible: bool  # would this signal pass MRA regime gate?


# ---------------------------------------------------------------------------
# Signal computation helpers
# ---------------------------------------------------------------------------

def _compute_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder RSI. Returns 50.0 if insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0.0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_momentum(closes: list[float], period: int = 20) -> float:
    """Simple price momentum over `period` days, expressed as a percentage."""
    if len(closes) < period + 1:
        return 0.0
    return (closes[-1] / closes[-(period + 1)] - 1.0) * 100.0


def _compute_vol(closes: list[float], period: int = 20) -> float:
    """Annualized realized volatility from daily log-returns. Defaults to 20%."""
    if len(closes) < period + 1:
        return 0.20
    returns = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(len(closes) - period, len(closes))
    ]
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    return math.sqrt(var * 252)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def fetch_signals(symbols: list[str] | None = None) -> list[SignalRow]:
    """
    Download 60 days of EOD OHLCV data via yfinance and compute signals.

    Returns a list of SignalRow sorted by composite score descending.
    Returns an empty list if yfinance is unavailable or all symbols fail.

    The composite score rewards high momentum, low realized volatility,
    and RSI that is neither overbought nor oversold:

        score = momentum_20d * (0.20 / vol_20d) * rsi_factor

    where rsi_factor peaks at RSI=50 and falls symmetrically to zero
    at RSI=0 or RSI=100:

        rsi_factor = 1.0 - |rsi - 50| / 50
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed — run: pip install yfinance")
        return []

    if symbols is None:
        symbols = UNIVERSE_SYMBOLS

    rows: list[SignalRow] = []

    try:
        # Batch download — much faster than one ticker at a time.
        raw = yf.download(
            symbols,
            period="60d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.error(f"yfinance batch download failed: {exc}")
        return []

    for sym in symbols:
        try:
            # yfinance returns a flat DataFrame for a single ticker.
            if len(symbols) == 1:
                closes_series = raw["Close"]
                volumes_series = raw["Volume"]
            else:
                closes_series = raw["Close"][sym]
                volumes_series = raw["Volume"][sym]

            closes  = closes_series.dropna().tolist()
            volumes = volumes_series.dropna().tolist()

            # Need at least 22 bars for a 20-day window plus one lookback bar.
            if len(closes) < 22:
                logger.debug(f"Skipping {sym}: only {len(closes)} bars (need 22)")
                continue

            close    = float(closes[-1])
            volume   = int(volumes[-1]) if volumes else 0
            momentum = _compute_momentum(closes, 20)
            vol      = _compute_vol(closes, 20)
            rsi      = _compute_rsi(closes, 14)

            # Composite score (see docstring).
            rsi_factor = 1.0 - abs(rsi - 50.0) / 50.0
            score = momentum * (0.20 / max(vol, 0.05)) * rsi_factor

            # Signal thresholds: require score > 2.0 and RSI not overbought
            # for LONG; score < -2.0 and RSI not oversold for SHORT.
            if score > 2.0 and rsi < 65.0:
                signal = "LONG"
            elif score < -2.0 and rsi > 35.0:
                signal = "SHORT"
            else:
                signal = "NEUTRAL"

            # Simplified regime compatibility: any directional signal is
            # considered compatible. A real integration would call MRA.
            regime_compatible = signal != "NEUTRAL"

            rows.append(SignalRow(
                symbol=sym,
                close=round(close, 2),
                volume=volume,
                momentum_20d=round(momentum, 3),
                vol_20d=round(vol, 4),
                rsi_14=round(rsi, 2),
                score=round(score, 4),
                signal=signal,
                regime_compatible=regime_compatible,
            ))

        except Exception as exc:
            logger.debug(f"Skipping {sym}: {exc}")
            continue

    return sorted(rows, key=lambda r: r.score, reverse=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_signals(rows: list[SignalRow]) -> None:
    """
    Persist signals to db/daily_signals.json using an atomic write
    (write to .tmp then rename) so the dashboard never reads a partial file.
    """
    SIGNALS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": date.today().isoformat(),
        "total_symbols": len(rows),
        "long_signals": sum(1 for r in rows if r.signal == "LONG"),
        "short_signals": sum(1 for r in rows if r.signal == "SHORT"),
        "signals": [r.__dict__ for r in rows],
    }
    tmp = SIGNALS_DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(SIGNALS_DB_PATH)
    logger.info(f"Saved {len(rows)} signals to {SIGNALS_DB_PATH}")


def load_signals() -> dict:
    """
    Load previously saved signals from disk.
    Returns an empty structure if the file does not exist or is corrupt.
    """
    if SIGNALS_DB_PATH.exists():
        try:
            return json.loads(SIGNALS_DB_PATH.read_text())
        except Exception as exc:
            logger.warning(f"Failed to load signals from {SIGNALS_DB_PATH}: {exc}")
    return {
        "generated_at": None,
        "date": None,
        "total_symbols": 0,
        "long_signals": 0,
        "short_signals": 0,
        "signals": [],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_nightly() -> None:
    """
    Entry point for the nightly cron job.

    Typical invocation (add to crontab for 18:00 ET):
        0 18 * * 1-5 cd /path/to/trading && python -m data.market_data
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger.info("Starting nightly signal generation...")
    rows = fetch_signals()
    if rows:
        save_signals(rows)
        long_count  = sum(1 for r in rows if r.signal == "LONG")
        short_count = sum(1 for r in rows if r.signal == "SHORT")
        logger.info(
            f"Done: {len(rows)} symbols scored · "
            f"{long_count} LONG · {short_count} SHORT"
        )
    else:
        logger.warning(
            "No signals generated — yfinance unavailable or all symbols failed. "
            "Run: pip install yfinance"
        )


if __name__ == "__main__":
    run_nightly()
