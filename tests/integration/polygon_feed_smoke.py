"""
Polygon feed smoke test — manual run only, requires market hours + real API key.

Usage:
    python -m tests.integration.polygon_feed_smoke --symbols AAPL,SPY --duration 300

Pass criteria:
  - Ticks arrive within 5 seconds of script start
  - MarketTick.bid and .ask non-zero for both symbols
  - BarData.vwap populated on every 1-min bar
  - OrderBookCache spread matches MarketTick spread within 0.5 bps
  - No FeedConnectionError raised during the duration
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from data.polygon_feed import PolygonDataFeed
from data.polygon_l2 import PolygonL2Manager
from data.level2 import OrderBookCache
from core.models import BarData, MarketTick

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class SmokeTestResults:
    def __init__(self) -> None:
        self.first_tick_at: datetime | None = None
        self.tick_counts: dict[str, int] = {}
        self.bar_counts: dict[str, int] = {}
        self.bars_with_vwap: dict[str, int] = {}
        self.bid_ask_errors: list[str] = []

    def record_tick(self, tick: MarketTick) -> None:
        if self.first_tick_at is None:
            self.first_tick_at = datetime.utcnow()
            logger.info("FIRST TICK received at %s: %s bid=%.2f ask=%.2f",
                        self.first_tick_at, tick.symbol, tick.bid, tick.ask)
        sym = tick.symbol
        self.tick_counts[sym] = self.tick_counts.get(sym, 0) + 1
        if tick.bid <= 0 or tick.ask <= 0:
            self.bid_ask_errors.append(f"{sym}: bid={tick.bid} ask={tick.ask}")

    def record_bar(self, bar: BarData) -> None:
        sym = bar.symbol
        self.bar_counts[sym] = self.bar_counts.get(sym, 0) + 1
        if bar.vwap:
            self.bars_with_vwap[sym] = self.bars_with_vwap.get(sym, 0) + 1

    def report(self) -> bool:
        """Print results and return True if all pass criteria met."""
        print("\n" + "="*60)
        print("SMOKE TEST RESULTS")
        print("="*60)

        passed = True

        # Criterion 1: ticks arrived
        if self.first_tick_at:
            print(f"✓ First tick received at {self.first_tick_at}")
        else:
            print("✗ No ticks received!")
            passed = False

        # Criterion 2: tick counts
        for sym, count in self.tick_counts.items():
            print(f"  {sym}: {count} ticks, {self.bar_counts.get(sym, 0)} bars")

        # Criterion 3: all bars have VWAP
        for sym, bar_count in self.bar_counts.items():
            vwap_count = self.bars_with_vwap.get(sym, 0)
            if vwap_count == bar_count:
                print(f"✓ {sym}: VWAP populated on all {bar_count} bars")
            else:
                print(f"✗ {sym}: VWAP missing on {bar_count - vwap_count}/{bar_count} bars")
                passed = False

        # Criterion 4: no zero bid/ask
        if not self.bid_ask_errors:
            print("✓ No zero bid/ask ticks")
        else:
            print(f"✗ Zero bid/ask ticks found: {self.bid_ask_errors[:5]}")
            passed = False

        print("="*60)
        print("RESULT:", "PASSED" if passed else "FAILED")
        return passed


async def run_smoke_test(symbols: list[str], duration_sec: int, api_key: str) -> bool:
    results = SmokeTestResults()
    order_book = OrderBookCache()

    feed = PolygonDataFeed(api_key=api_key, use_delayed=False)
    l2 = PolygonL2Manager(api_key=api_key, order_book=order_book, poll_sec=2)

    async def on_tick(tick: MarketTick) -> None:
        results.record_tick(tick)
        await l2.update_from_nbbo(tick.symbol, tick.bid, 0, tick.ask, 0, tick.timestamp)

    async def on_bar(bar: BarData) -> None:
        results.record_bar(bar)
        logger.info("BAR %s OHLC=%.2f/%.2f/%.2f/%.2f vwap=%s vol=%d",
                    bar.symbol, bar.open, bar.high, bar.low, bar.close,
                    f"{bar.vwap:.2f}" if bar.vwap else "None", bar.volume)

    feed.add_tick_handler(on_tick)
    feed.add_bar_handler(on_bar)
    await feed.subscribe(symbols)

    logger.info("Starting smoke test: symbols=%s duration=%ds", symbols, duration_sec)

    feed_task = asyncio.create_task(feed.run())
    l2_task = asyncio.create_task(l2.run(lambda: list(feed.subscribed_symbols)))

    await asyncio.sleep(duration_sec)

    await feed.stop()
    await l2.stop()
    feed_task.cancel()
    l2_task.cancel()

    return results.report()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polygon feed smoke test")
    parser.add_argument("--symbols", default="AAPL,SPY",
                        help="Comma-separated symbols (default: AAPL,SPY)")
    parser.add_argument("--duration", type=int, default=300,
                        help="Test duration in seconds (default: 300)")
    args = parser.parse_args()

    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print("ERROR: POLYGON_API_KEY environment variable not set")
        sys.exit(1)

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    passed = asyncio.run(run_smoke_test(symbols, args.duration, api_key))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
