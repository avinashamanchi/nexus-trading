#!/usr/bin/env python3
"""
Autonomous Cooperative-AI Day Trading System
Main entry point.

Usage:
    python main.py                    # run in mode set in config.yaml / TRADING_MODE env var
    python main.py --mode shadow      # force shadow mode
    python main.py --mode live        # force live trading (requires HGL sign-off)
    python main.py --mode paper       # Alpaca paper trading
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)-35s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/trading.log", mode="a"),
        ],
    )
    # Suppress noisy third-party loggers
    for lib in ("httpx", "httpcore", "alpaca", "websockets", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


async def main(args: argparse.Namespace) -> None:
    _setup_logging(args.log_level)
    logger = logging.getLogger("main")

    # Override mode via CLI
    if args.mode:
        os.environ["TRADING_MODE"] = args.mode

    mode = os.environ.get("TRADING_MODE", "shadow")
    logger.info("Starting trading system in %s mode", mode.upper())

    # Safety gate: live mode requires explicit confirmation
    if mode == "live":
        confirm = input(
            "\n⚠  LIVE TRADING MODE — real capital at risk.\n"
            "   Type 'I UNDERSTAND' to proceed: "
        ).strip()
        if confirm != "I UNDERSTAND":
            logger.info("Live mode confirmation failed — aborting")
            return

    from pipeline.coordinator import TradingSystemCoordinator

    coordinator = TradingSystemCoordinator(config_path=args.config)
    await coordinator.initialize()
    await coordinator.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Autonomous Cooperative-AI Day Trading System"
    )
    parser.add_argument(
        "--mode",
        choices=["shadow", "paper", "live"],
        default=None,
        help="Override trading mode (default: from config.yaml or TRADING_MODE env var)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to configuration file",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    # Ensure log directory exists
    Path("logs").mkdir(exist_ok=True)

    asyncio.run(main(args))
