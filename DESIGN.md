# Nexus System Design — Why 27 Agents, Raft Consensus, and Everything Else

*Written April 2026, after the system has been running in shadow mode for several months.*

This document is both a design record and an honest post-mortem. The architecture has opinions, and those opinions have costs. I'll explain both.

---

## Table of Contents

1. The Core Question: Why Not a Monolith?
2. Why Redis Streams and Not Kafka/RabbitMQ?
3. Why Raft Consensus for PSA Halt Transitions?
4. Why Separate TERA and SPA?
5. Why 10 Hard Risk Rules in TERA (Not ML)?
6. The 9-State FSM — Why So Many States?
7. What I Got Wrong in v1 and Fixed in v2
8. The HFT Layer — A Clarification
9. What's Next

---

## 1. The Core Question: Why Not a Monolith?

The honest answer to "why 27 agents?" is that a single-process trading bot would be simpler to build, simpler to run, and simpler to debug. For a $30k paper account, a well-structured monolith would probably win on every practical metric. I want to be clear about that upfront because every architectural decision in this system carries a real cost, and pretending those costs don't exist would make this document useless.

So why 27 agents?

**Each agent owns exactly one invariant.** TERA is the risk agent. It knows one thing: whether a proposed trade violates a hard risk rule. It has no opinion about position sizing, market regime, or order routing. This means a bug in the Kelly formula (which lives in SPA) cannot corrupt the risk check. The invariant is enforced by the architecture, not by code discipline. When TERA passes its 40 unit tests, you have a meaningful guarantee — not a hope that nobody touched the wrong variable.

**Independent testability.** The test suite has 722 tests (as of the last run). Each test hits one component in isolation. When a test fails, you know exactly which agent's invariant broke. With a monolith, a failing test tells you "something in the trading engine is wrong." With this architecture, a failing test tells you "TERA's drawdown velocity rule returned the wrong RiskRejectReason for this input." The diagnostic signal is an order of magnitude cleaner.

**Kill-switch isolation.** If SMRE (the state machine and risk engine supervisor) detects a reconciliation mismatch and halts, EXA (execution agent) stops receiving orders. The market data pipeline — DIA, Level2Feed, MBOOrderBook — keeps running. Logs keep flowing. The dashboard stays up. Nothing else cascades. In a monolith, halting the risk module means pausing the thread that also handles data ingestion, which means the next resume cycle starts with a stale book. That's a subtle bug that's hard to reproduce in testing.

**Mental model clarity.** Each agent has a one-sentence job description. GCA: "I am the global clock. Every timestamp in the system comes from me." HMM: "I am the regime classifier. I consume price history and emit a market regime." This clarity compounds over time. Six months after writing an agent, you can read its job description and immediately know what it should and shouldn't do.

**The real costs.** Message serialization overhead is real — every inter-agent call that would be a function call in a monolith is now a Redis round-trip with JSON serialization. On localhost that's roughly 0.5-2ms per hop, which is irrelevant for end-of-day strategy but would matter for a sub-second execution strategy. Debugging is genuinely harder: a bug that spans two agents requires tracing messages across the bus, which is a different skill than stepping through a call stack. The operational complexity of 27 processes is higher than one.

**The honest architectural calculus.** This system is designed for the institutional case — $30M+ AUM, multiple co-location sites, separate risk and portfolio teams, regulatory audit requirements, and operational SLAs that require components to fail independently. At that scale, the operational properties that feel like over-engineering at $30k become table stakes. The architecture is a bet that the system will grow into it. Whether that bet pays off is a question this document can't answer.

---

## 2. Why Redis Streams and Not Kafka/RabbitMQ?

The message bus is the circulatory system of the architecture. Getting it wrong is expensive to fix later, so it's worth explaining the choice carefully.

**What Redis Streams give us.** Redis Streams have consumer groups, which means multiple agents can consume from the same topic and Redis handles message assignment and acknowledgment. At-least-once delivery is guaranteed — if an agent crashes before acknowledging, Redis will redeliver to another consumer in the group. Replay is built in: `XRANGE` lets you re-read any historical window of messages, which is critical for the SMRE replay subsystem. Pub/sub latency on localhost is sub-millisecond.

**Why not Kafka?** Kafka is the right answer for production systems with durability requirements, high-throughput pipelines, and multiple consumer services with different consumption rates. At the scale Nexus operates today, Kafka would be correct architecture and unnecessary complexity simultaneously. The operational overhead of running a Kafka broker (ZooKeeper or KRaft mode, partition rebalancing, consumer group lag monitoring) is real. For a system where all components run on one or two machines, Redis delivers 95% of the Kafka value proposition with a fraction of the operational burden. The interface is designed so Kafka is a drop-in replacement — the bus abstraction layer (`core/bus.py`) exposes `publish(BusMessage)` and `subscribe(topic) → Queue`. Swapping the backend is a one-file change.

**Why not RabbitMQ?** RabbitMQ's AMQP model is richer than what this system needs. The main differentiator would be routing flexibility (topic exchanges, fanout, headers). Redis Streams cover the patterns this system actually uses: one-to-many broadcast, consumer group assignment, and replay. RabbitMQ's replay story is also weaker — messages are consumed and gone by default; dead letter queues are an approximation of what Redis Streams provide natively.

**The InMemoryBus.** One of the best design decisions in the system: the in-memory bus implementation that swaps in without changing any agent code. Every agent calls `bus.publish(msg)` and `bus.subscribe(topic)`. In production that's a RedisStream. In testing it's a dict of asyncio queues. This means the test suite never touches Redis, which makes tests fast (no network), deterministic (no race conditions from Redis reconnects), and runnable in CI with no infrastructure dependencies. The discipline of maintaining the interface contract paid back the cost of writing the InMemoryBus within the first week of testing.

**What we'd change at institutional scale.** Kafka, partitioned by symbol. Each symbol gets its own partition, which gives linear scalability and preserves per-symbol ordering guarantees. Consumer groups map to agent pools. The offset-based replay mechanism is more robust than Redis Streams' ID-based approach for very long retention windows. The Kafka migration path is intentionally kept clear by the bus abstraction.

---

## 3. Why Raft Consensus for PSA Halt Transitions?

This is the architectural decision that most often prompts "isn't that overkill?" The answer requires understanding what a halt actually is.

**What a halt means.** A halt is a state transition from ACTIVE to one of three HALTED states. Once halted, no new orders are submitted. Existing orders may be canceled depending on halt type. The system requires human sign-off to resume. In a live trading context, a halt is the most consequential state mutation the system can make. Getting it wrong in either direction is expensive: a false halt misses trades; a missed halt continues trading through a risk breach.

**The single-node failure problem.** Without consensus, the sequence is: PSA computes halt decision → writes HALTED_RISK to shared state → broadcasts halt message to all agents. If PSA crashes after the write but before the broadcast, the shared state says HALTED but agents haven't received the message. When PSA restarts, it reads HALTED and doesn't re-broadcast (it thinks the broadcast already happened). Result: the system is in an inconsistent state where the risk engine believes it's halted but the execution agent is still processing orders.

This is not a theoretical concern. Process crashes at the worst moment are a standard failure mode in distributed systems, and "the worst moment" in Murphy's law terms is always "during a state transition."

**What Raft guarantees.** A halt decision committed to a Raft cluster of 3 nodes is durable as long as at least 2 nodes are alive. The commit itself — the moment after which the halt is considered effective — happens only after a quorum acknowledges the log entry. There is no window where the state is written but unacknowledged. Linearizable reads mean any node can answer "is the system halted?" and the answer will be consistent with the committed state.

**The real deployment topology.** Two co-location sites: NY4 (Equinix, primary) and NY5 (Equinix, secondary), plus a cloud node for quorum. A halt proposed at NY4 requires NY5 or the cloud node to acknowledge before it takes effect. This means a single data center failure — the NY4 rack loses power — does not leave the halt state ambiguous. NY5 has the committed state and can continue (or remain halted, correctly).

**Why not a database transaction?** A database transaction with SERIALIZABLE isolation would prevent concurrent writes, but it introduces a central coordinator. If the database is the coordinator and it fails, the system blocks until the database recovers. Raft gives us a distributed coordinator with no single point of failure. Linearizable reads don't require a round-trip to a central node; any node in the cluster can serve them. The fault tolerance model is strictly better.

**The honest cost.** Three-node in-process Raft for a $30k paper account is overkill. The risk of a halt state inconsistency at this scale, with a single machine, is low and the consequence is recoverable manually. The Raft implementation exists because (a) the institutional case genuinely needs it, and (b) implementing Raft correctly at this scale means the upgrade to a real multi-node deployment is a configuration change, not an architecture change.

---

## 4. Why Separate TERA and SPA?

This is the decision I'm most confident about, because the separation maps directly to a real-world organizational structure that exists for good reasons.

**TERA's job.** Binary approve/reject. TERA receives a proposed trade (symbol, direction, notional size, current portfolio state) and returns either `RiskDecision.APPROVED` or `RiskDecision.REJECTED(reason)`. TERA knows nothing about how the trade was sized. It doesn't know what Kelly fraction was used. It doesn't care whether the sizing was aggressive or conservative. It only asks: does this trade violate a hard rule?

**SPA's job.** Given an approved trade signal, compute the position size. Kelly criterion applied to the strategy's historical win rate and payoff ratio, scaled by current portfolio volatility. The output is a share count or notional allocation. SPA knows nothing about whether the trade is allowed. It assumes that if a signal reaches it, the risk check has already passed.

**The interface.** TERA emits `RiskDecision.APPROVED` on the risk decision topic. SPA subscribes to that topic and computes size when it receives an approval. The two agents never share state. SPA cannot read TERA's rule evaluation cache. TERA cannot see SPA's Kelly fraction.

**Why separation enforces the invariant.** If TERA and SPA were a single agent, a bug in the Kelly formula could interact with the risk rule evaluation in non-obvious ways. Consider: a Kelly fraction computation that produces a negative number due to a bad win-rate estimate. In a merged agent, that negative might propagate into a position size check that confuses the risk rule about whether the trade is within notional limits. In the separated design, the Kelly computation happens after the risk check has already returned APPROVED. The risk check operates on a proposed notional (computed before sizing), and the actual size is computed independently.

**The real-world parallel.** At a hedge fund, risk management and portfolio management are separate teams with separate mandates. Risk management's job is to prevent blow-ups; portfolio management's job is to maximize risk-adjusted returns within the limits risk sets. They have different incentives, different metrics, and different reporting lines. That organizational separation exists because the invariant — risk rules cannot be accidentally bypassed by a sizing decision — is important enough to enforce structurally. The agent separation is the code-level analog.

---

## 5. Why 10 Hard Risk Rules in TERA (Not ML)?

The choice to implement risk controls as hard-coded rules rather than ML models is a deliberate position that needs defending, because ML risk models do exist in production systems and are not obviously wrong.

**The case against ML for risk gating.** ML models learn from data. The data available for training a risk model is historical market data, which means the model learns what risk looked like in past regimes. The failure modes that actually blow up trading accounts tend to be novel: the 2010 Flash Crash, the 2020 COVID collapse, the 2022 rate shock. These events were not well-represented in the training data of any ML model that existed before they happened. A model trained on 2010-2020 data would have no meaningful signal about the regime shift that began in 2022.

Hard rules, by contrast, don't overfit. A rule that says "halt if daily drawdown exceeds 5%" will fire correctly in every market regime because it's a function of observed loss, not predicted loss.

**Auditability.** SEC and FINRA require explainable risk controls for algorithmic trading systems. "The neural network said so" is not an acceptable answer to an auditor asking why a risk control did or did not fire. Each of the 10 TERA rules can be described in plain English, traced to a specific line of code, and tested with a specific input/output pair. That auditability is a regulatory requirement, not an aesthetic preference.

**What the 10 rules cover.** The rules were selected by looking at the failure modes that actually blow up retail and institutional accounts:

- Daily drawdown limit — prevents a bad morning from becoming a bad month
- Drawdown velocity — catches crash dynamics before the drawdown limit is hit (the 2008 failure mode)
- Maximum consecutive losses — prevents a broken strategy from continuing to trade into the hole
- PDT (Pattern Day Trader) rule — regulatory compliance, non-negotiable
- Maximum position concentration — prevents a single name from becoming too large a fraction of the portfolio
- Correlation-adjusted exposure — prevents "diversified" positions that all move together
- Overnight position limits — risk management for gap exposure
- Wash-sale detection — tax compliance
- Market hours check — no trading outside regular hours without explicit override
- Circuit breaker — halt if the market itself is in circuit-breaker territory

Each rule is a method on TERA that takes a `TradeProposal` and returns `RiskRejectReason | None`. Adding a new rule is adding a new method and a new test. No other code changes.

**Where ML does belong.** Two places in this system use ML correctly:

- LOB imbalance prediction (Agent 20, the microstructure agent): predicting short-term price direction from order book features. This is a regime-agnostic, stationary signal with a well-defined label.
- Regime classification (HMM in Agent 2, MRA): classifying market regime as trending/mean-reverting/volatile/crisis. The HMM is trained on features that are robust to distribution shift (volatility, autocorrelation, tail statistics), not on price levels.

In both cases, a wrong ML prediction leads to a missed trade, not an uncontrolled loss. The ML output is an input to a position-taking decision, not a gate on a risk control. That's the right place for ML in this architecture.

---

## 6. The 9-State FSM — Why So Many States?

The system FSM has 9 states:

```
BOOTING → WAITING_FOR_DATA_INTEGRITY → WAITING_FOR_REGIME → ACTIVE
                                                              ↓
                                          PAUSED_COOLDOWN ←——┤
                                          HALTED_RISK    ←——┤
                                          HALTED_ANOMALY ←——┤
                                          HALTED_RECONCILIATION ←——┘
                                          SHUTDOWN (terminal)
```

Nine states might seem like gold-plating. Here's why each one exists.

**BOOTING.** The system is initializing. Agents are starting up and performing their dependency injection checks. No market data has been validated. No trading. This state exists because there is a real window between "the process started" and "the system is ready to trade" that is dangerous to collapse. A system that begins trading before DIA has validated the data feed is a system that can trade on stale or corrupted prices.

**WAITING_FOR_DATA_INTEGRITY.** DIA (Data Integrity Agent) performs checks on the incoming market data feed: timestamp coherence, spread sanity, sequence number gaps, volume anomalies. Until DIA clears the feed, no trading. This is a distinct state from BOOTING because the system is fully initialized — it's just waiting for a specific external precondition to be satisfied. The transition is automatic when DIA emits `DataIntegrityStatus.CLEAN`.

**WAITING_FOR_REGIME.** MRA (Market Regime Agent) needs a window of data to fit the HMM and classify the current regime. Until MRA has a confident regime classification, no trading. This is distinct from WAITING_FOR_DATA_INTEGRITY because the system could have clean data but an unclassified regime, or a classified regime but a data integrity issue. Combining them into a single "WAITING" state would hide which precondition is blocking.

**ACTIVE.** Normal operations. All agents running, orders being generated and executed.

**PAUSED_COOLDOWN.** Triggered by 3 consecutive losses. The system pauses for a configurable cooldown period and then auto-resumes. This is not a halt — it doesn't require human sign-off. It exists because 3 consecutive losses in short succession is a pattern that often precedes a regime shift that the strategy is not suited for. Pausing for 15-30 minutes and re-checking the regime classification catches many of these cases without requiring a human in the loop.

**HALTED_RISK, HALTED_ANOMALY, HALTED_RECONCILIATION.** Three separate halt states because the recovery procedure for each is different.

- HALTED_RISK: a risk rule fired. Recovery requires a human to review the risk breach, assess whether the condition that triggered it has resolved, and explicitly approve resumption. The relevant artifacts are the risk rule that fired, the trade that triggered it, and the current portfolio state.
- HALTED_ANOMALY: DIA detected a data anomaly (sequence gap, price spike, latency spike). Recovery requires a human to investigate the data feed, confirm the feed is healthy, and explicitly approve resumption. The relevant artifacts are the anomaly report from DIA and the current state of the market data connection.
- HALTED_RECONCILIATION: the system's internal position record disagrees with the broker's position record by more than the tolerance threshold. Recovery requires a human to reconcile the two records, identify the source of the discrepancy (missed fill, partial fill, fat-finger), and explicitly approve resumption. The relevant artifacts are the internal position record, the broker position record, and the reconciliation diff.

If all three were a single HALTED state, the human receiving the alert would need to investigate all three possible causes before knowing which recovery procedure to follow. The state itself carries information. Halting correctly is not enough — the halt needs to tell you why.

**SHUTDOWN.** Terminal state. No recovery. The process is shutting down cleanly. This exists as a distinct state (rather than just calling `sys.exit()`) because there are cleanup actions required: cancel open orders, flush logs, write final state to disk, notify monitoring. A SHUTDOWN state gives the FSM machinery a defined exit point with associated actions.

---

## 7. What I Got Wrong in v1 and Fixed in v2

This section exists because honest post-mortems are more useful than design documents that pretend the first version was correct.

**Mistake 1: Single-agent monolith.**

v1 was a single Python file that grew to ~2,000 lines over three months. It worked for paper trading. It fell apart the first time I tried to backtest with a different regime classifier — changing the regime logic required touching the risk module, because they shared state through module-level globals. The fix was not "be more disciplined." The fix was strict agent boundaries, where each agent owns its state and exposes it only through typed messages. v2 was a rewrite. The rewrite took three weeks and was worth every hour.

**Mistake 2: Direct broker calls in agents.**

v1 agents held `alpaca.TradingClient` objects directly. Testing required either a live Alpaca paper account or mocking the entire Alpaca SDK — hundreds of methods, complex return types, rate limiting behavior. I spent more time maintaining mocks than writing tests. v2 introduced `brokers/prime_broker.py` as a thin interface layer. Agents call `broker.submit_order(order)`. In production that calls Alpaca. In testing it calls a `MockBroker` that's 50 lines of Python. The mock is authoritative because it implements the same interface. Tests went from brittle to reliable overnight.

**Mistake 3: Timestamps from `datetime.now()`.**

Every agent in v1 generated its own timestamps with `datetime.now()`. This made tests non-deterministic: the ordering of events in a test run depended on actual wall-clock time, which meant tests that passed locally would fail in CI under load. v2 routes all timestamps through GCA (Agent 17, Global Clock Agent). Every event timestamp is `await gca.now()`. In tests, GCA is replaced with a mock that advances time deterministically. Tests that involve time-dependent logic — momentum calculations, cooldown expiry, RSI windows — can be written as unit tests with precise, reproducible timelines.

**Mistake 4: Risk rules as a giant if-elif chain.**

v1's risk check was a 150-line function:

```python
def check_risk(proposal):
    if proposal.notional > MAX_POSITION:
        return RiskRejectReason.POSITION_LIMIT
    elif portfolio.drawdown > MAX_DRAWDOWN:
        return RiskRejectReason.DRAWDOWN_LIMIT
    elif consecutive_losses >= MAX_CONSECUTIVE:
        ...
```

Adding a rule meant editing this function in multiple places (the check, the reason enum, the tests). v2 makes each rule a method on TERA:

```python
class TERA:
    def _check_position_limit(self, p: TradeProposal) -> RiskRejectReason | None: ...
    def _check_drawdown_limit(self, p: TradeProposal) -> RiskRejectReason | None: ...
    def _check_consecutive_losses(self, p: TradeProposal) -> RiskRejectReason | None: ...

    def evaluate(self, proposal: TradeProposal) -> RiskDecision:
        for rule in self._rules:
            if reason := rule(proposal):
                return RiskDecision.rejected(reason)
        return RiskDecision.APPROVED
```

Adding a rule is adding a method and a test. The `_rules` list is populated in `__init__`. No other code changes.

**Mistake 5: Order book as a sorted Python list.**

v1's order book was a list sorted by price, with `bisect` for insertion and O(n) scans for best bid/ask. For L2 data (10-level book) this was fine. When I added MBO (Market by Order) support for the L3 book, with thousands of orders at hundreds of price levels, the O(n) scans became the bottleneck. v2 uses `SortedDict` from `sortedcontainers`, giving O(log n) insertion and O(1) best bid/ask access. The L3 book now handles realistic MBO traffic without burning CPU.

**Mistake 6: No replay.**

v1 had no event replay. When a bug appeared in production (paper trading), reproducing it meant either (a) waiting for the same market conditions to recur, which could take weeks, or (b) manually constructing a test case from logs, which was error-prone. v2's SMRE records every event to a structured log with SHA-256 checksums. Any past system state is reproducible by replaying the event log from any checkpoint. The first time I used this to reproduce a reconciliation bug that had been intermittent for two weeks, the three hours I'd spent building the replay infrastructure paid for themselves.

---

## 8. The HFT Layer — A Clarification

The system includes models of FPGA processing, microwave network latency, DMA order routing, kernel bypass networking, and IEEE 1588 PTP clock synchronization. These components exist in the codebase and are tested. They are educational models, and that distinction matters.

**What they are.**

The models are physically accurate. The microwave propagation model uses the Friis free-space path loss equation with real CME-to-NY4 distances (roughly 1,200 km) and real atmospheric absorption coefficients. The result — approximately 4.1ms one-way latency vs. 6.2ms for fiber — matches published figures from firms that operate microwave networks. The SBE (Simple Binary Encoding) codec implements the actual FIX/SBE specification used by CME Group. The LMAX Disruptor pattern is implemented correctly, with the ring buffer, sequence barrier, and wait strategy components that the original LMAX implementation uses.

The value of building these models is not that they run at hardware speed in Python. The value is that building them required understanding the actual physics, protocols, and engineering constraints at a level that reading blog posts about HFT does not provide. The model forces you to answer questions like: what is the actual one-way latency from Aurora, IL to Mahwah, NJ over microwave? (Answer: approximately 4.1ms, limited by the speed of light over that distance.) What is the SBE binary encoding for a CME MDP 3.0 market data message? (Answer: defined in the CME Group MDP 3.0 specification, implementable from the schema.) You can't fake those answers in a simulation.

**What they are not.**

These components do not produce sub-microsecond latency. Alpaca's REST API has 50-100ms round-trip latency. Python's GIL prevents true kernel-bypass performance. The FPGA simulation runs on a CPU. There is no production claim here — the claim is that the models are correct representations of the real systems, implemented in Python as learning artifacts.

Any system documentation or commentary that reads as claiming production HFT performance is inaccurate and should be corrected.

---

## 9. What's Next

The system is currently in shadow mode — generating signals, simulating execution, tracking P&L, but not submitting live orders. The next milestones:

**1. Live performance metrics on the dashboard.**

Running Sharpe ratio and drawdown against the shadow P&L, updated in real time. Currently the dashboard shows positions and signals; adding the risk-adjusted return metrics closes the feedback loop that makes shadow mode useful for strategy evaluation.

**2. Real EOD data from yfinance (shipping with this version).**

The nightly pipeline in `data/market_data.py` downloads 60 days of end-of-day data for a 25-symbol universe, computes momentum, realized volatility, and RSI, and writes scored signals to `db/daily_signals.json`. No API key required. Runs nightly at 18:00 ET via cron or `python -m data.market_data`. The dashboard reads from this file to show "today's signals" in shadow mode.

**3. Multi-strategy capital allocation.**

Currently Kelly criterion is applied per-trade. The correct institutional approach applies Kelly across strategies simultaneously — treating each strategy as an asset with its own expected return and covariance structure. This requires SPA to maintain a portfolio-level Kelly optimizer, not a per-signal one.

**4. Options analytics.**

Agent 20 (microstructure) is the natural extension point for options. Delta hedging requires real-time Greek calculation, which in turn requires an options pricing model (Black-Scholes for vanilla, Heston for vol surface). The data layer already handles the underlying; the options layer adds the derivative.

**5. FIX protocol implementation.**

Replacing Alpaca HTTP with FIX 4.4 is both a performance improvement (binary protocol, persistent connection, lower latency) and an institutional alignment — virtually every prime broker and exchange supports FIX. The interface abstraction in `brokers/prime_broker.py` means this is a new broker implementation, not an architecture change.

---

## Appendix: Agent Reference

| # | Name | Role | Key Invariant |
|---|------|------|---------------|
| 1 | DIA | Data Integrity Agent | Feed is clean before any downstream agent sees it |
| 2 | MRA | Market Regime Agent | Regime classification precedes signal generation |
| 3 | SVA | Signal Validation Agent | Signals are well-formed before risk evaluation |
| 4 | TERA | Trade Execution Risk Agent | No order is submitted without passing all 10 hard rules |
| 5 | SPA | Sizing and Portfolio Agent | Position size is Kelly-optimal given approved signal |
| 6 | EXA | Execution Agent | Orders are routed to broker correctly and confirmed |
| 7 | SMRE | State Machine and Risk Engine | System state is consistent at all times |
| 8 | SHA | Surveillance and Halt Agent | Anomalies trigger halt before they cause losses |
| 9 | PSA | Primary State Agent | Halt transitions are Raft-committed |
| 10 | GCA | Global Clock Agent | All timestamps are consistent and reproducible |
| 11 | RECON | Reconciliation Agent | Internal positions match broker positions |
| 12 | LOGX | Log Export Agent | All events are durably recorded with checksums |
| 13 | DASH | Dashboard Agent | System state is observable without affecting it |
| 14 | HMM | Hidden Markov Model Agent | Regime model is current and confident |
| 15 | LOB | Limit Order Book Agent | L2 book is always consistent with feed |
| 16 | MBO | Market by Order Agent | L3 book is consistent with MBO feed |
| 17 | FPGA | FPGA Simulation Agent | Hardware latency model is physically accurate |
| 18 | MWAVE | Microwave Network Agent | Propagation model matches real-world physics |
| 19 | DMA | Direct Market Access Agent | Order routing path is modeled correctly |
| 20 | MICRO | Microstructure Agent | LOB imbalance signals are statistically valid |
| 21 | KBYP | Kernel Bypass Agent | Network stack model is architecture-accurate |
| 22 | PTP | Precision Time Protocol Agent | Clock synchronization model is IEEE 1588 compliant |
| 23 | SBE | Simple Binary Encoding Agent | Wire format matches CME MDP 3.0 specification |
| 24 | DISRPT | Disruptor Agent | Ring buffer model matches LMAX implementation |
| 25 | SHMA | Shared Memory Agent | IPC model is correct for the OS memory model |
| 26 | ARBX | Arbitrage Agent | Spread calculations are correct across venues |
| 27 | PERF | Performance Monitoring Agent | Latency metrics are accurate and non-intrusive |

---

*Last updated: April 2026*
*Test suite: 722 passing*
*Shadow mode: active*
