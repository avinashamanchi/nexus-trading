"""
§8.3 Kill Switch API.

REST endpoint contract and bus-independent emergency flatten mechanism.

SLOs (§8.3.2):
  - Trigger acknowledgment:   ≤ 250 ms
  - PSA state transition:     ≤ 500 ms after trigger
  - Cancel order dispatch:    ≤ 1 s after trigger
  - Full position flatten:    ≤ 10 s p95

Endpoints (§8.3.1):
  POST /v1/killswitch/trigger           — initiate emergency stop
  GET  /v1/killswitch/status            — current status
  POST /v1/killswitch/retry-flatten     — retry after partial flatten
  POST /v1/killswitch/acknowledge       — operator confirms awareness
  POST /v1/killswitch/restart-request   — request supervised restart

Design constraints:
  - Kill switch is BUS-INDEPENDENT: it calls the broker directly, not via TERA/SPA.
  - All actions are idempotent — duplicate requests are safe.
  - Every trigger, retry, and acknowledgment is written to the audit log.
  - MFA token is validated before any destructive action.
  - The REST server runs as a separate aiohttp application alongside the main system.

Usage:
    ks = KillSwitch(broker, audit, bus, mfa_secret="TOTP_SECRET_BASE32")
    runner = await ks.create_server(host="0.0.0.0", port=8443)
    # ... system runs ...
    await runner.cleanup()
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from enum import Enum
from typing import Any

from core.enums import Topic
from core.models import AuditEvent
from infrastructure.audit_log import AuditLog
from infrastructure.message_bus import MessageBus

logger = logging.getLogger(__name__)


class KillSwitchState(str, Enum):
    ARMED = "armed"           # ready, no trigger
    TRIGGERED = "triggered"   # flatten in progress
    FLATTENED = "flattened"   # all positions closed
    ACKNOWLEDGED = "acknowledged"  # operator confirmed
    RESTARTING = "restarting"  # restart requested


class KillSwitch:
    """
    Bus-independent emergency flatten mechanism with REST API.

    The broker argument must implement `close_all_positions()` and
    `cancel_all_orders()` (BrokerBase interface).
    """

    def __init__(
        self,
        broker: Any,           # BrokerBase
        audit: AuditLog,
        bus: MessageBus,
        mfa_secret: str = "",  # HMAC-SHA256 secret or TOTP base32 secret
    ) -> None:
        self._broker = broker
        self._audit = audit
        self._bus = bus
        self._mfa_secret = mfa_secret.encode() if isinstance(mfa_secret, str) else mfa_secret

        self._state: KillSwitchState = KillSwitchState.ARMED
        self._trigger_id: str | None = None
        self._triggered_at: datetime | None = None
        self._triggered_by: str | None = None
        self._flatten_completed_at: datetime | None = None
        self._acknowledged_by: str | None = None
        self._restart_requested_by: str | None = None

        self._flatten_errors: list[str] = []
        self._positions_closed: int = 0
        self._orders_cancelled: int = 0

        self._lock = asyncio.Lock()

    # ── MFA validation ────────────────────────────────────────────────────────

    def _validate_mfa(self, token: str, initiator: str) -> bool:
        """
        Validate the provided token.

        In production: implement TOTP (RFC 6238) using pyotp.
        Here we implement a simple HMAC-SHA256 time-window token for portability.

        Token format: HMAC-SHA256(secret, f"{initiator}:{floor(epoch/30)}")[:8]
        Valid for current and previous 30-second window.
        """
        if not self._mfa_secret:
            # No MFA configured — only in dev/test environments
            logger.warning("[KS] MFA validation skipped (no secret configured)")
            return True

        now_window = int(time.time() // 30)
        for window in (now_window, now_window - 1):
            message = f"{initiator}:{window}".encode()
            expected = hmac.new(self._mfa_secret, message, hashlib.sha256).hexdigest()[:8]
            if hmac.compare_digest(token, expected):
                return True
        return False

    # ── Core trigger logic ────────────────────────────────────────────────────

    async def trigger(
        self,
        initiator: str,
        mfa_token: str,
        reason: str = "Manual kill switch",
    ) -> dict:
        """
        §8.3.1 POST /v1/killswitch/trigger

        Idempotent — if already triggered, returns current status.
        SLO: respond within 250ms, initiate flatten within 1s.
        """
        t0 = time.monotonic()

        if not self._validate_mfa(mfa_token, initiator):
            return {
                "success": False,
                "error": "MFA validation failed",
                "trigger_id": None,
            }

        async with self._lock:
            if self._state not in (KillSwitchState.ARMED,):
                # Already triggered — idempotent return
                return self._status_dict()

            import uuid
            self._trigger_id = str(uuid.uuid4())
            self._state = KillSwitchState.TRIGGERED
            self._triggered_at = datetime.utcnow()
            self._triggered_by = initiator

        # Publish to bus (non-blocking — broker flatten is bus-independent)
        try:
            await self._bus.publish_raw(
                topic=Topic.KILL_SWITCH,
                source_agent="kill_switch_api",
                payload={
                    "trigger_id": self._trigger_id,
                    "reason": reason,
                    "initiated_by": initiator,
                    "timestamp": self._triggered_at.isoformat(),
                },
            )
        except Exception as exc:
            logger.warning("[KS] Bus publish failed (bus-independent mode): %s", exc)

        await self._audit.record(AuditEvent(
            event_type="KILL_SWITCH_TRIGGERED",
            agent="kill_switch_api",
            details={
                "trigger_id": self._trigger_id,
                "initiator": initiator,
                "reason": reason,
                "ts": self._triggered_at.isoformat(),
            },
        ))

        ack_ms = (time.monotonic() - t0) * 1000
        logger.critical(
            "[KS] TRIGGERED by %s | trigger_id=%s | ack=%.0fms",
            initiator, self._trigger_id, ack_ms,
        )

        # Dispatch flatten as background task (must complete within 10s p95)
        asyncio.create_task(self._execute_flatten())

        return {
            "success": True,
            "trigger_id": self._trigger_id,
            "state": self._state.value,
            "ack_ms": ack_ms,
        }

    async def _execute_flatten(self) -> None:
        """Cancel all orders, then close all positions. Target ≤10s p95."""
        t0 = time.monotonic()
        try:
            logger.critical("[KS] Cancelling all open orders...")
            cancelled = await asyncio.wait_for(
                self._broker.cancel_all_orders(), timeout=3.0
            )
            self._orders_cancelled = cancelled if isinstance(cancelled, int) else 0
            logger.critical("[KS] Orders cancelled: %d", self._orders_cancelled)
        except asyncio.TimeoutError:
            self._flatten_errors.append("cancel_all_orders timed out (3s)")
            logger.error("[KS] cancel_all_orders timed out")
        except Exception as exc:
            self._flatten_errors.append(f"cancel_all_orders error: {exc}")
            logger.error("[KS] cancel_all_orders error: %s", exc)

        try:
            logger.critical("[KS] Flattening all positions...")
            closed = await asyncio.wait_for(
                self._broker.close_all_positions(), timeout=7.0
            )
            self._positions_closed = closed if isinstance(closed, int) else 0
            logger.critical("[KS] Positions closed: %d", self._positions_closed)
        except asyncio.TimeoutError:
            self._flatten_errors.append("close_all_positions timed out (7s)")
            logger.error("[KS] close_all_positions timed out")
        except Exception as exc:
            self._flatten_errors.append(f"close_all_positions error: {exc}")
            logger.error("[KS] close_all_positions error: %s", exc)

        async with self._lock:
            self._flatten_completed_at = datetime.utcnow()
            self._state = KillSwitchState.FLATTENED

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.critical("[KS] Flatten complete in %.0fms", elapsed_ms)

        await self._audit.record(AuditEvent(
            event_type="KILL_SWITCH_FLATTENED",
            agent="kill_switch_api",
            details={
                "trigger_id": self._trigger_id,
                "orders_cancelled": self._orders_cancelled,
                "positions_closed": self._positions_closed,
                "elapsed_ms": elapsed_ms,
                "errors": self._flatten_errors,
            },
        ))

    async def retry_flatten(self, initiator: str, mfa_token: str) -> dict:
        """
        §8.3.1 POST /v1/killswitch/retry-flatten

        Re-attempts position close after a partial flatten.
        Only valid in FLATTENED state with errors, or TRIGGERED state.
        """
        if not self._validate_mfa(mfa_token, initiator):
            return {"success": False, "error": "MFA validation failed"}

        if self._state not in (KillSwitchState.FLATTENED, KillSwitchState.TRIGGERED):
            return {
                "success": False,
                "error": f"retry-flatten not valid in state {self._state.value}",
            }

        self._flatten_errors = []
        asyncio.create_task(self._execute_flatten())

        await self._audit.record(AuditEvent(
            event_type="KILL_SWITCH_RETRY",
            agent="kill_switch_api",
            details={"trigger_id": self._trigger_id, "initiator": initiator},
        ))
        return {"success": True, "state": self._state.value}

    async def acknowledge(self, operator: str, mfa_token: str) -> dict:
        """
        §8.3.1 POST /v1/killswitch/acknowledge

        Operator confirms they are aware of the halt. Records in audit log.
        """
        if not self._validate_mfa(mfa_token, operator):
            return {"success": False, "error": "MFA validation failed"}

        async with self._lock:
            self._acknowledged_by = operator
            if self._state == KillSwitchState.FLATTENED:
                self._state = KillSwitchState.ACKNOWLEDGED

        await self._audit.record(AuditEvent(
            event_type="KILL_SWITCH_ACKNOWLEDGED",
            agent="kill_switch_api",
            details={"trigger_id": self._trigger_id, "operator": operator},
        ))
        logger.warning("[KS] Acknowledged by %s", operator)
        return {"success": True, "acknowledged_by": operator}

    async def restart_request(self, operator: str, mfa_token: str) -> dict:
        """
        §8.3.1 POST /v1/killswitch/restart-request

        Requests supervised restart. Publishes RESUME_COMMAND to bus.
        Only valid after ACKNOWLEDGED.
        """
        if not self._validate_mfa(mfa_token, operator):
            return {"success": False, "error": "MFA validation failed"}

        if self._state != KillSwitchState.ACKNOWLEDGED:
            return {
                "success": False,
                "error": f"restart not valid in state {self._state.value} (must be acknowledged first)",
            }

        async with self._lock:
            self._restart_requested_by = operator
            self._state = KillSwitchState.RESTARTING

        from core.enums import Topic
        await self._bus.publish_raw(
            topic=Topic.RESUME_COMMAND,
            source_agent="kill_switch_api",
            payload={
                "approved_by": operator,
                "trigger_id": self._trigger_id,
                "command": "resume",
                "reason": "kill_switch_restart_request",
            },
        )

        await self._audit.record(AuditEvent(
            event_type="KILL_SWITCH_RESTART_REQUESTED",
            agent="kill_switch_api",
            details={"trigger_id": self._trigger_id, "operator": operator},
        ))
        logger.warning("[KS] Restart requested by %s", operator)
        return {"success": True, "state": self._state.value}

    def status(self) -> dict:
        """§8.3.1 GET /v1/killswitch/status"""
        return self._status_dict()

    def _status_dict(self) -> dict:
        return {
            "state": self._state.value,
            "trigger_id": self._trigger_id,
            "triggered_at": self._triggered_at.isoformat() if self._triggered_at else None,
            "triggered_by": self._triggered_by,
            "flatten_completed_at": (
                self._flatten_completed_at.isoformat()
                if self._flatten_completed_at else None
            ),
            "positions_closed": self._positions_closed,
            "orders_cancelled": self._orders_cancelled,
            "flatten_errors": self._flatten_errors,
            "acknowledged_by": self._acknowledged_by,
            "restart_requested_by": self._restart_requested_by,
        }

    # ── REST server ───────────────────────────────────────────────────────────

    async def create_server(
        self,
        host: str = "127.0.0.1",
        port: int = 8443,
    ):
        """
        Create and start an aiohttp REST server for the kill switch API.

        Returns an aiohttp AppRunner; call `await runner.cleanup()` to stop.

        Endpoints:
          POST /v1/killswitch/trigger
          GET  /v1/killswitch/status
          POST /v1/killswitch/retry-flatten
          POST /v1/killswitch/acknowledge
          POST /v1/killswitch/restart-request
        """
        try:
            from aiohttp import web
        except ImportError:
            logger.warning("[KS] aiohttp not installed — REST server unavailable")
            return None

        ks = self  # capture for closures

        async def _json_body(request) -> dict:
            try:
                return await request.json()
            except Exception:
                return {}

        async def handle_trigger(request):
            body = await _json_body(request)
            result = await ks.trigger(
                initiator=body.get("initiator", "unknown"),
                mfa_token=body.get("mfa_token", ""),
                reason=body.get("reason", "API trigger"),
            )
            status = 200 if result.get("success") else 403
            return web.Response(
                text=json.dumps(result),
                content_type="application/json",
                status=status,
            )

        async def handle_status(request):
            return web.Response(
                text=json.dumps(ks.status()),
                content_type="application/json",
            )

        async def handle_retry(request):
            body = await _json_body(request)
            result = await ks.retry_flatten(
                initiator=body.get("initiator", "unknown"),
                mfa_token=body.get("mfa_token", ""),
            )
            status = 200 if result.get("success") else 403
            return web.Response(
                text=json.dumps(result),
                content_type="application/json",
                status=status,
            )

        async def handle_acknowledge(request):
            body = await _json_body(request)
            result = await ks.acknowledge(
                operator=body.get("operator", "unknown"),
                mfa_token=body.get("mfa_token", ""),
            )
            status = 200 if result.get("success") else 403
            return web.Response(
                text=json.dumps(result),
                content_type="application/json",
                status=status,
            )

        async def handle_restart(request):
            body = await _json_body(request)
            result = await ks.restart_request(
                operator=body.get("operator", "unknown"),
                mfa_token=body.get("mfa_token", ""),
            )
            status = 200 if result.get("success") else 403
            return web.Response(
                text=json.dumps(result),
                content_type="application/json",
                status=status,
            )

        app = web.Application()
        app.router.add_post("/v1/killswitch/trigger", handle_trigger)
        app.router.add_get("/v1/killswitch/status", handle_status)
        app.router.add_post("/v1/killswitch/retry-flatten", handle_retry)
        app.router.add_post("/v1/killswitch/acknowledge", handle_acknowledge)
        app.router.add_post("/v1/killswitch/restart-request", handle_restart)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info("[KS] REST API listening on http://%s:%d/v1/killswitch/", host, port)
        return runner
