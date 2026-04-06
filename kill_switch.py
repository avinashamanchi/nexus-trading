#!/usr/bin/env python3
"""
Kill Switch — Emergency position flatten and system halt.

Per §7.1 and §8.3:
- Operates INDEPENDENTLY of the message bus (direct broker API access)
- Cancels all open orders
- Flattens all positions
- Requires HGL sign-off before system can restart

Usage (from terminal, even if the main system is running):
    python kill_switch.py
    python kill_switch.py --dry-run     # show positions without flattening
    python kill_switch.py --confirm     # skip interactive prompt
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kill_switch")


async def run_kill_switch(dry_run: bool = False, skip_confirm: bool = False) -> None:
    logger.critical("=== KILL SWITCH ACTIVATED ===")

    # Direct broker connection — no message bus
    from brokers.alpaca import AlpacaBroker

    mode = os.environ.get("TRADING_MODE", "paper")
    paper = mode != "live"

    broker = AlpacaBroker(
        api_key=os.environ.get("ALPACA_API_KEY", ""),
        secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
        paper=paper,
    )

    try:
        await broker.connect()
        logger.info("Broker connected (%s)", "paper" if paper else "LIVE")
    except Exception as exc:
        logger.critical("BROKER CONNECTION FAILED: %s", exc)
        sys.exit(1)

    # Show current state
    account = await broker.get_account()
    positions = await broker.get_positions()
    orders = await broker.get_open_orders()

    logger.info("Account equity: $%.2f", account.equity)
    logger.info("Open positions: %d", len(positions))
    logger.info("Open orders: %d", len(orders))

    for pos in positions:
        logger.info(
            "  POSITION: %s %d shares @ $%.2f (P/L: $%.2f)",
            pos.symbol, pos.qty, pos.avg_entry_price, pos.unrealized_pnl,
        )

    for order in orders:
        logger.info(
            "  ORDER: %s %s x%d %s",
            order.symbol, order.side, order.qty, order.status,
        )

    if dry_run:
        logger.info("DRY RUN — no orders cancelled or positions closed")
        return

    if not skip_confirm:
        confirm = input(
            f"\n⚠  About to cancel {len(orders)} orders and flatten "
            f"{len(positions)} positions.\n"
            "   Type 'FLATTEN' to confirm: "
        ).strip()
        if confirm != "FLATTEN":
            logger.info("Kill switch aborted — no action taken")
            return

    # Your name for the audit log
    operator = input("Operator name (for audit log): ").strip() or "unknown"

    # Cancel all orders first
    logger.warning("Cancelling all open orders...")
    cancelled = await broker.cancel_all_orders()
    logger.warning("Cancelled %d orders", cancelled)

    # Flatten all positions
    logger.critical("FLATTENING ALL POSITIONS...")
    closed = await broker.close_all_positions()
    logger.critical("Closed %d positions", len(closed))

    # Write to audit log (direct, no message bus needed)
    try:
        from infrastructure.audit_log import AuditLog, system_halt_event
        from pathlib import Path

        audit = AuditLog()
        await audit.initialize()
        await audit.record(system_halt_event(
            "kill_switch",
            f"Emergency flatten by {operator}: {len(positions)} positions, {len(orders)} orders",
        ))
        await audit.close()
        logger.info("Audit log updated")
    except Exception as exc:
        logger.error("Could not write to audit log: %s — manual record required", exc)

    logger.critical("Kill switch complete — system halted")
    logger.critical("To restart: obtain HGL sign-off and run main.py")

    await broker.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Emergency kill switch")
    parser.add_argument("--dry-run", action="store_true", help="Show state without flattening")
    parser.add_argument("--confirm", action="store_true", help="Skip interactive confirmation")
    args = parser.parse_args()

    asyncio.run(run_kill_switch(dry_run=args.dry_run, skip_confirm=args.confirm))
