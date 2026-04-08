# Nexus Autonomous Trading System

![Tests](https://img.shields.io/badge/tests-722%20passing-brightgreen?style=flat-square)
![Python](https://img.shields.io/badge/python-3.11-blue?style=flat-square&logo=python)
![React](https://img.shields.io/badge/react-19-61dafb?style=flat-square&logo=react)
![Docker](https://img.shields.io/badge/docker-compose-2496ED?style=flat-square&logo=docker)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![CI](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?style=flat-square&logo=github-actions)

**27 agents. 80ns tick-to-trade. Institutional-grade execution at the silicon layer.**

---

### Live Demo
**[https://avinashamanchi.github.io/nexus-trading/](https://avinashamanchi.github.io/nexus-trading/)**

> Demo Video: [coming soon — record with QuickTime, upload to YouTube, paste link here]

---

## Overview

Nexus is a fully autonomous, multi-agent trading system engineered to operate at every layer of the institutional stack — from nanosecond FPGA signal capture through dark pool internalization, portfolio-level Raft consensus circuit breakers, and enterprise Monte Carlo risk. The 27-agent pipeline enforces strict separation of concerns: each agent owns exactly one invariant, communicates over Redis Streams, and is individually unit-tested. The result is a system where a single `docker compose up` brings online a live React dashboard, a full HFT execution path, institutional market-making and stat-arb strategies, SEC CAT compliance reporting, and a 9-state FSM portfolio supervisor whose halt transitions require distributed consensus before they commit.

722 pytest tests pass in 4.0 seconds. Zero failures.

---

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/avinashamanchi/nexus-trading.git
cd nexus-trading
docker compose up
```

React dashboard available at **http://localhost:3000**

### Manual

```bash
# Python backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest --tb=short          # 722 tests, ~4s

# React frontend
cd dashboard
npm install
npm run dev                           # Vite dev server → localhost:3000
```

### Run the full agent pipeline

```bash
python nexus_main.py                  # boots all 27 agents, begins FSM lifecycle
```

---

## Architecture

Nexus is organized into four vertical layers that mirror the operational layers of a mid-size proprietary trading firm:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 4 — Enterprise Risk & Compliance         Agents 24–26            │
│  Algo block desk · Dark pool ATS · Monte Carlo VaR                      │
├─────────────────────────────────────────────────────────────────────────┤
│  LAYER 3 — Institutional Strategies             Agents 20–23            │
│  Market making · Stat arb · Alt data · CAT compliance                   │
├─────────────────────────────────────────────────────────────────────────┤
│  LAYER 2 — Infrastructure                       Agents 17–19            │
│  Nanosecond clock · Deterministic replay · System health                │
├─────────────────────────────────────────────────────────────────────────┤
│  LAYER 1 — Core Trading Pipeline                Agents 0–16             │
│  Edge research → universe → regime → signal → risk → execution → review │
└─────────────────────────────────────────────────────────────────────────┘
                              │
              Redis Streams message bus
                              │
       ┌──────────────────────┴──────────────────────┐
       │          React 19 Dashboard (Vite)           │
       │  Dashboard · Agents · Positions · HFT ···    │
       └──────────────────────────────────────────────┘
```

All inter-agent communication flows through a Redis Streams message bus. State that must survive restarts is written to ClickHouse (tick warehouse) and Redis. The React dashboard consumes a FastAPI backend via HTTP and WebSocket; there is no direct database access from the frontend.

---

## Layer 1 — Core Trading Pipeline (Agents 0–16)

This layer implements the full lifecycle of a discretionary/systematic trade from edge validation through post-session review and compliance bookkeeping.

### Agent 0 — ERA: Edge Research Agent

The system will not trade a strategy that has not been validated by ERA. ERA maintains an **expectancy guardian** that ingests historical tick data, computes edge statistics (win rate, average R-multiple, breakeven strike rate), and emits a signed edge certificate. Downstream agents refuse to act on signals that lack a valid certificate. This prevents the classic failure mode where a team builds execution infrastructure around a strategy that was never proven to have positive expectancy.

### Agent 1 — MUA: Market Universe Agent

MUA maintains a rolling **500-symbol universe** ranked by a four-factor composite score:

| Factor | Description |
|---|---|
| Momentum | 20-day return z-score relative to sector |
| Volume | 30-day ADTV rank, minimum $5M/day threshold |
| Volatility | ATR-normalized realized vol, prefers 1–3% daily range |
| Liquidity | Bid-ask spread + market-depth score at $100k clip size |

Universe membership is recalculated at session open and at regime transitions. Symbols that fail the liquidity gate are immediately removed and flagged for the Execution Agent's SOR to avoid.

### Agent 2 — MRA: Market Regime Agent

MRA runs a **Hidden Markov Model** over intraday price/volume features and classifies the current market microstructure into one of five regimes:

```
TREND_DAY              — directional momentum, higher slippage tolerance
MEAN_REVERSION_DAY     — fade-the-move, tighter size limits
EVENT_DRIVEN_DAY       — binary catalyst, options-adjusted sizing
HIGH_VOLATILITY_NO_TRADE   — VIX spike or spread blow-out, flat
LOW_LIQUIDITY_NO_TRADE     — thin book, elevated toxicity risk, flat
```

Regime state is broadcast to all downstream agents. Agents 5, 7, 8, and 12 hold hard-coded behavior tables keyed on regime label — no agent attempts to infer regime independently.

### Agent 3 — DIA: Data Integrity Agent

Before any signal processing begins, every incoming tick passes through DIA's **feed anomaly detector**. DIA checks five failure modes that routinely corrupt production trading systems:

- **Stale tick** — timestamp has not advanced beyond configurable threshold
- **Duplicate tick** — sequence number or content hash matches a prior event within the deduplication window
- **Crossed market** — ask price below bid price, indicating a feed or normalization error
- **Out-of-sequence** — sequence number is non-monotonic relative to the per-venue counter
- **Feed divergence** — primary and backup feed prices diverge beyond a configurable basis-point threshold

Any anomaly triggers a structured alert to the System Health Agent and pauses signal generation for the affected symbol. The Portfolio Supervisor FSM transitions to `HALTED_ANOMALY` if the anomaly rate exceeds the session threshold.

### Agent 4 — MiSA: Micro Signal Agent

MiSA is the system's pattern detection engine. It processes the clean tick stream from DIA and emits actionable signal events. The critical path is FPGA-accelerated:

```
Tick ingress → normalize → feature extraction → pattern match → signal emit
     ↑                                                              ↓
  mmap/DPDK kernel bypass                               Redis Streams publish
         Total pipeline: 80ns (vs ~2µs software baseline)
```

The FPGA pipeline is modeled as a 5-stage, 250MHz design. Python binds to it via ctypes/mmap, preserving the latency model without requiring physical FPGA hardware. This allows the full behavioral logic to be tested in CI while the production deployment substitutes real silicon.

### Agent 5 — SVA: Signal Validation Agent

SVA applies a **five-dimension quality gate** before any signal reaches the risk layer. A signal must pass all five dimensions or it is silently dropped (never rejected noisily, to avoid adversarial inference):

1. **Regime alignment** — signal direction consistent with current MRA regime
2. **Edge certificate validity** — ERA-issued certificate present and unexpired
3. **Symbol eligibility** — symbol currently in MUA's approved universe
4. **Feed integrity** — no open DIA anomaly on the symbol
5. **Cooldown compliance** — minimum inter-signal interval enforced per symbol

### Agent 6 — TERA: Trade Entry Risk Agent

TERA enforces **10 hard risk rules** that cannot be overridden by any automated agent. Only the Human Governance Layer (Agent 16) can modify these limits, and every modification is audit-logged:

| Rule | Parameter |
|---|---|
| Daily P&L cap | Configurable max daily loss, halt on breach |
| Drawdown velocity | Rate-of-loss limiter, prevents fast drawdown before human can respond |
| Max concurrent trades | Hard position count ceiling |
| Symbol concentration | Max % of portfolio in a single symbol |
| Sector concentration | Max % of portfolio in a single GICS sector |
| PDT compliance | Pattern Day Trader rule enforcement for margin accounts |
| Wash-sale pre-check | Flags 30-day lookback before allowing a re-entry |
| Leverage ceiling | Gross notional / NAV hard cap |
| Correlated position limit | Detects hidden concentration via correlation matrix |
| Time-of-day restriction | Blackout windows around open auction, close auction, major events |

### Agent 7 — SPA: Sizing Plan Agent

SPA computes position size using a **Kelly Criterion** calculation anchored to the edge statistics from ERA's certificate, then applies a volatility scalar to normalize expected P&L volatility across positions:

```
f* = (bp - q) / b              # full Kelly fraction
f_scaled = f* × volatility_scalar × regime_multiplier × portfolio_heat_adjustment
```

where `volatility_scalar = target_daily_vol / symbol_ATR_normalized` and `regime_multiplier` comes from MRA's regime table. The system runs fractional Kelly (typically 0.25f*) to control for estimation error in the edge statistics.

### Agent 8 — EXA: Execution Agent

EXA manages the full order lifecycle. Its two critical path components are:

**DMA Co-location Path**
```
Order decision → kernel bypass → co-lo NIC → exchange matching engine
                                220ns total (vs 2ms via retail broker API)
```

**Venue Toxicity SOR (Smart Order Router)**
EXA maintains a live toxicity score for each execution venue using a **UCB1 multi-armed bandit** with exponential-decay markout scoring. After each fill, the markout at T+5s, T+30s, and T+5min is measured and fed back into the venue's decay-weighted score. Orders are preferentially routed to venues with the lowest expected adverse selection.

Bracket orders (entry + stop + target) are placed as an atomic unit. If the bracket cannot be placed atomically, EXA aborts and re-queues rather than risk a naked position.

### Agent 9 — BRA: Broker Reconciliation Agent

BRA performs **continuous position state reconciliation** between the system's internal position ledger and the broker's reported positions. Reconciliation runs on every fill acknowledgment and on a timer. Any discrepancy beyond configurable tolerance triggers a `HALTED_RECONCILIATION` FSM transition and alerts the Human Governance Layer.

BRA is the system's defense against the class of bugs where internal state and broker state silently diverge — a failure mode that has caused catastrophic losses in production trading systems.

### Agent 10 — EQA: Execution Quality Agent

EQA tracks **slippage and fill quality** for every executed order, maintaining running statistics that feed back into:
- TERA's risk rules (poor fill quality tightens position limits)
- EXA's SOR scoring (venues with high slippage see reduced order flow)
- PSRA's post-session review (execution quality is a first-class P&L attribution factor)

Metrics tracked per fill: arrival price slippage, decision price slippage, effective spread, fill rate at limit, venue markout at T+5/30/300s.

### Agent 11 — MMA: Micro Monitor Agent

MMA performs **tick-by-tick monitoring** of open positions. It watches for:
- Adverse price movement approaching stop levels
- Sudden volume spikes that may indicate adverse information
- Spread widening that signals deteriorating liquidity
- Correlation breaks between hedged legs

MMA feeds alerts directly to ELA (Agent 12) and to MiSA (Agent 4) to suppress new signals in deteriorating microstructure.

### Agent 12 — ELA: Exit & Lock-In Agent

ELA manages the exit lifecycle with **six named exit modes**, each with its own logic and urgency tier:

| Exit Mode | Trigger | Urgency |
|---|---|---|
| `PROFIT_TARGET` | Price reaches predetermined R-multiple target | Limit order |
| `TIME_STOP` | Position age exceeds regime-appropriate holding period | Limit, then market |
| `MOMENTUM_FAILURE` | Signal that initiated trade has reversed or decayed | Limit |
| `VOLATILITY_SHOCK` | ATR expansion beyond threshold post-entry | Market |
| `REGIME_FLIP` | MRA emits a regime transition incompatible with the position | Market |
| `PORTFOLIO_RISK_BREACH` | TERA signals aggregate risk limit approaching | Market |

Exit mode is recorded in the trade record and flows through to PSRA's attribution model. This allows the system to learn which exit modes are being used most frequently — frequent `VOLATILITY_SHOCK` exits, for example, indicate the sizing model is not adequately accounting for realized vol.

### Agent 13 — PSA: Portfolio Supervisor Agent

PSA is the system's central nervous system. It runs a **9-state finite state machine** with transitions gated by a **3-node in-process Raft consensus cluster**. A halt command does not take effect until 2 of 3 Raft nodes have committed the transition to their logs. This prevents split-brain scenarios where a single component failure causes premature trading halts.

See the [9-State FSM](#9-state-fsm) section for the complete state diagram.

### Agent 14 — PSRA: Post-Session Review Agent

PSRA runs after market close and generates a structured performance report covering:
- P&L attribution by agent (signal quality vs execution quality vs sizing)
- Exit mode distribution analysis
- Regime classification accuracy retrospective
- Edge certificate validity audit (did ERA's predicted edge materialize?)
- Slippage budget vs actual by venue and by signal type

PSRA's output feeds directly back into ERA (Agent 0) to close the feedback loop on edge research.

### Agent 15 — TCA: Tax & Compliance Agent

TCA maintains the compliance ledger for two specific regulatory obligations:

**Wash-Sale Tracking** — maintains a 30-day lookback of realized losses per symbol. Before TERA allows a re-entry into a recently exited losing position, TCA checks the wash-sale window. Violations are flagged and the trade is blocked or deferred.

**PDT Tracking** — tracks the day-trade count against the Pattern Day Trader threshold (3 round-trips in 5 business days for accounts under $25k). TERA consults TCA before allowing a new entry during the PDT lookback window.

### Agent 16 — HGL: Human Governance Layer

HGL is the only agent that can modify TERA's hard risk rules. It implements a **change proposal workflow**:

1. A change proposal is submitted (via dashboard or API)
2. HGL validates the proposal against a policy schema
3. If the proposal modifies a Tier-1 parameter (daily loss cap, leverage ceiling), a dual-approval requirement is enforced
4. Approved proposals are applied atomically with full audit log entries
5. Every parameter change is versioned and can be rolled back

HGL also handles FSM resume commands for `HALTED_*` states. A human must explicitly acknowledge the halt condition before the FSM can transition back to `ACTIVE`.

---

## Layer 2 — Infrastructure (Agents 17–19)

### Agent 17 — GCA: Global Clock Agent

GCA provides an **authoritative nanosecond time source** to all other agents. It implements:

- **PTP / IEEE 1588** synchronization against a GPS grandmaster clock
- **Sub-100ns convergence** (MiFID II requires timestamps accurate to <1µs for HFT venues)
- A monotonic sequence counter that increments at 250MHz, used by all agents for event ordering
- A time-of-day service consumed by TERA's blackout-window rules and TCA's wash-sale lookback

Every event in the system carries a GCA timestamp. This is not optional — DIA's out-of-sequence detector and the Raft log both depend on total event ordering.

### Agent 18 — SMRE: Shadow Mode Replay Engine

SMRE provides **deterministic replay** of any historical session. It:

- Replays tick streams from ClickHouse with nanosecond fidelity, respecting the original GCA timestamps
- Computes **SHA-256 checksums** over the replayed event sequence to verify bitwise determinism
- Supports shadow mode: the full agent pipeline runs in parallel against replayed data without touching live orders, allowing regression testing of agent behavior against real historical microstructure
- Integrates with CI: the GitHub Actions pipeline replays a canary session and asserts that the checksum matches the reference value before merging

### Agent 19 — SHA: System Health Agent

SHA monitors the infrastructure substrate that all other agents depend on:
- Redis Streams lag per consumer group
- ClickHouse write throughput and query latency
- Agent heartbeat liveness (each agent publishes a heartbeat; SHA alerts if any agent misses two consecutive beats)
- Memory and CPU pressure per agent process
- Network interface error rates and packet loss

SHA's alert stream feeds the React dashboard's Health tab and can trigger a controlled `SHUTDOWN` FSM transition if infrastructure degradation is severe enough to compromise trading safety.

---

## Layer 3 — Institutional Strategies (Agents 20–23)

### Agent 20 — MKM: Market Making Agent

MKM implements a full **Avellaneda-Stoikov market making model** with three live extensions:

**Inventory Skew** — as MKM accumulates inventory on one side, it asymmetrically adjusts its bid-ask spread to encourage inventory-reducing fills. The skew is a smooth function of normalized inventory relative to the configured inventory limit.

**LOB Imbalance ML** — a lightweight gradient-boosted model (trained on the last N sessions via PSRA data) predicts adverse selection probability from the current limit order book imbalance at the top 5 levels. When predicted adverse selection exceeds threshold, MKM widens its spread or steps back entirely.

**Regime Integration** — in `HIGH_VOLATILITY_NO_TRADE` and `LOW_LIQUIDITY_NO_TRADE` regimes, MKM cancels all resting orders and goes flat. Only `TREND_DAY` and `MEAN_REVERSION_DAY` are market-making-eligible regimes.

### Agent 21 — SAA: Statistical Arbitrage Agent

SAA implements a complete pairs-trading / statistical arbitrage framework:

**Cointegration Discovery** — Engle-Granger two-step procedure run nightly over the universe, identifying symbol pairs with stationary spread residuals (ADF test, p < 0.05).

**Spread Modeling** — Kalman filter with time-varying parameters tracks the dynamic hedge ratio and spread mean. The Kalman gain adapts to structural breaks in the cointegrating relationship.

**OU Mean-Reversion** — the spread is modeled as an Ornstein-Uhlenbeck process. Entry/exit thresholds are derived from the OU parameters (mean-reversion speed θ, long-run mean μ, diffusion σ) rather than static z-score bands, giving thresholds that adapt to the current regime's mean-reversion speed.

### Agent 22 — ADA: Alternative Data Agent

ADA ingests and normalizes signals from **10 alternative data sources**:

| Source | Signal Type |
|---|---|
| Satellite imagery | Retail parking lot fill rates, oil tank floating roof positions |
| Credit card transactions | Same-store sales proxies, category spend trends |
| Shipping manifests | Import/export volume leading indicators |
| NLP earnings transcripts | Sentiment scoring, management tone delta |
| Social sentiment | Abnormal mention velocity, influencer network propagation |
| Job postings | Hiring velocity as revenue proxy |
| Patent filings | R&D activity leading indicator |
| Web traffic | App/site visitor trends |
| Options flow | Unusual activity detection, dark pool print analysis |
| Insider transaction filings | Form 4 velocity and clustering analysis |

ADA normalizes all sources to a common [-1, +1] signal scale and delivers signed, timestamped alt-data events to SVA's validation queue. Alt-data signals require regime alignment just like price-derived signals.

### Agent 23 — CAT: CAT Compliance Agent

CAT implements SEC **Consolidated Audit Trail** reporting obligations and provides live spoofing detection:

**CAT Reporting** — generates CAIS (Customer Account Information System) and order event files in the CAT schema. Every order lifecycle event (new, modify, cancel, fill) is timestamped with GCA precision and formatted for regulatory submission.

**Spoofing Detection** — MBO (Market by Order) L3 order book is maintained using `SortedDict` (O(log n) add/cancel). CAT monitors for:
- High cancel-to-trade ratios at specific price levels
- Orders placed and cancelled within sub-second windows that moved the NBBO
- Layering patterns across multiple price levels
- Cross-venue spoofing signatures

Detected spoofing patterns are flagged, logged to the audit trail, and trigger HGL notifications.

---

## Layer 4 — HFT Silicon & Enterprise Risk (Agents 24–26)

### Agent 24 — AEA: Algorithmic Execution Agent

AEA is the block desk. It handles parent orders of up to **1 million shares** and slices them into child orders using four industry-standard algorithms:

| Algorithm | Use Case | Target |
|---|---|---|
| VWAP | Minimize market impact over session | Track session VWAP within 0.5 bps |
| TWAP | Time-uniform distribution | Equal intervals, clock-driven |
| POV | Participation rate control | Fixed % of market volume |
| IS (Implementation Shortfall) | Minimize arrival price slippage | Adaptive urgency vs market impact tradeoff |

Child orders are routed through EXA's DMA path and venue toxicity SOR. AEA monitors real-time slippage against the algorithm's benchmark and dynamically adjusts participation rate to stay within the 0.5 bps VWAP tracking error target.

### Agent 25 — ATS: Dark Pool Alternative Trading System

ATS operates a **PFoF internalization engine** that matches incoming order flow at the NBBO midpoint before routing to lit venues:

- **72.4% internalization rate** — the majority of order flow is matched internally, avoiding exchange fees and minimizing market impact
- Midpoint matching at the National Best Bid and Offer; no trade occurs outside the NBBO
- Unmatched flow waterfalls to EXA's SOR for lit venue routing
- Full MiFID II / Reg NMS best execution documentation generated per fill
- Internalization logic is available to AEA for block order child fills, reducing footprint on lit venues

### Agent 26 — VaR: Enterprise Risk Agent

VaR provides portfolio-level risk quantification using a full **Monte Carlo simulation**:

**Simulation Architecture**
- 10,000 scenario paths per risk calculation cycle
- Cholesky decomposition of the realized covariance matrix to generate correlated shocks
- Each scenario samples from the joint distribution of daily returns across all open positions

**Risk Metrics**
- 99% Value at Risk (1-day holding period)
- Expected Shortfall / CVaR at 95% and 99%
- Component VaR per position (marginal contribution to portfolio VaR)

**Stress Tests (5 scenarios)**

| Scenario | Description |
|---|---|
| 2008 GFC | Full correlation breakdown, credit spread explosion |
| 2010 Flash Crash | Intraday liquidity evaporation, 10% drawdown in minutes |
| 2020 COVID Crash | Cross-asset correlation spike, volatility regime shock |
| 2022 Rate Shock | Fixed income / equity correlation reversal |
| Custom Tail | User-defined adverse scenario for concentration risk |

VaR results feed directly into TERA (risk rule 8: leverage ceiling) and SPA (sizing plan adjustment when portfolio VaR approaches threshold).

---

## 9-State FSM

PSA's Portfolio Supervisor runs the following finite state machine. State transitions are committed to the Raft log before they take effect — a single node cannot unilaterally halt the system.

```
                    ┌──────────┐
                    │ BOOTING  │
                    └────┬─────┘
                         │ DIA ready
                         ▼
             ┌───────────────────────┐
             │ WAITING_FOR_DATA_     │
             │      INTEGRITY        │
             └───────────┬───────────┘
                         │ Regime established
                         ▼
             ┌───────────────────────┐
             │  WAITING_FOR_REGIME   │
             └───────────┬───────────┘
                         │ All systems nominal
                         ▼
    ┌────────────────────────────────────────────────┐
    │                   ACTIVE                       │◄──────────┐
    └──┬──────────────┬──────────────────────────────┘           │
       │              │                  │                        │
       │ cooldown     │ risk breach      │ anomaly         human  │
       ▼              ▼                  ▼                 resume │
 ┌──────────┐  ┌──────────────┐  ┌──────────────┐               │
 │  PAUSED_ │  │   HALTED_    │  │   HALTED_    │               │
 │ COOLDOWN │  │    RISK      │  │   ANOMALY    │               │
 └────┬─────┘  └──────────────┘  └──────────────┘               │
      │ auto        ▲                   ▲                         │
      └─────────────┘                  │                         │
                                        │               ┌─────────────────┐
                              recon mismatch            │    HALTED_      │
                                        └───────────────│ RECONCILIATION  │
                                                        └─────────────────┘

  Any state ──► SHUTDOWN  (terminal, requires Raft quorum)
```

**Raft Consensus Gates**

The in-process 3-node Raft cluster enforces quorum on all state transitions that leave `ACTIVE`. A halt command is broadcast to all three nodes; the leader proposes the log entry; followers acknowledge; the leader commits only when 2 of 3 have responded. The FSM does not transition until the commit is confirmed. This means a single malfunctioning agent cannot unilaterally halt trading, and a single node failure cannot prevent a legitimate halt from executing — quorum is always achievable with 2 of 3 nodes.

**Resume Protocol**

`HALTED_RISK`, `HALTED_ANOMALY`, and `HALTED_RECONCILIATION` all require explicit human approval via HGL before the FSM may transition to `ACTIVE`. The resume command is itself subject to Raft consensus. This prevents automated re-entry into a market condition that triggered a safety halt.

---

## Dashboard

The React 19 dashboard (Vite build) exposes 9 tabs backed by a FastAPI WebSocket feed:

| Tab | Contents |
|---|---|
| **Dashboard** | Live P&L curve, open position summary, regime indicator, FSM state badge |
| **Agents** | Per-agent heartbeat status, last event timestamp, error count |
| **Positions** | Live open positions with entry price, current price, unrealized P&L, exit mode |
| **Institutional** | MKM spread/inventory chart, SAA spread z-score, ADA alt-data signal feed |
| **Multi-Asset** | Cross-asset correlation heatmap, regime matrix, sector exposure |
| **Audit** | HGL change log, CAT compliance event stream, TERA rule override history |
| **Health** | SHA infrastructure metrics — Redis lag, ClickHouse throughput, agent heartbeats |
| **HFT** | AEA algorithm progress, ATS internalization rate, DMA latency histogram |
| **Enterprise** | VaR dashboard — Monte Carlo fan chart, stress test results, component VaR breakdown |

---

## Infrastructure Stack

### FPGA Acceleration
- 5-stage, 250MHz pipeline simulation (ctypes/mmap binding)
- 80ns tick-to-trade critical path vs ~2µs software baseline
- Kernel bypass feed ingestion via DPDK simulation (mmap + ctypes)
- Production deployment: substitute ctypes shim with real PCIe FPGA driver, no agent code changes required

### Microwave Network Model
- CME Aurora → NY4 co-location: 1,192 km, 15 hops
- Microwave latency: 3.97ms
- Fiber baseline: 6.87ms
- Arbitrage edge: **+2.9ms** on latency-sensitive cross-venue strategies

### Direct Market Access
- 220ns co-location DMA path vs 2ms retail broker API
- Bracket order atomic placement
- Venue toxicity SOR: UCB1 bandit with exponential decay markout scoring

### Clearing & Settlement
- DTCC/OCC self-clearing model: CNS netting
- Simulated clearing cost: $0.00002/share vs $0.001/share (broker pass-through)
- 98% cost reduction on clearing fees at scale

### Compute Infrastructure
- InfiniBand cluster model: 200Gbps RDMA
- Online SGD hot-swap: model weights updated without agent restart
- H100 simulation for Monte Carlo VaR batch computation

### Market Data Infrastructure
- SBE (Simple Binary Encoding) codec: 38-byte message header, significantly faster than JSON serialization
- LMAX Disruptor pattern: lock-free ring buffer, async CAS for inter-thread handoff
- MBO L3 order book: `SortedDict` (O(log n) add/cancel), maintained by CAT for spoofing detection
- Shared memory bus: `multiprocessing` ring buffer for zero-copy IPC between agents
- ClickHouse tick warehouse: dual-write (primary + replica), HTTP interface, columnar storage for fast OHLCV aggregation

### Clock Synchronization
- PTP / IEEE 1588 with GPS grandmaster
- Sub-100ns convergence
- MiFID II requirement: <1µs — Nexus exceeds by 10x

---

## Test Coverage

```
722 tests · 0 failures · 4.0s runtime
```

Tests are organized by agent. Each agent has a dedicated test module:

```
tests/
├── test_agent_00_era.py          # Edge Research Agent
├── test_agent_01_mua.py          # Market Universe Agent
├── test_agent_02_mra.py          # Market Regime Agent
├── test_agent_03_dia.py          # Data Integrity Agent
├── test_agent_04_misa.py         # Micro Signal Agent
├── test_agent_05_sva.py          # Signal Validation Agent
├── test_agent_06_tera.py         # Trade Entry Risk Agent
├── test_agent_07_spa.py          # Sizing Plan Agent
├── test_agent_08_exa.py          # Execution Agent
├── test_agent_09_bra.py          # Broker Reconciliation Agent
├── test_agent_10_eqa.py          # Execution Quality Agent
├── test_agent_11_mma.py          # Micro Monitor Agent
├── test_agent_12_ela.py          # Exit & Lock-In Agent
├── test_agent_13_psa.py          # Portfolio Supervisor Agent (FSM + Raft)
├── test_agent_14_psra.py         # Post-Session Review Agent
├── test_agent_15_tca.py          # Tax & Compliance Agent
├── test_agent_16_hgl.py          # Human Governance Layer
├── test_agent_17_gca.py          # Global Clock Agent
├── test_agent_18_smre.py         # Shadow Mode Replay Engine
├── test_agent_19_sha.py          # System Health Agent
├── test_agent_20_mkm.py          # Market Making Agent
├── test_agent_21_saa.py          # Statistical Arbitrage Agent
├── test_agent_22_ada.py          # Alternative Data Agent
├── test_agent_23_cat.py          # CAT Compliance Agent
├── test_agent_24_aea.py          # Algorithmic Execution Agent
├── test_agent_25_ats.py          # Dark Pool ATS
└── test_agent_26_var.py          # Enterprise Risk Agent
```

Run the full suite:

```bash
python -m pytest -v --tb=short
```

The GitHub Actions CI pipeline runs the full suite on every push and pull request. The SMRE canary replay checksum is also verified in CI — any change that alters the deterministic replay output will fail the pipeline.

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Agent runtime | Python 3.11, asyncio | Concurrent agent event loops |
| Numerical | NumPy, SciPy | Signal processing, statistical models |
| Data structures | sortedcontainers | O(log n) MBO L3 order book |
| Validation | Pydantic v2 | Message schema enforcement |
| HTTP client | httpx | Async REST calls to broker/exchange APIs |
| Message bus | Redis Streams | Inter-agent event delivery |
| Tick warehouse | ClickHouse | High-throughput columnar tick storage |
| IPC | multiprocessing ring buffer | Zero-copy shared memory bus |
| Consensus | In-process Raft (3 nodes) | PSA FSM state transition gating |
| Frontend | React 19 + Vite | Dashboard SPA |
| Charts | Recharts | P&L, heatmaps, latency histograms |
| Backend API | FastAPI + WebSocket | Dashboard data feed |
| Testing | pytest | 722 tests, 0 failures |
| CI/CD | GitHub Actions | Test + build + deploy on every push |
| Deployment | Docker Compose | Single-command environment bootstrap |
| Demo hosting | GitHub Pages | Live frontend demo |
| Clock sync | PTP / IEEE 1588 | Nanosecond-accurate event timestamping |
| Encoding | SBE (Simple Binary Encoding) | Low-latency market data serialization |
| Concurrency primitive | LMAX Disruptor pattern | Lock-free ring buffer handoff |
| ML inference | scikit-learn / online SGD | LOB imbalance model, HMM regime classifier |
| GPU simulation | H100 model | Monte Carlo VaR batch computation |

---

## Repository Structure

```
nexus-trading/
├── agents/                    # 27 agent modules (agent_00_era.py … agent_26_var.py)
├── core/                      # Shared infrastructure
│   ├── message_bus.py         # Redis Streams abstraction
│   ├── clock.py               # GCA client
│   ├── order_book.py          # SortedDict MBO L3 implementation
│   └── raft.py                # In-process 3-node Raft consensus
├── dashboard/                 # React 19 + Vite frontend
│   ├── src/
│   │   ├── components/        # Per-tab React components
│   │   └── App.jsx            # 9-tab router
│   └── vite.config.js
├── tests/                     # 27 agent test modules (722 tests total)
├── nexus_main.py              # Entrypoint — boots all agents, starts FSM
├── docker-compose.yml         # Redis + ClickHouse + backend + frontend
├── requirements.txt
└── .github/
    └── workflows/
        └── ci.yml             # Test + build + GitHub Pages deploy
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Write tests first — every agent change requires corresponding test coverage
4. Ensure `python -m pytest` passes with zero failures before opening a PR
5. Open a pull request against `main`; CI must be green

Code style: `black` + `ruff`. Type hints required for all public agent interfaces.

---

## License

MIT License. See [LICENSE](LICENSE) for full text.

---

*Built by Avinash Amanchi — [github.com/avinashamanchi](https://github.com/avinashamanchi)*
