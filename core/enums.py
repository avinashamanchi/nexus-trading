"""
Core enumerations for the Autonomous Cooperative-AI Trading System.
These are used by all 17 agents and the pipeline coordinator.
"""
from enum import Enum, auto


# ─── Market Regime ─────────────────────────────────────────────────────────────

class RegimeLabel(str, Enum):
    TREND_DAY = "trend-day"
    MEAN_REVERSION_DAY = "mean-reversion-day"
    EVENT_DRIVEN_DAY = "event-driven-day"
    HIGH_VOLATILITY_NO_TRADE = "high-volatility/no-trade"
    LOW_LIQUIDITY_NO_TRADE = "low-liquidity/no-trade"

    @property
    def is_tradeable(self) -> bool:
        return self in (
            RegimeLabel.TREND_DAY,
            RegimeLabel.MEAN_REVERSION_DAY,
            RegimeLabel.EVENT_DRIVEN_DAY,
        )


# ─── Setup / Strategy Types ────────────────────────────────────────────────────

class SetupType(str, Enum):
    BREAKOUT = "breakout"
    VWAP_TAP = "vwap_tap"
    LIQUIDITY_SWEEP = "liquidity_sweep"
    MOMENTUM_BURST = "momentum_burst"
    OPENING_RANGE_BREAK = "opening_range_break"
    MEAN_REVERSION_FADE = "mean_reversion_fade"
    CATALYST_MOVER = "catalyst_mover"
    GAP_AND_GO = "gap_and_go"


# ─── Trade Direction ───────────────────────────────────────────────────────────

class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


# ─── Order & Execution ─────────────────────────────────────────────────────────

class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"           # emergency flatten only
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    BRACKET = "bracket"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SELL_SHORT = "sell_short"
    BUY_TO_COVER = "buy_to_cover"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


# ─── Exit Modes (Agent 12) ─────────────────────────────────────────────────────

class ExitMode(str, Enum):
    PROFIT_TARGET = "profit_target"
    TIME_STOP = "time_stop"
    MOMENTUM_FAILURE = "momentum_failure"
    VOLATILITY_SHOCK = "volatility_shock"
    REGIME_FLIP = "regime_flip"
    PORTFOLIO_RISK_BREACH = "portfolio_risk_breach"


# ─── System State Machine ──────────────────────────────────────────────────────

class SystemState(str, Enum):
    INITIALIZING = "initializing"
    SHADOW_MODE = "shadow_mode"
    PRE_SESSION = "pre_session"
    SESSION_OPEN = "session_open"
    TRADING = "trading"
    PAUSED = "paused"           # temporary pause (circuit breaker)
    HALTED = "halted"           # PSA halt — requires explicit clearance
    POST_SESSION = "post_session"
    SHUTDOWN = "shutdown"


# ─── §5.6 Formal 9-State Session State Machine ────────────────────────────────

class SessionState(str, Enum):
    """
    Owned exclusively by PSA (Agent 13).
    All transitions must follow the permitted-transition table.
    """
    BOOTING = "booting"
    WAITING_FOR_DATA_INTEGRITY = "waiting_for_data_integrity"
    WAITING_FOR_REGIME = "waiting_for_regime"
    ACTIVE = "active"
    PAUSED_COOLDOWN = "paused_cooldown"
    HALTED_RISK = "halted_risk"
    HALTED_ANOMALY = "halted_anomaly"
    HALTED_RECONCILIATION = "halted_reconciliation"
    SHUTDOWN = "shutdown"


# Permitted transition table: {from_state: {to_states}}
SESSION_STATE_TRANSITIONS: dict = {
    "booting": {"waiting_for_data_integrity", "shutdown"},
    "waiting_for_data_integrity": {"waiting_for_regime", "halted_anomaly", "shutdown"},
    "waiting_for_regime": {"active", "halted_anomaly", "shutdown"},
    "active": {"paused_cooldown", "halted_risk", "halted_anomaly", "halted_reconciliation", "shutdown"},
    "paused_cooldown": {"active", "halted_risk", "halted_anomaly", "shutdown"},
    "halted_risk": {"active", "shutdown"},           # requires human approval
    "halted_anomaly": {"active", "shutdown"},        # requires human approval
    "halted_reconciliation": {"active", "shutdown"}, # requires human approval
    "shutdown": set(),                               # terminal
}


class AgentState(str, Enum):
    IDLE = "idle"
    PROCESSING = "processing"
    BLOCKED = "blocked"         # waiting on upstream gate
    ERROR = "error"
    SHUTDOWN = "shutdown"


# ─── System Health (Agent 19) ─────────────────────────────────────────────────

class HealthSeverity(str, Enum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


class HealthDimension(str, Enum):
    CPU = "cpu"
    MEMORY = "memory"
    EVENT_LOOP_LAG = "event_loop_lag"
    BUS_LATENCY = "bus_latency"
    API_LATENCY = "api_latency"
    WEBSOCKET = "websocket"
    RECONCILIATION_DRIFT = "reconciliation_drift"
    DISK = "disk"


# ─── Data Feed ─────────────────────────────────────────────────────────────────

class FeedStatus(str, Enum):
    CLEAN = "clean"
    DEGRADED = "degraded"
    HALTED = "halted"


class FeedAnomaly(str, Enum):
    STALE_TICK = "stale_tick"
    DUPLICATE_TICK = "duplicate_tick"
    CROSSED_MARKET = "crossed_market"
    OUT_OF_SEQUENCE = "out_of_sequence"
    FEED_DIVERGENCE = "feed_divergence"
    MISSING_L2 = "missing_l2"


# ─── Signal Validation ─────────────────────────────────────────────────────────

class ValidationResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class ValidationFailReason(str, Enum):
    NO_HTF_ALIGNMENT = "no_higher_timeframe_alignment"
    INSUFFICIENT_VOLUME = "insufficient_volume"
    SPREAD_TOO_WIDE = "spread_too_wide"
    CATALYST_MISMATCH = "catalyst_mismatch"
    REGIME_CONTRADICTION = "regime_contradiction"
    EDGE_NOT_APPROVED = "edge_not_approved"
    DATA_FEED_DIRTY = "data_feed_dirty"


# ─── Risk / TERA ───────────────────────────────────────────────────────────────

class RiskDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RiskRejectReason(str, Enum):
    DAILY_LOSS_CAP = "daily_loss_cap_reached"
    DRAWDOWN_VELOCITY = "drawdown_velocity_exceeded"
    MAX_CONCURRENT_TRADES = "max_concurrent_trades_reached"
    SYMBOL_CONCENTRATION = "symbol_concentration_limit"
    SECTOR_CONCENTRATION = "sector_concentration_limit"
    CORRELATION_BUCKET = "correlation_bucket_limit"
    CONSECUTIVE_LOSSES = "consecutive_loss_cooldown"
    ANOMALY_HOLD = "no_trade_after_anomaly"
    PDT_LIMIT = "pdt_limit_reached"
    WASH_SALE_FLAG = "wash_sale_flag"
    ACCOUNT_BELOW_PDT = "account_below_pdt_threshold"


# ─── Broker Reconciliation ─────────────────────────────────────────────────────

class ReconciliationStatus(str, Enum):
    CLEAN = "clean"
    MISMATCH = "mismatch"
    PENDING = "pending"


# ─── Change Proposal Types (§5.5) ─────────────────────────────────────────────

class ChangeProposalType(str, Enum):
    PARAMETER_TWEAK = "parameter_tweak"
    SETUP_MODIFICATION = "setup_modification"
    SETUP_RETIREMENT = "setup_retirement"
    NEW_SETUP_ADDITION = "new_setup_addition"


class ChangeProposalStatus(str, Enum):
    PROPOSED = "proposed"
    AWAITING_HGL = "awaiting_hgl"
    HGL_APPROVED = "hgl_approved"
    HGL_REJECTED = "hgl_rejected"
    EDGE_VALIDATING = "edge_validating"
    EDGE_PASSED = "edge_passed"
    EDGE_FAILED = "edge_failed"
    LIVE = "live"


# ─── Message Bus Topics ────────────────────────────────────────────────────────

class Topic(str, Enum):
    # Pipeline topics (ordered by pipeline step)
    APPROVED_SETUPS = "approved_setups"
    UNIVERSE_UPDATE = "universe_update"
    REGIME_UPDATE = "regime_update"
    FEED_STATUS = "feed_status"
    CANDIDATE_SIGNAL = "candidate_signal"
    VALIDATED_SIGNAL = "validated_signal"
    RISK_DECISION = "risk_decision"
    TRADE_PLAN = "trade_plan"
    RECONCILIATION_STATUS = "reconciliation_status"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_UPDATE = "order_update"
    FILL_EVENT = "fill_event"
    EXECUTION_QUALITY = "execution_quality"
    POSITION_UPDATE = "position_update"
    EXIT_TRIGGERED = "exit_triggered"
    TRADE_CLOSED = "trade_closed"

    # Supervision topics
    SYSTEM_STATE = "system_state"
    PSA_ALERT = "psa_alert"
    HALT_COMMAND = "halt_command"
    RESUME_COMMAND = "resume_command"
    DAILY_PNL = "daily_pnl"

    # Governance topics
    CHANGE_PROPOSAL = "change_proposal"
    HGL_DECISION = "hgl_decision"
    AUDIT_EVENT = "audit_event"

    # Monitoring
    AGENT_HEARTBEAT = "agent_heartbeat"
    STARVATION_ALERT = "starvation_alert"

    # Cross-cutting (Agents 17–19)
    CLOCK_TICK = "clock_tick"            # GCA authoritative tick
    SESSION_START = "session_start"      # GCA: market open
    SESSION_END = "session_end"          # GCA: market close
    TIMER_EXPIRED = "timer_expired"      # GCA: named timer fired
    HEALTH_UPDATE = "health_update"      # SHA infrastructure health
    REPLAY_REQUEST = "replay_request"    # SMRE trigger
    REPLAY_REPORT = "replay_report"      # SMRE diff report
    KILL_SWITCH = "kill_switch"          # emergency flatten command


# ─── Institutional / Multi-Asset ──────────────────────────────────────────────

class AssetClass(str, Enum):
    EQUITY = "equity"
    FX = "fx"
    FUTURES = "futures"
    OPTIONS = "options"
    FIXED_INCOME = "fixed_income"
    CRYPTO = "crypto"


class OrderVenue(str, Enum):
    NYSE = "nyse"
    NASDAQ = "nasdaq"
    BATS = "bats"
    IEX = "iex"
    DARK_POOL = "dark_pool"
    FX_ECN = "fx_ecn"
    CME = "cme"
    CBOE = "cboe"
    DIRECT_DMA = "direct_dma"


class QuoteSide(str, Enum):
    BID = "bid"
    ASK = "ask"
    BOTH = "both"


class InventoryAction(str, Enum):
    SKEW_BID = "skew_bid"       # widen ask, tighten bid (unload long)
    SKEW_ASK = "skew_ask"       # widen bid, tighten ask (unload short)
    NEUTRAL = "neutral"
    PULL_QUOTES = "pull_quotes"  # inventory limit hit


class StatArbSignal(str, Enum):
    ENTER_LONG_SPREAD = "enter_long_spread"   # spread too wide, mean-revert
    ENTER_SHORT_SPREAD = "enter_short_spread"
    EXIT = "exit"
    NO_SIGNAL = "no_signal"


class AltDataSource(str, Enum):
    SATELLITE = "satellite"
    CREDIT_CARD = "credit_card"
    SHIPPING_MANIFEST = "shipping_manifest"
    NEWS_NLP = "news_nlp"
    SOCIAL_SENTIMENT = "social_sentiment"
    WEATHER = "weather"
    JOB_POSTINGS = "job_postings"
    APP_DOWNLOADS = "app_downloads"
    PATENT_FILINGS = "patent_filings"
    EARNINGS_WHISPER = "earnings_whisper"


class AltDataSignal(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    INSUFFICIENT_DATA = "insufficient_data"


# ─── FIX Protocol ─────────────────────────────────────────────────────────────

class FIXMsgType(str, Enum):
    LOGON = "A"
    LOGOUT = "5"
    HEARTBEAT = "0"
    NEW_ORDER_SINGLE = "D"
    ORDER_CANCEL_REQUEST = "F"
    ORDER_CANCEL_REPLACE = "G"
    EXECUTION_REPORT = "8"
    ORDER_CANCEL_REJECT = "9"
    MARKET_DATA_REQUEST = "V"
    MARKET_DATA_SNAPSHOT = "W"
    MARKET_DATA_INCREMENTAL = "X"
    QUOTE = "S"
    QUOTE_REQUEST = "R"
    MASS_QUOTE = "i"


class FIXSessionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    LOGON_SENT = "logon_sent"
    ACTIVE = "active"
    LOGOUT_PENDING = "logout_pending"


class FIXOrdStatus(str, Enum):
    NEW = "0"
    PARTIALLY_FILLED = "1"
    FILLED = "2"
    CANCELLED = "4"
    REJECTED = "8"
    PENDING_NEW = "A"
    EXPIRED = "C"


# ─── Regulatory / CAT ─────────────────────────────────────────────────────────

class CATEventType(str, Enum):
    NEW_ORDER = "MENO"           # Manual Exchange New Order
    ROUTE_ORDER = "MEOR"         # Route to Exchange
    FILL = "MEEF"                # Exchange Fill
    CANCEL = "MEOC"              # Order Cancel
    CANCEL_REPLACE = "MEOCR"     # Cancel/Replace
    QUOTE = "MEQT"               # Market Maker Quote
    EXCEPTION = "MEXC"           # CAT exception report


class SpoofingRisk(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"         # triggers alert + hold
    CRITICAL = "critical"  # triggers immediate halt + regulatory report


# ─── Prime Broker / Clearing ──────────────────────────────────────────────────

class PrimeBroker(str, Enum):
    GOLDMAN_SACHS = "goldman_sachs"
    MORGAN_STANLEY = "morgan_stanley"
    JP_MORGAN = "jp_morgan"
    INTERACTIVE_BROKERS = "interactive_brokers"  # institutional tier
    ALPACA = "alpaca"                             # current paper/dev


class MarginType(str, Enum):
    REG_T = "reg_t"          # standard US margin (50% initial, 25% maintenance)
    PORTFOLIO_MARGIN = "portfolio_margin"  # risk-based, much lower for hedged books
    CROSS_MARGIN = "cross_margin"  # cross-collateralized across positions
    INTRADAY = "intraday"    # same-day flat requirement


# ─── Co-location / Latency ────────────────────────────────────────────────────

class DataCenter(str, Enum):
    EQUINIX_NY4 = "ny4"         # New Jersey — primary US equities
    EQUINIX_NY5 = "ny5"         # New Jersey — backup
    CME_AURORA = "aurora"        # Chicago — derivatives
    EQUINIX_LD4 = "ld4"         # London — European equities
    EQUINIX_TY3 = "ty3"         # Tokyo — Asian equities
    EQUINIX_HK1 = "hk1"         # Hong Kong


class LatencyTier(str, Enum):
    RETAIL = "retail"           # 50–200ms — current
    INSTITUTIONAL = "institutional"  # 1–10ms — co-located
    HFT = "hft"                 # 1–100µs — FPGA
    ULTRA_HFT = "ultra_hft"     # sub-microsecond — custom ASICs
