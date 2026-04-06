"""
Bus Watchdog — independent process that monitors message bus health.

Per §7.1: The watchdog runs independently of all trading agents.
A bus failure is detected within one heartbeat interval and PSA is notified
via direct broker API access (not via the bus itself).

The watchdog is the ONLY component allowed to contact the broker directly
without going through the Execution Agent — solely for emergency position
flattening when the bus is down.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

OnBusFailureCallback = Callable[[], Awaitable[None]]
OnBusRecoveryCallback = Callable[[], Awaitable[None]]


class BusWatchdog:
    """
    Monitors message bus health and agent heartbeats.

    - Pings the bus every `check_interval_sec` seconds.
    - If the bus doesn't respond within `heartbeat_interval_sec`, calls
      `on_failure` (typically: PSA enters safe mode).
    - When the bus recovers, calls `on_recovery` (but system restart
      still requires HGL sign-off per §7.1).
    - Monitors per-agent heartbeats; alerts if any agent goes silent.
    """

    def __init__(
        self,
        bus_health_fn: Callable[[], Awaitable[bool]],
        check_interval_sec: float = 0.5,
        agent_timeout_sec: float = 5.0,
        on_bus_failure: OnBusFailureCallback | None = None,
        on_bus_recovery: OnBusRecoveryCallback | None = None,
        on_agent_timeout: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._bus_health_fn = bus_health_fn
        self._check_interval = check_interval_sec
        self._agent_timeout = agent_timeout_sec
        self._on_bus_failure = on_bus_failure
        self._on_bus_recovery = on_bus_recovery
        self._on_agent_timeout = on_agent_timeout

        self._running = False
        self._bus_was_healthy = True
        self._agent_last_seen: dict[str, datetime] = {}
        self._alerted_agents: set[str] = set()

    def update_agent_heartbeat(self, agent_id: str) -> None:
        """Called by agents to register a heartbeat."""
        self._agent_last_seen[agent_id] = datetime.utcnow()
        self._alerted_agents.discard(agent_id)

    async def run(self) -> None:
        """Main watchdog loop — run as a separate asyncio task."""
        self._running = True
        logger.info("BusWatchdog started (check_interval=%.1fs)", self._check_interval)

        while self._running:
            await self._check_bus_health()
            await self._check_agent_heartbeats()
            await asyncio.sleep(self._check_interval)

    async def _check_bus_health(self) -> None:
        try:
            healthy = await asyncio.wait_for(
                self._bus_health_fn(), timeout=self._check_interval
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error("Bus health check failed: %s", exc)
            healthy = False

        if not healthy and self._bus_was_healthy:
            logger.critical(
                "MESSAGE BUS FAILURE DETECTED — entering safe mode"
            )
            self._bus_was_healthy = False
            if self._on_bus_failure:
                try:
                    await self._on_bus_failure()
                except Exception as exc:
                    logger.error("on_bus_failure callback error: %s", exc)

        elif healthy and not self._bus_was_healthy:
            logger.warning(
                "Message bus recovered — awaiting HGL sign-off before restart"
            )
            self._bus_was_healthy = True
            if self._on_bus_recovery:
                try:
                    await self._on_bus_recovery()
                except Exception as exc:
                    logger.error("on_bus_recovery callback error: %s", exc)

    async def _check_agent_heartbeats(self) -> None:
        now = datetime.utcnow()
        timeout = timedelta(seconds=self._agent_timeout)

        for agent_id, last_seen in self._agent_last_seen.items():
            if (now - last_seen) > timeout and agent_id not in self._alerted_agents:
                self._alerted_agents.add(agent_id)
                logger.error(
                    "Agent '%s' has not sent a heartbeat in %.1fs — possible crash",
                    agent_id,
                    (now - last_seen).total_seconds(),
                )
                if self._on_agent_timeout:
                    try:
                        await self._on_agent_timeout(agent_id)
                    except Exception as exc:
                        logger.error("on_agent_timeout callback error: %s", exc)

    def stop(self) -> None:
        self._running = False
        logger.info("BusWatchdog stopped")

    @property
    def bus_healthy(self) -> bool:
        return self._bus_was_healthy

    def agent_status_summary(self) -> dict[str, str]:
        now = datetime.utcnow()
        summary = {}
        for agent_id, last_seen in self._agent_last_seen.items():
            elapsed = (now - last_seen).total_seconds()
            if elapsed < self._agent_timeout:
                summary[agent_id] = f"ok ({elapsed:.1f}s ago)"
            else:
                summary[agent_id] = f"TIMEOUT ({elapsed:.1f}s ago)"
        return summary


async def run_watchdog_process(
    bus_health_fn: Callable[[], Awaitable[bool]],
    on_bus_failure: OnBusFailureCallback | None = None,
    on_bus_recovery: OnBusRecoveryCallback | None = None,
) -> None:
    """
    Entry point for running the watchdog as a standalone coroutine.
    Handles SIGTERM/SIGINT gracefully.
    """
    watchdog = BusWatchdog(
        bus_health_fn=bus_health_fn,
        on_bus_failure=on_bus_failure,
        on_bus_recovery=on_bus_recovery,
    )

    loop = asyncio.get_running_loop()

    def _shutdown(sig_name: str) -> None:
        logger.warning("Watchdog received %s — shutting down", sig_name)
        watchdog.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))

    await watchdog.run()
