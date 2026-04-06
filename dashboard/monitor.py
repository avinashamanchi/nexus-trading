"""
Real-time monitoring dashboard using Rich.

Displays live P/L, open positions, agent health, and system state.
Runs independently and reads from the state store — no message bus dependency.

Usage:
    python dashboard/monitor.py
    python dashboard/monitor.py --refresh 2  # refresh every 2 seconds
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from infrastructure.state_store import StateStore

console = Console()


class TradingDashboard:
    """Rich-based real-time monitoring dashboard."""

    def __init__(self, refresh_sec: float = 1.0) -> None:
        self._refresh = refresh_sec
        self._store = StateStore()
        self._running = True

    async def run(self) -> None:
        await self._store.initialize()
        with Live(self._build_layout(), refresh_per_second=1, screen=True) as live:
            while self._running:
                try:
                    layout = await self._update_layout()
                    live.update(layout)
                except KeyboardInterrupt:
                    self._running = False
                    break
                except Exception as exc:
                    console.print(f"[red]Dashboard error: {exc}[/red]")
                await asyncio.sleep(self._refresh)
        await self._store.close()

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )
        layout["body"]["left"].split_column(
            Layout(name="pnl", size=8),
            Layout(name="positions"),
        )
        layout["body"]["right"].split_column(
            Layout(name="system", size=8),
            Layout(name="agents"),
        )
        return layout

    async def _update_layout(self) -> Layout:
        layout = self._build_layout()

        # Fetch state
        session_date = datetime.utcnow().strftime("%Y-%m-%d")
        system_state, halt_reason, anomaly_hold = await self._store.load_system_state()
        positions = await self._store.load_open_positions()
        trades = await self._store.load_closed_trades(session_date)
        heartbeats = await self._store.load_all_heartbeats()

        # Header
        now = datetime.utcnow().strftime("%H:%M:%S UTC")
        layout["header"].update(Panel(
            Text(f"⚡ Autonomous Cooperative-AI Trading System  |  {now}  |  {session_date}", justify="center"),
            style="bold blue",
        ))

        # P/L panel
        layout["pnl"].update(self._pnl_panel(trades))

        # Positions panel
        layout["positions"].update(self._positions_panel(positions))

        # System state panel
        layout["system"].update(self._system_panel(system_state, halt_reason, anomaly_hold))

        # Agent health panel
        layout["agents"].update(self._agents_panel(heartbeats))

        # Footer
        layout["footer"].update(Panel(
            "Press Ctrl+C to exit  |  kill_switch.py for emergency flatten  |  §8.3 kill switch operates independently of this dashboard",
            style="dim",
        ))

        return layout

    def _pnl_panel(self, trades) -> Panel:
        wins = [t for t in trades if t.net_pnl > 0]
        total_pnl = sum(t.net_pnl for t in trades)
        win_rate = len(wins) / len(trades) if trades else 0

        color = "green" if total_pnl >= 0 else "red"
        text = Text()
        text.append(f"  Daily P/L: ", style="bold")
        text.append(f"${total_pnl:+,.2f}\n", style=f"bold {color}")
        text.append(f"  Trades: {len(trades)}  ", style="white")
        text.append(f"W: {len(wins)}  ", style="green")
        text.append(f"L: {len(trades)-len(wins)}  ", style="red")
        text.append(f"WR: {win_rate*100:.1f}%\n", style="cyan")

        return Panel(text, title="[bold]P/L[/bold]", border_style=color)

    def _positions_panel(self, positions) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
        table.add_column("Symbol", style="bold")
        table.add_column("Dir")
        table.add_column("Qty", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("P/L", justify="right")
        table.add_column("Time Left", justify="right")

        for pos in positions:
            pnl = pos.unrealized_pnl
            pnl_str = f"${pnl:+.2f}"
            pnl_style = "green" if pnl >= 0 else "red"
            secs_left = max(0, (pos.time_stop_at - datetime.utcnow()).total_seconds())
            dir_style = "green" if pos.direction.value == "long" else "red"

            table.add_row(
                pos.symbol,
                Text(pos.direction.value[0].upper(), style=dir_style),
                str(pos.shares),
                f"${pos.entry_price:.2f}",
                Text(pnl_str, style=pnl_style),
                f"{int(secs_left)}s",
            )

        if not positions:
            return Panel("[dim]No open positions[/dim]", title="[bold]Open Positions[/bold]")
        return Panel(table, title=f"[bold]Open Positions ({len(positions)})[/bold]")

    def _system_panel(self, state, halt_reason, anomaly_hold) -> Panel:
        state_colors = {
            "trading": "green",
            "paused": "yellow",
            "halted": "red bold",
            "shadow_mode": "cyan",
            "initializing": "blue",
            "pre_session": "blue",
            "post_session": "magenta",
        }
        color = state_colors.get(state.value if hasattr(state, 'value') else str(state), "white")

        text = Text()
        text.append("  State: ", style="bold")
        text.append(f"{state.value.upper()}\n" if hasattr(state, 'value') else f"{state}\n", style=color)
        if halt_reason:
            text.append(f"  Halt: {halt_reason[:50]}\n", style="red")
        if anomaly_hold:
            text.append("  ⚠ ANOMALY HOLD ACTIVE\n", style="yellow bold")

        return Panel(text, title="[bold]System State[/bold]", border_style=color)

    def _agents_panel(self, heartbeats) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
        table.add_column("Agent", style="bold", width=28)
        table.add_column("State", width=12)
        table.add_column("Errors", justify="right", width=6)
        table.add_column("Last Seen", width=10)

        state_styles = {
            "idle": "green",
            "processing": "cyan",
            "blocked": "yellow",
            "error": "red bold",
            "shutdown": "dim",
        }

        for hb in sorted(heartbeats, key=lambda h: h.get("agent_name", "")):
            state = hb.get("state", "unknown")
            errors = int(hb.get("error_count", 0))
            last = hb.get("last_seen", "")
            if last:
                try:
                    dt = datetime.fromisoformat(last)
                    elapsed = (datetime.utcnow() - dt).total_seconds()
                    last_str = f"{elapsed:.0f}s ago"
                    if elapsed > 5:
                        last_str = Text(last_str, style="red")
                    else:
                        last_str = Text(last_str, style="green")
                except Exception:
                    last_str = last[:10]
            else:
                last_str = "N/A"

            table.add_row(
                hb.get("agent_name", "")[:28],
                Text(state, style=state_styles.get(state, "white")),
                Text(str(errors), style="red" if errors > 0 else "green"),
                last_str,
            )

        return Panel(table, title="[bold]Agent Health[/bold]")


async def main(refresh_sec: float = 1.0) -> None:
    dashboard = TradingDashboard(refresh_sec=refresh_sec)
    await dashboard.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trading system monitor")
    parser.add_argument("--refresh", type=float, default=1.0, help="Refresh interval in seconds")
    args = parser.parse_args()

    asyncio.run(main(refresh_sec=args.refresh))
