"""
Live Paper-Trading Performance Tracker

Tracks equity curve, trade statistics, and risk metrics for the
Alpaca paper-trading account. Persists to db/performance.json
so the React dashboard always shows real historical data.

Metrics computed:
  - Equity curve (daily close equity)
  - Sharpe ratio (annualized, rolling 252 days)
  - Max drawdown (peak-to-trough, %)
  - Win rate (winning trades / total closed trades)
  - Average win / average loss (expectancy)
  - Trades per day
  - Current streak (consecutive wins/losses)
"""

from dataclasses import dataclass, field
from pathlib import Path
import json
import time
import math
from typing import NamedTuple

PERF_DB_PATH = Path(__file__).parent.parent / "db" / "performance.json"


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    side: str           # "buy" or "sell"
    qty: int
    entry_price: float
    exit_price: float
    pnl: float          # realized P&L in dollars
    pnl_pct: float      # P&L as % of entry notional
    opened_at: str      # ISO timestamp
    closed_at: str      # ISO timestamp
    duration_min: float
    exit_reason: str    # from ExitMode


@dataclass
class DailyEquityPoint:
    date: str           # YYYY-MM-DD
    equity: float       # account equity at close
    daily_pnl: float
    daily_return: float # pnl / prev_equity


@dataclass
class PerformanceStats:
    start_date: str
    last_updated: str
    starting_equity: float
    current_equity: float
    total_pnl: float
    total_return_pct: float
    sharpe_ratio: float     # annualized, rolling
    sortino_ratio: float    # downside deviation only
    max_drawdown_pct: float # peak-to-trough
    current_drawdown_pct: float
    win_rate: float
    avg_win_usd: float
    avg_loss_usd: float
    expectancy_usd: float   # avg_win * win_rate - avg_loss * (1 - win_rate)
    total_trades: int
    winning_trades: int
    losing_trades: int
    trades_per_day: float
    current_streak: int     # positive = win streak, negative = loss streak
    best_trade_usd: float
    worst_trade_usd: float
    equity_curve: list      # list[DailyEquityPoint] — last 252 days
    recent_trades: list     # list[TradeRecord]      — last 20 trades


class PerformanceTracker:
    """
    Tracks and persists live trading performance.
    Thread-safe via asyncio (not multiprocessing).
    """

    def __init__(self, starting_equity: float = 30_000.0, db_path: Path = PERF_DB_PATH):
        self._db_path = db_path
        self._starting_equity = starting_equity
        self._trades: list[TradeRecord] = []
        self._equity_curve: list[DailyEquityPoint] = []
        self._current_equity = starting_equity
        self._load()

    def _load(self) -> None:
        """Load persisted data from JSON if it exists."""
        if self._db_path.exists():
            try:
                data = json.loads(self._db_path.read_text())
                self._trades = [TradeRecord(**t) for t in data.get("trades", [])]
                self._equity_curve = [DailyEquityPoint(**p) for p in data.get("equity_curve", [])]
                self._current_equity = data.get("current_equity", self._starting_equity)
            except Exception:
                pass  # corrupt file — start fresh

    def _save(self) -> None:
        """Persist to JSON atomically."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "current_equity": self._current_equity,
            "starting_equity": self._starting_equity,
            "trades": [t.__dict__ for t in self._trades],
            "equity_curve": [p.__dict__ for p in self._equity_curve],
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = self._db_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._db_path)  # atomic rename

    def record_trade(self, trade: TradeRecord) -> None:
        """Record a closed trade and update equity."""
        self._trades.append(trade)
        self._current_equity += trade.pnl
        self._save()

    def record_daily_equity(self, date: str, equity: float) -> None:
        """Call once per trading day at market close."""
        prev_equity = self._equity_curve[-1].equity if self._equity_curve else self._starting_equity
        daily_pnl = equity - prev_equity
        daily_return = daily_pnl / prev_equity if prev_equity > 0 else 0.0
        # Deduplicate by date
        if self._equity_curve and self._equity_curve[-1].date == date:
            self._equity_curve[-1] = DailyEquityPoint(date, equity, daily_pnl, daily_return)
        else:
            self._equity_curve.append(DailyEquityPoint(date, equity, daily_pnl, daily_return))
        # Keep last 504 days (2 years)
        if len(self._equity_curve) > 504:
            self._equity_curve = self._equity_curve[-504:]
        self._current_equity = equity
        self._save()

    def compute_stats(self) -> PerformanceStats:
        """Compute all performance statistics from stored data."""
        from datetime import datetime, timezone

        trades = self._trades
        curve = self._equity_curve

        # Basic P&L
        total_pnl = self._current_equity - self._starting_equity
        total_return_pct = total_pnl / self._starting_equity * 100 if self._starting_equity > 0 else 0.0

        # Win/loss stats
        winning = [t for t in trades if t.pnl > 0]
        losing  = [t for t in trades if t.pnl <= 0]
        total   = len(trades)
        win_rate = len(winning) / total if total > 0 else 0.0
        avg_win  = sum(t.pnl for t in winning) / len(winning) if winning else 0.0
        avg_loss = abs(sum(t.pnl for t in losing) / len(losing)) if losing else 0.0
        expectancy = avg_win * win_rate - avg_loss * (1 - win_rate)

        # Sharpe & Sortino
        returns = [p.daily_return for p in curve[-252:]] if len(curve) >= 5 else []
        if len(returns) >= 5:
            mean_r = sum(returns) / len(returns)
            std_r  = math.sqrt(sum((r - mean_r)**2 for r in returns) / len(returns))
            sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0
            neg_returns = [r for r in returns if r < 0]
            downside_std = math.sqrt(sum(r**2 for r in neg_returns) / len(neg_returns)) if neg_returns else std_r
            sortino = (mean_r / downside_std * math.sqrt(252)) if downside_std > 0 else 0.0
        else:
            sharpe = sortino = 0.0

        # Max drawdown
        peak = self._starting_equity
        max_dd = 0.0
        running_equity = self._starting_equity
        for p in curve:
            running_equity = p.equity
            if running_equity > peak:
                peak = running_equity
            dd = (peak - running_equity) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        current_dd = (peak - self._current_equity) / peak * 100 if peak > 0 else 0.0

        # Trades per day
        if curve and total > 0:
            try:
                start = datetime.fromisoformat(curve[0].date)
                end   = datetime.fromisoformat(curve[-1].date)
                days  = max(1, (end - start).days)
            except Exception:
                days = max(1, len(curve))
            trades_per_day = total / days
        else:
            trades_per_day = 0.0

        # Current streak
        streak = 0
        for t in reversed(trades[-20:]):
            if streak == 0:
                streak = 1 if t.pnl > 0 else -1
            elif streak > 0 and t.pnl > 0:
                streak += 1
            elif streak < 0 and t.pnl <= 0:
                streak -= 1
            else:
                break

        return PerformanceStats(
            start_date=curve[0].date if curve else time.strftime("%Y-%m-%d"),
            last_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            starting_equity=self._starting_equity,
            current_equity=round(self._current_equity, 2),
            total_pnl=round(total_pnl, 2),
            total_return_pct=round(total_return_pct, 3),
            sharpe_ratio=round(sharpe, 3),
            sortino_ratio=round(sortino, 3),
            max_drawdown_pct=round(max_dd, 3),
            current_drawdown_pct=round(current_dd, 3),
            win_rate=round(win_rate, 4),
            avg_win_usd=round(avg_win, 2),
            avg_loss_usd=round(avg_loss, 2),
            expectancy_usd=round(expectancy, 2),
            total_trades=total,
            winning_trades=len(winning),
            losing_trades=len(losing),
            trades_per_day=round(trades_per_day, 2),
            current_streak=streak,
            best_trade_usd=round(max((t.pnl for t in trades), default=0.0), 2),
            worst_trade_usd=round(min((t.pnl for t in trades), default=0.0), 2),
            equity_curve=curve[-90:],   # last 90 days for dashboard
            recent_trades=trades[-20:], # last 20 trades
        )

    def summary(self) -> dict:
        stats = self.compute_stats()
        return stats.__dict__ | {
            "equity_curve": [p.__dict__ for p in stats.equity_curve],
            "recent_trades": [t.__dict__ for t in stats.recent_trades],
        }
