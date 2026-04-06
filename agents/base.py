"""
BaseAgent — shared foundation for all 17 trading agents.

Every agent:
  1. Has a unique ID, name, and state machine.
  2. Publishes heartbeats at a configured interval.
  3. Subscribes to one or more message bus topics.
  4. Has access to: message bus, state store, audit log, system config.
  5. Has a Claude LLM client for reasoning tasks.
  6. Enforces its safety contract via abstract `process()`.

Agents must never block the event loop. All I/O is async.
"""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import anthropic
import yaml

from core.enums import AgentState, Topic
from core.models import AgentHeartbeat, BusMessage, AuditEvent
from infrastructure.message_bus import MessageBus
from infrastructure.state_store import StateStore
from infrastructure.audit_log import AuditLog

logger = logging.getLogger(__name__)

# Load config once at module level
_config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_config_path) as f:
    _CONFIG = yaml.safe_load(f)


class BaseAgent(ABC):
    """
    Abstract base class for all 17 cooperative trading agents.

    Subclasses must implement:
        - `subscribed_topics` property: list of Topics this agent listens to
        - `process(message)`: handle one incoming message
        - `on_startup()`: initialization (optional)
        - `on_shutdown()`: cleanup (optional)
    """

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        bus: MessageBus,
        store: StateStore,
        audit: AuditLog,
        config: dict | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.bus = bus
        self.store = store
        self.audit = audit
        self.config = config or _CONFIG
        self.state = AgentState.IDLE
        self._error_count = 0
        self._heartbeat_task: asyncio.Task | None = None
        self._consumer_tasks: list[asyncio.Task] = []
        self._running = False

        # LLM client (lazy — only agents that need it will call llm_call())
        self._llm_client: anthropic.AsyncAnthropic | None = None

    @property
    @abstractmethod
    def subscribed_topics(self) -> list[Topic]:
        """Return the list of message bus topics this agent subscribes to."""

    @abstractmethod
    async def process(self, message: BusMessage) -> None:
        """
        Handle one incoming message. This is the agent's core logic.
        Must enforce all safety contracts defined in the business case.
        Must not raise — catch and log all errors internally.
        """

    async def on_startup(self) -> None:
        """Override for agent-specific initialization."""

    async def on_shutdown(self) -> None:
        """Override for agent-specific cleanup."""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self.state = AgentState.IDLE
        logger.info("[%s] Starting", self.agent_name)

        await self.on_startup()

        # Subscribe to topics
        for topic in self.subscribed_topics:
            self.bus.subscribe(topic, self._safe_process, consumer_name=self.agent_id)

        # Start consumer loops
        await self.bus.start_consuming(consumer_name=self.agent_id)

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"{self.agent_id}-heartbeat"
        )
        logger.info("[%s] Ready", self.agent_name)

    async def stop(self) -> None:
        self._running = False
        self.state = AgentState.SHUTDOWN
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        await self.on_shutdown()
        logger.info("[%s] Stopped", self.agent_name)

    # ── Safe message processing ────────────────────────────────────────────────

    async def _safe_process(self, message: BusMessage) -> None:
        """Wraps process() with error handling and state tracking."""
        self.state = AgentState.PROCESSING
        try:
            await self.process(message)
        except Exception as exc:
            self._error_count += 1
            self.state = AgentState.ERROR
            logger.exception(
                "[%s] Unhandled error processing topic '%s': %s",
                self.agent_name, message.topic, exc,
            )
            await self.audit.record(AuditEvent(
                event_type="AGENT_ERROR",
                agent=self.agent_id,
                details={
                    "topic": message.topic.value,
                    "error": str(exc),
                    "error_count": self._error_count,
                },
            ))
        finally:
            if self.state == AgentState.PROCESSING:
                self.state = AgentState.IDLE

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        interval = self.config.get("system", {}).get("heartbeat_interval_ms", 500) / 1000
        while self._running:
            try:
                hb = AgentHeartbeat(
                    agent_id=self.agent_id,
                    agent_name=self.agent_name,
                    state=self.state,
                    error_count=self._error_count,
                )
                await self.bus.publish_raw(
                    topic=Topic.AGENT_HEARTBEAT,
                    source_agent=self.agent_id,
                    payload=hb.model_dump(mode="json"),
                )
                await self.store.save_heartbeat(
                    self.agent_id, self.agent_name, self.state.value, self._error_count
                )
            except Exception as exc:
                logger.warning("[%s] Heartbeat error: %s", self.agent_name, exc)
            await asyncio.sleep(interval)

    # ── Publishing helpers ─────────────────────────────────────────────────────

    async def publish(
        self,
        topic: Topic,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> None:
        await self.bus.publish_raw(
            topic=topic,
            source_agent=self.agent_id,
            payload=payload,
            correlation_id=correlation_id,
        )

    # ── LLM helpers ───────────────────────────────────────────────────────────

    def _get_llm(self) -> anthropic.AsyncAnthropic:
        if self._llm_client is None:
            self._llm_client = anthropic.AsyncAnthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY", "")
            )
        return self._llm_client

    async def llm_call(
        self,
        system_prompt: str,
        user_message: str,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """
        Make a Claude API call. Returns the text content of the response.
        Uses config defaults unless overridden.
        """
        llm_cfg = self.config.get("llm", {})
        client = self._get_llm()
        response = await client.messages.create(
            model=model or llm_cfg.get("model", "claude-sonnet-4-6"),
            max_tokens=max_tokens or llm_cfg.get("max_tokens", 4096),
            temperature=temperature or llm_cfg.get("temperature", 0.1),
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text if response.content else ""

    async def llm_call_json(
        self,
        system_prompt: str,
        user_message: str,
        **kwargs,
    ) -> dict:
        """LLM call that expects a JSON response. Parses and returns as dict."""
        import json
        text = await self.llm_call(
            system_prompt=system_prompt + "\n\nRespond with valid JSON only. No markdown.",
            user_message=user_message,
            **kwargs,
        )
        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

    # ── Config helpers ─────────────────────────────────────────────────────────

    def cfg(self, *keys: str, default: Any = None) -> Any:
        """Navigate the config dict with dot-path keys."""
        val = self.config
        for k in keys:
            if not isinstance(val, dict):
                return default
            val = val.get(k, default)
        return val
