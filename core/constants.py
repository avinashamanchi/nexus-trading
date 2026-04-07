"""
System-wide constants. Runtime parameters come from config.yaml.
"""

# ─── Session Hours (US Eastern) ───────────────────────────────────────────────
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0
PRE_MARKET_OPEN_HOUR = 4

# ─── PDT ──────────────────────────────────────────────────────────────────────
PDT_MINIMUM_EQUITY = 25_000.00
PDT_MAX_DAY_TRADES_PER_WINDOW = 3
PDT_ROLLING_WINDOW_DAYS = 5

# ─── SEC Fee Rate (as of 2024) ─────────────────────────────────────────────────
SEC_FEE_RATE = 0.0000278       # $0.0000278 per $ of sale proceeds
FINRA_TAF_RATE = 0.000166      # $0.000166 per share (sell side), max $8.30/trade

# ─── Wash Sale Window ─────────────────────────────────────────────────────────
WASH_SALE_DAYS = 30

# ─── Agent IDs ────────────────────────────────────────────────────────────────
AGENT_IDS = {
    0:  "edge_research",
    1:  "market_universe",
    2:  "market_regime",
    3:  "data_integrity",
    4:  "micro_signal",
    5:  "signal_validation",
    6:  "tera",
    7:  "spa",
    8:  "execution",
    9:  "broker_reconciliation",
    10: "execution_quality",
    11: "micro_monitoring",
    12: "exit_lockin",
    13: "portfolio_supervisor",
    14: "post_session_review",
    15: "tax_compliance",
    16: "human_governance",
    17: "global_clock",
    18: "shadow_replay",
    19: "system_health",
    20: "market_making",
    21: "stat_arb",
    22: "alt_data",
    23: "cat_compliance",
}

AGENT_NAMES = {v: k for k, v in AGENT_IDS.items()}

# ─── Heartbeat ─────────────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL_SEC = 0.5
AGENT_TIMEOUT_SEC = 5.0         # agent considered dead if heartbeat gap > this

# ─── Message Bus ──────────────────────────────────────────────────────────────
BUS_RECONNECT_ATTEMPTS = 3
BUS_RECONNECT_DELAY_SEC = 2.0
BUS_SAFE_MODE_LATENCY_MS = 500  # safe mode if no heartbeat within this

# ─── LLM ──────────────────────────────────────────────────────────────────────
LLM_MODEL = "claude-sonnet-4-6"
LLM_MAX_TOKENS = 4096
LLM_TEMPERATURE = 0.1
LLM_TIMEOUT_SEC = 30

# ─── Timeframes ───────────────────────────────────────────────────────────────
TF_1M = "1m"
TF_5M = "5m"
TF_15M = "15m"
TF_1D = "1d"

# ─── Alpaca endpoints ─────────────────────────────────────────────────────────
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"
ALPACA_DATA_WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
ALPACA_PAPER_WS_URL = "wss://stream.data.alpaca.markets/v2/test"

# ─── Risk Labels ──────────────────────────────────────────────────────────────
SPREAD_TIER_TIGHT = "tight"     # < 5 bps
SPREAD_TIER_MODERATE = "moderate"  # 5–15 bps
SPREAD_TIER_WIDE = "wide"       # > 15 bps
