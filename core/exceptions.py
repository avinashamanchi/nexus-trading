"""
Custom exceptions for the Autonomous Cooperative-AI Trading System.
All exceptions carry enough context for audit logging.
"""
from __future__ import annotations


class TradingSystemError(Exception):
    """Base exception for all system errors."""


# ─── Pipeline Gate Failures ────────────────────────────────────────────────────

class PipelineGateError(TradingSystemError):
    """Raised when a pipeline step rejects a signal/trade."""
    def __init__(self, gate: str, reason: str, context: dict | None = None):
        self.gate = gate
        self.reason = reason
        self.context = context or {}
        super().__init__(f"[{gate}] Gate rejected: {reason}")


class NoApprovedEdgeError(PipelineGateError):
    """Setup not in approved list (Agent 0)."""
    def __init__(self, setup_type: str):
        super().__init__("EdgeResearch", f"Setup '{setup_type}' not approved or retired")


class NoTradeRegimeError(PipelineGateError):
    """Market regime is no-trade (Agent 2)."""
    def __init__(self, regime: str):
        super().__init__("MarketRegime", f"No-trade regime active: {regime}")


class FeedDirtyError(PipelineGateError):
    """Data feed is not clean (Agent 3)."""
    def __init__(self, anomaly: str):
        super().__init__("DataIntegrity", f"Feed anomaly: {anomaly}")


class SignalValidationError(PipelineGateError):
    """Signal failed multi-dimensional validation (Agent 5)."""
    def __init__(self, reason: str):
        super().__init__("SignalValidation", reason)


class RiskRejectionError(PipelineGateError):
    """Trade rejected by TERA (Agent 6)."""
    def __init__(self, reason: str, details: dict | None = None):
        super().__init__("TERA", reason, details)


class ReconciliationMismatchError(PipelineGateError):
    """Broker state mismatch detected (Agent 9)."""
    def __init__(self, details: str):
        super().__init__("BrokerReconciliation", f"Mismatch: {details}")


# ─── Execution Errors ─────────────────────────────────────────────────────────

class OrderSubmissionError(TradingSystemError):
    """Failed to submit an order to the broker."""
    def __init__(self, symbol: str, reason: str):
        self.symbol = symbol
        super().__init__(f"Order submission failed for {symbol}: {reason}")


class PositionWithoutStopError(TradingSystemError):
    """Attempt to enter a position without an active stop — structurally blocked."""
    def __init__(self, symbol: str):
        super().__init__(f"FATAL: Position entry blocked — no stop defined for {symbol}")


class PriceChaseLimitError(TradingSystemError):
    """Price moved beyond the allowable chase tolerance."""
    def __init__(self, symbol: str, chase_bps: float, limit_bps: float):
        super().__init__(
            f"{symbol}: price chase {chase_bps:.1f}bps exceeds limit {limit_bps:.1f}bps"
        )


# ─── System State Errors ───────────────────────────────────────────────────────

class SystemHaltedError(TradingSystemError):
    """Raised when any agent attempts action while system is halted."""
    def __init__(self, reason: str):
        super().__init__(f"System is halted — action blocked: {reason}")


class PDTLimitError(TradingSystemError):
    """Account would breach PDT rule."""
    def __init__(self, day_trades_used: int):
        super().__init__(
            f"PDT limit: {day_trades_used}/3 day trades used in rolling 5-day window"
        )


class AccountBelowPDTThresholdError(TradingSystemError):
    """Account equity below PDT minimum — trading halted."""
    def __init__(self, equity: float, minimum: float):
        super().__init__(
            f"Account equity ${equity:,.2f} below PDT minimum ${minimum:,.2f} — HALT"
        )


# ─── Infrastructure Errors ─────────────────────────────────────────────────────

class MessageBusError(TradingSystemError):
    """Message bus unavailable or failed."""


class StateStoreError(TradingSystemError):
    """State persistence failure."""


class BrokerConnectionError(TradingSystemError):
    """Broker API is unreachable."""


class BrokerAPIError(TradingSystemError):
    """Broker API returned an error response."""
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Broker API error {status}: {message}")


# ─── Governance Errors ─────────────────────────────────────────────────────────

class UnauthorizedChangeError(TradingSystemError):
    """Attempted to modify a live parameter without HGL approval."""
    def __init__(self, parameter: str, agent: str):
        super().__init__(
            f"Agent '{agent}' attempted to modify '{parameter}' without HGL approval"
        )


class IntraSessionChangeError(TradingSystemError):
    """Attempted to apply a parameter change during an active session."""
    def __init__(self, parameter: str):
        super().__init__(
            f"Parameter change to '{parameter}' rejected — no intraday changes permitted"
        )
