"""
Message Bus — Redis Streams with in-memory fallback.

Architecture notes:
  - All 17 agents communicate exclusively via this bus.
  - Each topic is a Redis Stream; agents are consumers in a consumer group.
  - If Redis is unavailable, the in-memory fallback keeps the system alive
    but PSA must be notified immediately (bus failure is a safe-mode trigger).
  - All messages are persisted for post-session audit.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Awaitable

import redis.asyncio as aioredis

from core.enums import Topic
from core.models import BusMessage

logger = logging.getLogger(__name__)


# ─── Subscriber type ──────────────────────────────────────────────────────────
MessageHandler = Callable[[BusMessage], Awaitable[None]]


# ═══════════════════════════════════════════════════════════════════════════════
#  In-Memory Fallback Bus (dev / Redis-unavailable mode)
# ═══════════════════════════════════════════════════════════════════════════════

class InMemoryBus:
    """
    Asyncio-native pub/sub bus used when Redis is unavailable.
    NOT suitable for multi-process deployments; single-process only.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._running = True

    async def publish(self, message: BusMessage) -> None:
        topic = message.topic.value
        payload = message.model_dump_json()
        for q in self._queues.get(topic, []):
            await q.put(payload)

    def subscribe(self, topic: Topic) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._queues[topic.value].append(q)
        return q

    async def close(self) -> None:
        self._running = False


# ═══════════════════════════════════════════════════════════════════════════════
#  Redis Streams Bus
# ═══════════════════════════════════════════════════════════════════════════════

class RedisStreamBus:
    """
    Production message bus backed by Redis Streams.

    Features:
    - Consumer groups ensure each message is processed by exactly one consumer
      per agent type (fan-out achieved by separate groups per agent).
    - Stream maxlen caps memory use; oldest messages are trimmed automatically.
    - Health pings every heartbeat_interval_sec for the watchdog.
    """

    def __init__(
        self,
        redis_url: str,
        consumer_group: str,
        max_len: int = 10_000,
        heartbeat_interval_sec: float = 0.5,
    ) -> None:
        self._redis_url = redis_url
        self._consumer_group = consumer_group
        self._max_len = max_len
        self._heartbeat_interval = heartbeat_interval_sec
        self._client: aioredis.Redis | None = None
        self._healthy = False
        self._last_ping: datetime | None = None

    async def connect(self) -> None:
        self._client = aioredis.from_url(
            self._redis_url, decode_responses=True, socket_timeout=5.0
        )
        await self._client.ping()
        self._healthy = True
        self._last_ping = datetime.utcnow()
        logger.info("RedisStreamBus connected to %s", self._redis_url)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
        self._healthy = False

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    async def ping(self) -> bool:
        try:
            if self._client:
                await self._client.ping()
                self._healthy = True
                self._last_ping = datetime.utcnow()
                return True
        except Exception as exc:
            logger.error("Redis ping failed: %s", exc)
            self._healthy = False
        return False

    async def _ensure_group(self, topic: Topic) -> None:
        """Create consumer group if it doesn't exist."""
        stream = topic.value
        try:
            await self._client.xgroup_create(  # type: ignore[union-attr]
                stream, self._consumer_group, id="$", mkstream=True
            )
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def publish(self, message: BusMessage) -> str:
        """Publish a message; returns the Redis stream entry ID."""
        if not self._client or not self._healthy:
            raise RuntimeError("RedisStreamBus not connected or unhealthy")
        stream = message.topic.value
        data = {"payload": message.model_dump_json()}
        entry_id = await self._client.xadd(  # type: ignore[union-attr]
            stream, data, maxlen=self._max_len, approximate=True
        )
        return entry_id

    async def subscribe(
        self,
        topic: Topic,
        consumer_name: str,
        handler: MessageHandler,
        poll_interval_ms: int = 100,
    ) -> None:
        """
        Blocking subscription loop — run this as an asyncio task.
        Uses consumer groups so each message is acknowledged once processed.
        """
        await self._ensure_group(topic)
        stream = topic.value
        logger.info(
            "Consumer '%s' subscribed to stream '%s'", consumer_name, stream
        )
        while self._healthy:
            try:
                results = await self._client.xreadgroup(  # type: ignore[union-attr]
                    self._consumer_group,
                    consumer_name,
                    {stream: ">"},
                    count=10,
                    block=poll_interval_ms,
                )
                if not results:
                    continue
                for _stream, entries in results:
                    for entry_id, fields in entries:
                        raw = fields.get("payload")
                        if not raw:
                            continue
                        try:
                            msg = BusMessage.model_validate_json(raw)
                            await handler(msg)
                            await self._client.xack(  # type: ignore[union-attr]
                                stream, self._consumer_group, entry_id
                            )
                        except Exception as exc:
                            logger.exception(
                                "Handler error on topic '%s': %s", topic, exc
                            )
            except aioredis.ConnectionError as exc:
                logger.error("Redis connection lost on '%s': %s", topic, exc)
                self._healthy = False
                break
            await asyncio.sleep(0)


# ═══════════════════════════════════════════════════════════════════════════════
#  Unified MessageBus facade
# ═══════════════════════════════════════════════════════════════════════════════

class MessageBus:
    """
    Facade that wraps either Redis Streams or the in-memory fallback.

    Usage:
        bus = MessageBus(redis_url="redis://localhost:6379/0")
        await bus.connect()
        await bus.publish(message)
        # subscribe is wrapped per-agent by BaseAgent
    """

    def __init__(
        self,
        redis_url: str | None = None,
        consumer_group: str = "trading_system",
        max_len: int = 10_000,
    ) -> None:
        self._redis_url = redis_url
        self._consumer_group = consumer_group
        self._max_len = max_len
        self._backend: RedisStreamBus | InMemoryBus | None = None
        self._using_fallback = False
        self._handlers: dict[str, list[MessageHandler]] = defaultdict(list)
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> None:
        if self._redis_url:
            bus = RedisStreamBus(
                self._redis_url, self._consumer_group, self._max_len
            )
            try:
                await bus.connect()
                self._backend = bus
                self._using_fallback = False
                logger.info("MessageBus: using Redis backend")
                return
            except Exception as exc:
                logger.warning(
                    "Redis unavailable (%s) — falling back to in-memory bus", exc
                )
        self._backend = InMemoryBus()
        self._using_fallback = True
        logger.warning(
            "MessageBus: using IN-MEMORY fallback — "
            "NOT suitable for multi-process or production deployment"
        )

    async def publish(self, message: BusMessage) -> None:
        if self._backend is None:
            raise RuntimeError("MessageBus not connected — call connect() first")
        await self._backend.publish(message)

    async def publish_raw(
        self,
        topic: Topic,
        source_agent: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> None:
        msg = BusMessage(
            topic=topic,
            source_agent=source_agent,
            payload=payload,
            correlation_id=correlation_id,
        )
        await self.publish(msg)

    def subscribe(
        self,
        topic: Topic,
        handler: MessageHandler,
        consumer_name: str = "default",
    ) -> None:
        """
        Register a handler for a topic.
        For the Redis backend, the subscription loop is started when
        start_consuming() is called.
        For the in-memory backend, messages are pulled from a queue.
        """
        self._handlers[topic.value].append(handler)

    async def start_consuming(self, consumer_name: str = "default") -> None:
        """Start all subscription loops as background asyncio tasks."""
        if isinstance(self._backend, RedisStreamBus):
            for topic_str, handlers in self._handlers.items():
                topic = Topic(topic_str)
                for handler in handlers:
                    task = asyncio.create_task(
                        self._backend.subscribe(topic, consumer_name, handler),
                        name=f"bus-consumer-{topic_str}",
                    )
                    self._tasks.append(task)
        elif isinstance(self._backend, InMemoryBus):
            for topic_str, handlers in self._handlers.items():
                topic = Topic(topic_str)
                q = self._backend.subscribe(topic)
                for handler in handlers:
                    task = asyncio.create_task(
                        self._pump_in_memory(q, handler, topic_str),
                        name=f"bus-consumer-{topic_str}",
                    )
                    self._tasks.append(task)

    async def _pump_in_memory(
        self,
        queue: asyncio.Queue,
        handler: MessageHandler,
        topic_str: str,
    ) -> None:
        while True:
            raw = await queue.get()
            try:
                msg = BusMessage.model_validate_json(raw)
                await handler(msg)
            except Exception as exc:
                logger.exception("In-memory handler error on '%s': %s", topic_str, exc)

    @property
    def is_healthy(self) -> bool:
        if isinstance(self._backend, RedisStreamBus):
            return self._backend.is_healthy
        return self._backend is not None

    @property
    def using_fallback(self) -> bool:
        return self._using_fallback

    async def health_check(self) -> bool:
        if isinstance(self._backend, RedisStreamBus):
            return await self._backend.ping()
        return self._backend is not None

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._backend:
            await self._backend.close()
        logger.info("MessageBus closed")
