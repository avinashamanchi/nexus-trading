"""
Core data models for the Autonomous Cooperative-AI Trading System.
Pydantic v2 models used by all 17 agents and the pipeline coordinator.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from core.enums import (
    AgentState,
    ChangeProposalStatus,
    ChangeProposalType,
    Direction,
    ExitMode,
    FeedAnomaly,
    FeedStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    ReconciliationStatus,
    RegimeLabel,
    RiskDecision,
    RiskRejectReason,
    SetupType,
    SystemState,
    TimeInForce,
    Topic,
    ValidationFailReason,
    ValidationResult,
)


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.utcnow()


# ═══════════════════════════════════════════════════════════════════════════════
#  Market Data
# ═══════════════════════════════════════════════════════════════════════════════

class MarketTick(BaseModel):
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float
    volume: int
    sequence: int = 0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.spread / mid) * 10_000 if mid > 0 else 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class L2Level(BaseModel):
    price: float
    size: int


class OrderBook(BaseModel):
    symbol: str
    timestamp: datetime
    bids: list[L2Level] = Field(default_factory=list)  # descending by price
    asks: list[L2Level] = Field(default_factory=list)  # ascending by price

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None


class BarData(BaseModel):
    """OHLCV bar."""
    symbol: str
    timestamp: datetime
    timeframe: str          # "1m", "5m", etc.
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None


# ═══════════════════════════════════════════════════════════════════════════════
#  Symbol Classification (Agent 1 — Market Universe)
# ═══════════════════════════════════════════════════════════════════════════════

class SymbolClassification(BaseModel):
    symbol: str
    approved: bool = True
    float_shares_million: float | None = None
    avg_daily_volume: int | None = None
    spread_tier: str | None = None          # "tight" | "moderate" | "wide"
    sector: str | None = None
    catalyst: str | None = None             # earnings, news, sector rotation, etc.
    news_sensitive: bool = False
    borrow_restricted: bool = False
    halt_prone: bool = False
    reject_reason: str | None = None
    classified_at: datetime = Field(default_factory=_now)


class UniverseSnapshot(BaseModel):
    session_date: str           # YYYY-MM-DD
    symbols: list[SymbolClassification]
    generated_at: datetime = Field(default_factory=_now)

    @property
    def approved_symbols(self) -> list[str]:
        return [s.symbol for s in self.symbols if s.approved]


# ═══════════════════════════════════════════════════════════════════════════════
#  Approved Setup (Agent 0 — Edge Research)
# ═══════════════════════════════════════════════════════════════════════════════

class ApprovedSetup(BaseModel):
    setup_type: SetupType
    expectancy_per_share: float     # net EV after all costs
    win_rate: float
    avg_win: float
    avg_loss: float
    sample_count: int
    out_of_sample_validated: bool = False
    allowed_regimes: list[RegimeLabel]
    last_validated: datetime = Field(default_factory=_now)
    rolling_expectancy: float | None = None
    retired: bool = False
    retire_reason: str | None = None


class ApprovedSetupList(BaseModel):
    setups: list[ApprovedSetup]
    version: int = 1
    effective_session: str | None = None    # YYYY-MM-DD, None = current
    generated_at: datetime = Field(default_factory=_now)

    def is_approved(self, setup_type: SetupType) -> bool:
        return any(
            s.setup_type == setup_type and not s.retired
            for s in self.setups
        )

    def get_setup(self, setup_type: SetupType) -> ApprovedSetup | None:
        return next(
            (s for s in self.setups if s.setup_type == setup_type and not s.retired),
            None,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  Market Regime (Agent 2)
# ═══════════════════════════════════════════════════════════════════════════════

class RegimeAssessment(BaseModel):
    label: RegimeLabel
    vix_level: float | None = None
    trend_strength: float | None = None     # 0–1
    news_flags: list[str] = Field(default_factory=list)
    allowed_setup_types: list[SetupType] = Field(default_factory=list)
    confidence: float = 1.0
    assessed_at: datetime = Field(default_factory=_now)
    reasoning: str | None = None            # LLM explanation


# ═══════════════════════════════════════════════════════════════════════════════
#  Data Integrity (Agent 3)
# ═══════════════════════════════════════════════════════════════════════════════

class FeedStatusReport(BaseModel):
    status: FeedStatus
    anomalies: list[FeedAnomaly] = Field(default_factory=list)
    affected_symbols: list[str] = Field(default_factory=list)
    details: str | None = None
    reported_at: datetime = Field(default_factory=_now)


# ═══════════════════════════════════════════════════════════════════════════════
#  Signals (Agents 4 & 5)
# ═══════════════════════════════════════════════════════════════════════════════

class CandidateSignal(BaseModel):
    signal_id: str = Field(default_factory=_uid)
    symbol: str
    setup_type: SetupType
    direction: Direction
    entry_price: float
    initial_stop: float
    initial_target: float
    regime: RegimeLabel
    tick_at_signal: MarketTick
    detected_at: datetime = Field(default_factory=_now)
    reasoning: str | None = None            # MiSA's LLM explanation

    @property
    def risk_reward(self) -> float:
        r = abs(self.entry_price - self.initial_stop)
        t = abs(self.initial_target - self.entry_price)
        return t / r if r > 0 else 0.0


class ValidatedSignal(BaseModel):
    signal_id: str                          # same as CandidateSignal.signal_id
    candidate: CandidateSignal
    result: ValidationResult
    fail_reasons: list[ValidationFailReason] = Field(default_factory=list)
    htf_aligned: bool = False
    volume_ok: bool = False
    spread_ok: bool = False
    catalyst_consistent: bool = False
    regime_consistent: bool = False
    validated_at: datetime = Field(default_factory=_now)


# ═══════════════════════════════════════════════════════════════════════════════
#  Trade Plan (Agent 7 — SPA)
# ═══════════════════════════════════════════════════════════════════════════════

class CostModel(BaseModel):
    commission: float = 0.0
    sec_fee: float = 0.0
    finra_taf: float = 0.0
    estimated_slippage: float = 0.0

    @property
    def total_cost_per_share(self) -> float:
        return self.commission + self.sec_fee + self.finra_taf + self.estimated_slippage


class TradePlan(BaseModel):
    plan_id: str = Field(default_factory=_uid)
    signal_id: str
    symbol: str
    direction: Direction
    entry_price: float
    stop_price: float
    target_price: float
    shares: int
    time_stop_sec: int              # max hold time in seconds
    invalidation_reason: str        # what would make this trade wrong
    cost_model: CostModel
    net_expectancy_per_share: float
    account_equity_at_plan: float
    risk_per_share: float
    total_risk: float
    worst_case_slippage: float
    created_at: datetime = Field(default_factory=_now)

    @model_validator(mode='after')
    def validate_positive_expectancy(self) -> 'TradePlan':
        if self.net_expectancy_per_share <= 0:
            raise ValueError(
                f"TradePlan rejected: net_expectancy_per_share "
                f"{self.net_expectancy_per_share:.4f} is not positive"
            )
        return self

    @model_validator(mode='after')
    def validate_has_stop(self) -> 'TradePlan':
        if self.stop_price <= 0:
            raise ValueError("TradePlan requires a valid stop_price > 0")
        return self


# ═══════════════════════════════════════════════════════════════════════════════
#  Orders (Agent 8 — Execution)
# ═══════════════════════════════════════════════════════════════════════════════

class Order(BaseModel):
    order_id: str = Field(default_factory=_uid)
    broker_order_id: str | None = None
    plan_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: int
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    reject_reason: str | None = None


class FillEvent(BaseModel):
    fill_id: str = Field(default_factory=_uid)
    order_id: str
    symbol: str
    qty: int
    price: float
    side: OrderSide
    filled_at: datetime = Field(default_factory=_now)


# ═══════════════════════════════════════════════════════════════════════════════
#  Positions (Agent 11 — MMA)
# ═══════════════════════════════════════════════════════════════════════════════

class Position(BaseModel):
    position_id: str = Field(default_factory=_uid)
    plan_id: str
    symbol: str
    direction: Direction
    shares: int
    entry_price: float
    current_price: float
    stop_price: float
    target_price: float
    time_stop_at: datetime          # absolute time stop
    opened_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    trailing_stop: float | None = None

    @property
    def unrealized_pnl(self) -> float:
        sign = 1 if self.direction == Direction.LONG else -1
        return (self.current_price - self.entry_price) * self.shares * sign

    @property
    def unrealized_pnl_pct(self) -> float:
        return self.unrealized_pnl / (self.entry_price * self.shares)

    @property
    def seconds_held(self) -> float:
        return (datetime.utcnow() - self.opened_at).total_seconds()


# ═══════════════════════════════════════════════════════════════════════════════
#  Trade Record (complete lifecycle)
# ═══════════════════════════════════════════════════════════════════════════════

class ClosedTrade(BaseModel):
    trade_id: str = Field(default_factory=_uid)
    plan_id: str
    symbol: str
    setup_type: SetupType
    regime: RegimeLabel
    direction: Direction
    shares: int
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    exit_mode: ExitMode
    gross_pnl: float
    net_pnl: float
    commission: float
    slippage: float
    hold_time_sec: float
    opened_at: datetime
    closed_at: datetime = Field(default_factory=_now)

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0


# ═══════════════════════════════════════════════════════════════════════════════
#  Portfolio State (Agent 13 — PSA)
# ═══════════════════════════════════════════════════════════════════════════════

class PortfolioState(BaseModel):
    session_date: str
    account_equity: float
    starting_equity: float
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    open_positions: list[Position] = Field(default_factory=list)
    closed_trades_today: list[ClosedTrade] = Field(default_factory=list)
    consecutive_losses: int = 0
    system_state: SystemState = SystemState.TRADING
    halt_reason: str | None = None
    anomaly_hold: bool = False
    pdt_trades_used: int = 0
    updated_at: datetime = Field(default_factory=_now)

    @property
    def open_symbols(self) -> list[str]:
        return [p.symbol for p in self.open_positions]

    @property
    def is_halted(self) -> bool:
        return self.system_state in (SystemState.HALTED, SystemState.PAUSED)


# ═══════════════════════════════════════════════════════════════════════════════
#  Execution Quality (Agent 10)
# ═══════════════════════════════════════════════════════════════════════════════

class ExecutionMetrics(BaseModel):
    order_id: str
    symbol: str
    expected_price: float
    actual_fill_price: float
    slippage_bps: float
    order_to_fill_latency_ms: float
    cancel_latency_ms: float | None = None
    rejected: bool = False
    reject_reason: str | None = None
    measured_at: datetime = Field(default_factory=_now)


class SessionExecutionSummary(BaseModel):
    session_date: str
    total_orders: int
    fill_rate: float
    avg_slippage_bps: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    rejection_rate: float
    quality_degraded: bool = False
    generated_at: datetime = Field(default_factory=_now)


# ═══════════════════════════════════════════════════════════════════════════════
#  Broker Reconciliation (Agent 9)
# ═══════════════════════════════════════════════════════════════════════════════

class ReconciliationReport(BaseModel):
    status: ReconciliationStatus
    internal_positions: list[str] = Field(default_factory=list)
    broker_positions: list[str] = Field(default_factory=list)
    discrepancies: list[str] = Field(default_factory=list)
    buying_power_ok: bool = True
    duplicate_order_risk: bool = False
    reconciled_at: datetime = Field(default_factory=_now)


# ═══════════════════════════════════════════════════════════════════════════════
#  Change Proposals (§5.5 — PSRA → Edge Research handoff)
# ═══════════════════════════════════════════════════════════════════════════════

class ChangeProposal(BaseModel):
    proposal_id: str = Field(default_factory=_uid)
    proposal_type: ChangeProposalType
    status: ChangeProposalStatus = ChangeProposalStatus.PROPOSED
    submitted_by: str = "PSRA"
    proposed_for_session: str | None = None     # YYYY-MM-DD
    description: str
    parameter_changes: dict[str, Any] = Field(default_factory=dict)
    hgl_approved_by: str | None = None
    hgl_approved_at: datetime | None = None
    hgl_rejection_reason: str | None = None
    edge_validation_result: str | None = None
    edge_rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ═══════════════════════════════════════════════════════════════════════════════
#  Agent Heartbeat
# ═══════════════════════════════════════════════════════════════════════════════

class AgentHeartbeat(BaseModel):
    agent_id: str
    agent_name: str
    state: AgentState
    last_processed: datetime | None = None
    error_count: int = 0
    timestamp: datetime = Field(default_factory=_now)


# ═══════════════════════════════════════════════════════════════════════════════
#  Message Bus Envelope
# ═══════════════════════════════════════════════════════════════════════════════

class BusMessage(BaseModel):
    message_id: str = Field(default_factory=_uid)
    topic: Topic
    source_agent: str
    payload: dict[str, Any]
    timestamp: datetime = Field(default_factory=_now)
    correlation_id: str | None = None       # tracks a signal through the pipeline


# ═══════════════════════════════════════════════════════════════════════════════
#  Audit / Compliance (Agent 15)
# ═══════════════════════════════════════════════════════════════════════════════

class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=_uid)
    event_type: str
    agent: str
    symbol: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_now)
    immutable: bool = True      # flag — never modified after write


class WashSaleFlag(BaseModel):
    symbol: str
    last_exit_at: datetime
    last_exit_price: float
    net_loss: float
    wash_sale_window_end: datetime  # 30 days from last_exit
    flagged_at: datetime = Field(default_factory=_now)


# ═══════════════════════════════════════════════════════════════════════════════
#  Shadow Mode
# ═══════════════════════════════════════════════════════════════════════════════

class ShadowTrade(BaseModel):
    """A trade that was planned but not executed (shadow mode)."""
    shadow_id: str = Field(default_factory=_uid)
    plan: TradePlan
    simulated_entry: float
    simulated_exit: float | None = None
    simulated_exit_mode: ExitMode | None = None
    simulated_pnl: float | None = None
    simulated_hold_sec: float | None = None
    session_date: str = ""
    created_at: datetime = Field(default_factory=_now)


class ShadowSessionSummary(BaseModel):
    session_date: str
    total_shadow_trades: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: float
    net_pnl: float
    max_drawdown: float
    sharpe_contribution: float
    regime_coverage: list[str] = Field(default_factory=list)
    data_integrity_events: int = 0
    reconciliation_events: int = 0
