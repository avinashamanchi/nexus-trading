"""
Pipeline Coordinator — boots all 17 agents and manages the system lifecycle.

Architecture:
  - Instantiates all agents with shared infrastructure (bus, store, audit)
  - Wires callable dependencies between agents (e.g. account_equity_fn)
  - Drives the session state machine
  - Runs the bus watchdog as an independent task
  - Handles graceful shutdown

The coordinator itself does NOT process trade signals — it only orchestrates.
All intelligence lives in the 17 specialized agents.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from brokers.alpaca import AlpacaBroker
from core.enums import SystemState, Topic
from data.base import DataFeedBase
from data.feed import AlpacaDataFeed
from data.level2 import MockOrderBookFeed, OrderBookCache
from infrastructure.audit_log import AuditLog
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.watchdog import BusWatchdog
from pipeline.state_machine import TradingSessionStateMachine

logger = logging.getLogger(__name__)


class TradingSystemCoordinator:
    """
    Boots and coordinates all 17 trading agents.

    Usage:
        coordinator = TradingSystemCoordinator()
        await coordinator.initialize()
        await coordinator.run()  # blocks until shutdown
    """

    def __init__(self, config_path: str = "config.yaml") -> None:
        load_dotenv()
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self._mode = os.environ.get("TRADING_MODE", self.config.get("system", {}).get("mode", "shadow"))
        self._agents: list = []
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Infrastructure (initialized in initialize())
        self.bus: MessageBus | None = None
        self.store: StateStore | None = None
        self.audit: AuditLog | None = None
        self.broker: AlpacaBroker | None = None
        self.data_feed: DataFeedBase | None = None
        self._l2_manager = None
        self._deferred_agents: list = []
        self.order_book: OrderBookCache | None = None
        self.state_machine: TradingSessionStateMachine | None = None
        self.watchdog: BusWatchdog | None = None

        # Agent references (set after instantiation)
        self.psa = None
        self.hgl = None
        self.psra = None

        # In-memory open position symbol tracker (sync-accessible by MiSA)
        self._open_position_symbols: set[str] = set()

    def _build_data_feed(self) -> DataFeedBase:
        """
        Instantiate the configured data feed provider.
        Controlled by config.yaml: data_feed.provider (polygon | alpaca).
        """
        provider = self.config.get("data_feed", {}).get("provider", "alpaca")
        if provider == "polygon":
            from data.polygon_feed import PolygonDataFeed
            from data.polygon_l2 import PolygonL2Manager

            api_key_env = self.config.get("polygon", {}).get("api_key_env", "POLYGON_API_KEY")
            api_key = os.environ[api_key_env]
            use_delayed = self.config.get("data_feed", {}).get("use_delayed", False)
            reconnect_delay = self.config.get("polygon", {}).get("reconnect_delay_sec", 5.0)
            max_reconnects = self.config.get("polygon", {}).get("max_reconnect_attempts", 10)

            feed = PolygonDataFeed(
                api_key=api_key,
                use_delayed=use_delayed,
                reconnect_delay_sec=reconnect_delay,
                max_reconnect_attempts=max_reconnects,
            )

            poll_sec = self.config.get("polygon", {}).get("l2_snapshot_poll_sec", 2)
            l2_manager = PolygonL2Manager(
                api_key=api_key,
                order_book=self.order_book,
                poll_sec=poll_sec,
            )
            self._l2_manager = l2_manager
            return feed
        else:
            paper = self._mode != "live"
            return AlpacaDataFeed(
                api_key=os.environ.get("ALPACA_API_KEY", ""),
                secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
                paper=paper,
            )

    async def initialize(self) -> None:
        """Initialize all infrastructure and agents."""
        logger.info("=== Autonomous Cooperative-AI Trading System ===")
        logger.info("Mode: %s", self._mode.upper())

        # ── Infrastructure ────────────────────────────────────────────────────
        self.bus = MessageBus(
            redis_url=os.environ.get("REDIS_URL"),
            consumer_group=self.config.get("message_bus", {}).get("consumer_group", "trading_system"),
            max_len=self.config.get("message_bus", {}).get("max_len", 10_000),
        )
        await self.bus.connect()

        self.store = StateStore()
        await self.store.initialize()

        self.audit = AuditLog()
        await self.audit.initialize()

        # ── Broker ────────────────────────────────────────────────────────────
        paper = self._mode != "live"
        self.broker = AlpacaBroker(
            api_key=os.environ.get("ALPACA_API_KEY", ""),
            secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
            paper=paper,
        )
        await self.broker.connect()

        # ── Data Feed ─────────────────────────────────────────────────────────
        self.order_book = OrderBookCache()
        self.data_feed = self._build_data_feed()

        provider = self.config.get("data_feed", {}).get("provider", "alpaca")
        if provider == "polygon" and self._l2_manager:
            # Wire Q.* NBBO events from PolygonDataFeed → PolygonL2Manager
            l2 = self._l2_manager
            async def _nbbo_handler(tick) -> None:
                await l2.update_from_nbbo(tick.symbol, tick.bid, 0, tick.ask, 0, tick.timestamp)
            self.data_feed.add_tick_handler(_nbbo_handler)
        else:
            # Alpaca paper: synthetic L2 from L1 ticks (unchanged)
            mock_l2 = MockOrderBookFeed(self.order_book)
            self.data_feed.add_tick_handler(
                lambda tick: mock_l2.update_from_tick(tick.symbol, tick.bid, tick.ask)
            )

        # ── State Machine ─────────────────────────────────────────────────────
        self.state_machine = TradingSessionStateMachine()
        self.state_machine.register_callback(self._on_state_transition)

        # ── Agents ────────────────────────────────────────────────────────────
        await self._instantiate_agents()

        # ── Watchdog ──────────────────────────────────────────────────────────
        self.watchdog = BusWatchdog(
            bus_health_fn=self.bus.health_check,
            on_bus_failure=self._on_bus_failure,
            on_bus_recovery=self._on_bus_recovery,
        )

        logger.info("Initialization complete")

    async def _instantiate_agents(self) -> None:
        """Instantiate all 17 agents with dependency injection."""
        base_kwargs = dict(bus=self.bus, store=self.store, audit=self.audit, config=self.config)

        # Deferred imports to avoid circular deps at module level
        from agents.agent_17_global_clock import GlobalClockAgent
        from agents.agent_00_edge_research import EdgeResearchAgent
        from agents.agent_01_market_universe import MarketUniverseAgent
        from agents.agent_02_market_regime import MarketRegimeAgent
        from agents.agent_03_data_integrity import DataIntegrityAgent
        from agents.agent_04_micro_signal import MicroSignalAgent
        from agents.agent_05_signal_validation import SignalValidationAgent
        from agents.agent_06_tera import TERAAgent
        from agents.agent_07_spa import SPAAgent
        from agents.agent_08_execution import ExecutionAgent
        from agents.agent_09_broker_reconciliation import BrokerReconciliationAgent
        from agents.agent_10_execution_quality import ExecutionQualityAgent
        from agents.agent_11_micro_monitoring import MicroMonitoringAgent
        from agents.agent_12_exit_lockin import ExitLockInAgent
        from agents.agent_13_portfolio_supervisor import PortfolioSupervisorAgent
        from agents.agent_14_post_session_review import PostSessionReviewAgent
        from agents.agent_15_tax_compliance import TaxComplianceAgent
        from agents.agent_16_human_governance import HumanGovernanceAgent

        # Callables shared between agents
        async def get_account_equity() -> float:
            acct = await self.broker.get_account()
            return acct.equity

        # Agent 17 — Global Clock (instantiated first, started first)
        gca = GlobalClockAgent(bus=self.bus, store=self.store, audit=self.audit)

        # Agent 0 — Edge Research
        era = EdgeResearchAgent(
            agent_id="edge_research", agent_name="EdgeResearchAgent", **base_kwargs
        )

        # Agent 1 — Market Universe
        mua = MarketUniverseAgent(**base_kwargs)

        # Agent 2 — Market Regime
        mra = MarketRegimeAgent(**base_kwargs)

        # Agent 3 — Data Integrity
        dia = DataIntegrityAgent(
            data_feed=self.data_feed, order_book=self.order_book, **base_kwargs,
        )

        # Agent 4 — Micro-Signal (MiSA)
        misa = MicroSignalAgent(
            agent_id="micro_signal", agent_name="MicroSignalAgent",
            data_feed=self.data_feed,
            order_book=self.order_book,
            open_positions_fn=lambda: list(self._open_position_symbols),
            **base_kwargs,
        )

        # Agent 5 — Signal Validation
        sva = SignalValidationAgent(
            agent_id="signal_validation", agent_name="SignalValidationAgent",
            data_feed=self.data_feed,
            current_regime_fn=lambda: mra.current_regime,
            current_universe_fn=lambda: mua.current_universe,
            **base_kwargs,
        )

        # Agent 6 — TERA
        tera = TERAAgent(
            agent_id="tera", agent_name="TERAAgent",
            account_equity_fn=get_account_equity,
            get_wash_sale_fn=self.store.get_wash_sale_flag,
            open_positions_fn=self.store.load_open_positions,
            **base_kwargs,
        )

        # Agent 7 — SPA
        spa = SPAAgent(
            agent_id="spa", agent_name="SPAAgent",
            account_equity_fn=get_account_equity,
            approved_setups_fn=lambda: era.approved_setup_list,
            current_execution_quality_fn=lambda: eq_agent.session_summary,
            **base_kwargs,
        )

        # Agent 8 — Execution
        ea = ExecutionAgent(
            agent_id="execution", agent_name="ExecutionAgent",
            broker=self.broker, data_feed=self.data_feed, **base_kwargs,
        )

        # Agent 9 — Broker Reconciliation
        bra = BrokerReconciliationAgent(
            agent_id="broker_reconciliation", agent_name="BrokerReconciliationAgent",
            broker=self.broker, **base_kwargs,
        )

        # Agent 10 — Execution Quality
        eq_agent = ExecutionQualityAgent(
            agent_id="execution_quality", agent_name="ExecutionQualityAgent",
            **base_kwargs,
        )

        # Agent 11 — Micro Monitoring
        mma = MicroMonitoringAgent(
            agent_id="micro_monitoring", agent_name="MicroMonitoringAgent",
            data_feed=self.data_feed, **base_kwargs,
        )

        # Agent 12 — Exit & Lock-In
        ela = ExitLockInAgent(
            agent_id="exit_lockin", agent_name="ExitLockInAgent",
            broker=self.broker, **base_kwargs,
        )

        # Agent 13 — Portfolio Supervisor
        psa = PortfolioSupervisorAgent(
            agent_id="portfolio_supervisor", agent_name="PortfolioSupervisorAgent",
            account_equity_fn=get_account_equity, **base_kwargs,
        )

        # Agent 14 — Post-Session Review
        psra = PostSessionReviewAgent(
            agent_id="post_session_review", agent_name="PostSessionReviewAgent",
            **base_kwargs,
        )

        # Agent 15 — Tax & Compliance
        tca = TaxComplianceAgent(
            agent_id="tax_compliance", agent_name="TaxComplianceAgent",
            **base_kwargs,
        )

        # Agent 16 — Human Governance
        hgl = HumanGovernanceAgent(
            agent_id="human_governance", agent_name="HumanGovernanceAgent",
            **base_kwargs,
        )

        # MVP active agents (17→1→2→3→4→5→8→9→11→12→13)
        self._agents = [gca, mua, mra, dia, misa, sva, ea, bra, mma, ela, psa]

        # Deferred agents — instantiated but not started in MVP
        # Reactivating any: move from this list to self._agents
        self._deferred_agents = [era, tera, spa, eq_agent, psra, tca, hgl]

        deferred_names = [type(a).__name__ for a in self._deferred_agents]
        logger.info("Deferred agents (not started): %s", deferred_names)

        # Keep named references for direct access
        self.psa = psa
        self.hgl = hgl
        self.psra = psra

    async def run(self) -> None:
        """Start all agents and run until shutdown."""
        self._running = True

        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())

        # Subscribe coordinator to FILL_EVENT and TRADE_CLOSED so we can maintain
        # the open position symbol set that MiSA uses synchronously
        self.bus.subscribe(Topic.FILL_EVENT, self._on_fill_event)
        self.bus.subscribe(Topic.TRADE_CLOSED, self._on_trade_closed)

        # Start all agents
        agent_tasks = []
        for agent in self._agents:
            task = asyncio.create_task(agent.start(), name=agent.agent_name)
            agent_tasks.append(task)

        # Start state machine
        sm_task = asyncio.create_task(
            self.state_machine.run(), name="state-machine"
        )

        # Start data feed
        feed_task = asyncio.create_task(
            self.data_feed.run(), name="data-feed"
        )

        # Start watchdog
        watchdog_task = asyncio.create_task(
            self.watchdog.run(), name="bus-watchdog"
        )

        # Start L2 manager background poll (Polygon only — None for Alpaca)
        if self._l2_manager:
            l2_task = asyncio.create_task(
                self._l2_manager.run(lambda: list(self.data_feed.subscribed_symbols)),
                name="l2-manager",
            )
            tasks_to_cancel = agent_tasks + [sm_task, feed_task, watchdog_task, l2_task]
        else:
            tasks_to_cancel = agent_tasks + [sm_task, feed_task, watchdog_task]

        logger.info("All agents started — system is LIVE")

        # Wait for shutdown signal
        await self._shutdown_event.wait()
        await self._graceful_shutdown(tasks_to_cancel)

    async def _graceful_shutdown(self, tasks: list) -> None:
        logger.info("Graceful shutdown initiated...")

        # Stop agents first
        for agent in self._agents:
            await agent.stop()

        # Stop data feed
        await self.data_feed.stop()

        # Stop L2 manager (Polygon only)
        if self._l2_manager:
            await self._l2_manager.stop()

        # Cancel remaining tasks
        for task in tasks:
            task.cancel()

        # Close infrastructure
        if self.bus:
            await self.bus.close()
        if self.store:
            await self.store.close()
        if self.audit:
            await self.audit.close()
        if self.broker:
            await self.broker.disconnect()

        logger.info("Shutdown complete")

    # ── State transition handler ───────────────────────────────────────────────

    async def _on_state_transition(
        self, old: SystemState, new: SystemState, reason: str
    ) -> None:
        logger.info("System state: %s → %s (%s)", old.value, new.value, reason)

        if new == SystemState.POST_SESSION:
            # Trigger post-session review
            session_date = datetime.utcnow().strftime("%Y-%m-%d")
            if self.psra:
                asyncio.create_task(self.psra.run_review(session_date))

        elif new == SystemState.HALTED:
            # Emit halt command to all agents
            if self.bus:
                await self.bus.publish_raw(
                    topic=Topic.HALT_COMMAND,
                    source_agent="coordinator",
                    payload={"reason": reason, "state": new.value},
                )

    # ── Position tracking (keeps MiSA's open_positions_fn current) ───────────

    async def _on_fill_event(self, message: "BusMessage") -> None:
        symbol = message.payload.get("symbol")
        if symbol and not message.payload.get("is_shadow"):
            self._open_position_symbols.add(symbol)

    async def _on_trade_closed(self, message: "BusMessage") -> None:
        symbol = message.payload.get("symbol")
        if symbol:
            self._open_position_symbols.discard(symbol)

    # ── Bus failure handlers ───────────────────────────────────────────────────

    async def _on_bus_failure(self) -> None:
        logger.critical("Bus failure — entering safe mode via PSA")
        if self.psa:
            await self.psa._halt("Message bus failure — safe mode", "bus_failure")

    async def _on_bus_recovery(self) -> None:
        logger.warning(
            "Bus recovered — system remains halted until HGL sign-off"
        )

    # ── Kill switch (direct broker access, independent of bus) ────────────────

    async def emergency_flatten(self, approved_by: str) -> None:
        """
        Emergency kill switch — direct broker access, bypasses message bus.
        Per §7.1 and §8.3.
        """
        logger.critical("KILL SWITCH ACTIVATED by %s", approved_by)
        try:
            await self.broker.cancel_all_orders()
            orders = await self.broker.close_all_positions()
            logger.warning("Kill switch: %d positions closed", len(orders))
        except Exception as exc:
            logger.exception("Kill switch broker error: %s", exc)

        from infrastructure.audit_log import system_halt_event
        await self.audit.record(
            system_halt_event("coordinator", f"Kill switch activated by {approved_by}")
        )
        self._shutdown_event.set()
