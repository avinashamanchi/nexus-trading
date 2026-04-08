// Mock real-time data — replaces WebSocket feed in demo mode
// In production: connect to ws://localhost:8080/ws

export const AGENTS = [
  // ── Trading Pipeline (Agents 0–16) ───────────────────────────────────────
  { id:0,  name:"Edge Research",     abbr:"ERA",  color:"#0A84FF", desc:"Setup expectancy guardian",       tier:"pipeline" },
  { id:1,  name:"Market Universe",   abbr:"MUA",  color:"#30D158", desc:"Symbol filtering & ranking",      tier:"pipeline" },
  { id:2,  name:"Market Regime",     abbr:"MRA",  color:"#BF5AF2", desc:"Regime classification gate",      tier:"pipeline" },
  { id:3,  name:"Data Integrity",    abbr:"DIA",  color:"#FF9F0A", desc:"Feed anomaly detection",          tier:"pipeline" },
  { id:4,  name:"Micro Signal",      abbr:"MiSA", color:"#64D2FF", desc:"Pattern detection engine",        tier:"pipeline" },
  { id:5,  name:"Signal Validation", abbr:"SVA",  color:"#0A84FF", desc:"5-dimension quality gate",        tier:"pipeline" },
  { id:6,  name:"TERA",              abbr:"TERA", color:"#FF453A", desc:"10 hard risk rules",              tier:"pipeline" },
  { id:7,  name:"Sizing Plan",       abbr:"SPA",  color:"#32D74B", desc:"Fully costed trade plans",        tier:"pipeline" },
  { id:8,  name:"Execution",         abbr:"EXA",  color:"#FFD60A", desc:"Bracket order placement",         tier:"pipeline" },
  { id:9,  name:"Broker Recon",      abbr:"BRA",  color:"#FF9F0A", desc:"State reconciliation",            tier:"pipeline" },
  { id:10, name:"Exec Quality",      abbr:"EQA",  color:"#BF5AF2", desc:"Slippage & fill tracking",        tier:"pipeline" },
  { id:11, name:"Micro Monitor",     abbr:"MMA",  color:"#64D2FF", desc:"Tick-by-tick monitoring",         tier:"pipeline" },
  { id:12, name:"Exit & Lock-In",    abbr:"ELA",  color:"#32D74B", desc:"6 named exit modes",              tier:"pipeline" },
  { id:13, name:"Portfolio Sup.",    abbr:"PSA",  color:"#0A84FF", desc:"Circuit breakers & halt",         tier:"pipeline" },
  { id:14, name:"Post-Session",      abbr:"PSRA", color:"#FF9F0A", desc:"Performance analysis",            tier:"pipeline" },
  { id:15, name:"Tax & Compliance",  abbr:"TCA",  color:"#BF5AF2", desc:"Wash-sale & PDT tracking",        tier:"pipeline" },
  { id:16, name:"Human Gov.",        abbr:"HGL",  color:"#FFD60A", desc:"Human approval layer",            tier:"pipeline" },
  // ── Infrastructure (Agents 17–19) ────────────────────────────────────────
  { id:17, name:"Global Clock",      abbr:"GCA",  color:"#32D74B", desc:"Authoritative time source",       tier:"infra" },
  { id:18, name:"Shadow Replay",     abbr:"SMRE", color:"#64D2FF", desc:"Deterministic replay engine",     tier:"infra" },
  { id:19, name:"System Health",     abbr:"SHA",  color:"#FF9F0A", desc:"Infrastructure monitoring",       tier:"infra" },
  // ── Institutional (Agents 20–23) ─────────────────────────────────────────
  { id:20, name:"Market Making",     abbr:"MKM",  color:"#BF5AF2", desc:"Avellaneda-Stoikov A/S quoting",  tier:"institutional" },
  { id:21, name:"Stat Arb",          abbr:"SAA",  color:"#0A84FF", desc:"Cointegration pair trading",      tier:"institutional" },
  { id:22, name:"Alt Data",          abbr:"ADA",  color:"#FF9F0A", desc:"Satellite · NLP · sentiment",     tier:"institutional" },
  { id:23, name:"CAT Compliance",    abbr:"CAT",  color:"#FF453A", desc:"SEC CAT + spoofing detection",    tier:"institutional" },
  // ── Enterprise (Agents 24–26) ─────────────────────────────────────────────
  { id:24, name:"Algo Execution",    abbr:"AEA",  color:"#64D2FF", desc:"VWAP/TWAP/POV/IS block desk",     tier:"enterprise" },
  { id:25, name:"Dark Pool ATS",     abbr:"ATS",  color:"#32D74B", desc:"PFoF internalization engine",     tier:"enterprise" },
  { id:26, name:"Enterprise Risk",   abbr:"VaR",  color:"#FF453A", desc:"Monte Carlo VaR · stress tests",  tier:"enterprise" },
];

export function makeLivePnl() {
  const pts = [];
  let v = 0;
  for (let i = 0; i < 60; i++) {
    v += (Math.random() - 0.42) * 35;
    pts.push({ t: i, v: Math.round(v) });
  }
  return pts;
}

export function makePipeline() {
  return [
    { step:"Universe",   status:"active", signals:12, icon:"🌐" },
    { step:"Regime",     status:"active", signals:1,  icon:"📊" },
    { step:"Data Check", status:"active", signals:1,  icon:"✅" },
    { step:"Signals",    status:"active", signals:3,  icon:"⚡" },
    { step:"Validate",   status:"active", signals:2,  icon:"🔬" },
    { step:"Risk Gate",  status:"active", signals:2,  icon:"🛡️" },
    { step:"Size Plan",  status:"active", signals:1,  icon:"📐" },
    { step:"Execute",    status:"active", signals:1,  icon:"⚙️" },
  ];
}

export function makePositions() {
  return [
    { symbol:"NVDA", direction:"LONG",  shares:85,  entry:847.20, current:852.40, pnl:+442.0, stop:842.00, target:862.00, time:"04:32", setup:"BREAKOUT" },
    { symbol:"TSLA", direction:"LONG",  shares:50,  entry:248.50, current:251.80, pnl:+165.0, stop:245.00, target:256.00, time:"02:14", setup:"VWAP_TAP" },
    { symbol:"AAPL", direction:"SHORT", shares:100, entry:218.90, current:217.50, pnl:+140.0, stop:221.00, target:215.00, time:"01:07", setup:"MOMENTUM_BURST" },
  ];
}

export function makeMMPositions() {
  const symbols = ["AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOG","AMD","INTC","SPY"];
  return symbols.map(sym => {
    const inv = Math.round((Math.random() - 0.5) * 1600);
    const mid = 100 + Math.random() * 900;
    const halfSpread = (mid * 0.0004 + 0.02);
    const pnl = (Math.random() - 0.35) * 800;
    return {
      symbol: sym,
      inventory: inv,
      bid: +(mid - halfSpread).toFixed(2),
      ask: +(mid + halfSpread).toFixed(2),
      spread_bps: +(halfSpread / mid * 20000).toFixed(1),
      pnl: +pnl.toFixed(2),
      fills: Math.floor(Math.random() * 80),
      action: inv > 600 ? "SKEW_BID" : inv < -600 ? "SKEW_ASK" : "NEUTRAL",
      inv_var: +(Math.abs(inv) * mid * 0.0002 * 2.33).toFixed(0),
    };
  });
}

export function makeStatArbPositions() {
  return [
    { pair:"AAPL-QQQ",  dir:+1, entryZ:-2.31, currentZ:-0.84, pnl:+312, hedgeRatio:0.847, hl:18.4, age:"00:34", status:"open" },
    { pair:"MSFT-SPY",  dir:-1, entryZ:+2.15, currentZ:+1.21, pnl:+187, hedgeRatio:1.023, hl:22.1, age:"01:12", status:"open" },
    { pair:"NVDA-AMD",  dir:+1, entryZ:-2.44, currentZ:+0.12, pnl:+641, hedgeRatio:2.114, hl:11.8, age:"02:47", status:"closed" },
    { pair:"TSLA-RIVN", dir:-1, entryZ:+2.62, currentZ:+1.95, pnl:-94,  hedgeRatio:3.271, hl:9.3,  age:"00:08", status:"open" },
  ];
}

export function makeAltDataSignals() {
  const symbols = ["AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOG","AMD","WMT","TGT"];
  const sources = ["NEWS_NLP","SOCIAL","SATELLITE","EARNINGS_WHISPER"];
  return symbols.map(sym => {
    const score = (Math.random() * 2 - 1) * 0.85;
    const signal = score > 0.2 ? "BULLISH" : score < -0.2 ? "BEARISH" : "NEUTRAL";
    const srcBreakdown = sources.map(s => ({
      source: s,
      score: +(((Math.random()-0.5)*1.6)).toFixed(2),
      signal: Math.random() > 0.5 ? "BULLISH" : Math.random() > 0.5 ? "BEARISH" : "NEUTRAL",
    }));
    return { symbol:sym, signal, score:+score.toFixed(3), sources: srcBreakdown.length, breakdown: srcBreakdown };
  });
}

export function makeCATSummary() {
  return {
    eventsToday: 2847,
    ordersTotal: 312,
    fills: 187,
    cancels: 98,
    spoofingAlerts: 2,
    criticalEscalations: 0,
    heldSymbols: [],
    symbols: [
      { symbol:"AAPL", cfr:1.2, rapidCancels:0, risk:"NONE" },
      { symbol:"TSLA", cfr:3.8, rapidCancels:1, risk:"LOW" },
      { symbol:"GME",  cfr:14.2, rapidCancels:2, risk:"HIGH" },
      { symbol:"NVDA", cfr:0.8, rapidCancels:0, risk:"NONE" },
    ],
  };
}

export function makeAuditLog() {
  const types = [
    { t:"ORDER_FILLED",       c:"green",  msg:"NVDA BUY 85 @ $847.20 filled" },
    { t:"RISK_APPROVED",      c:"blue",   msg:"TSLA LONG signal approved by TERA" },
    { t:"SIGNAL_VALIDATED",   c:"purple", msg:"AAPL SHORT: all 5 dimensions PASS" },
    { t:"REGIME_UPDATE",      c:"yellow", msg:"Regime → TREND_DAY (VIX: 16.4)" },
    { t:"TRADE_CLOSED",       c:"green",  msg:"META LONG closed +$284 (PROFIT_TARGET)" },
    { t:"RISK_REJECTED",      c:"red",    msg:"AMD: sector concentration limit hit" },
    { t:"FEED_CLEAN",         c:"green",  msg:"Data Integrity: 3 consecutive clean checks" },
    { t:"TIMER_EXPIRED",      c:"blue",   msg:"GCA: time-stop timer fired for NVDA" },
    { t:"HEALTH_OK",          c:"green",  msg:"SHA: all metrics nominal" },
    { t:"ORDER_SUBMITTED",    c:"blue",   msg:"TSLA bracket order submitted" },
    { t:"MM_QUOTE",           c:"purple", msg:"MKM: AAPL bid $182.48 / ask $182.54 (6.0bps)" },
    { t:"STAT_ARB_OPEN",      c:"blue",   msg:"SAA: AAPL-QQQ pair long z=-2.31 HL=18.4" },
    { t:"ALT_DATA_BULLISH",   c:"green",  msg:"ADA: MSFT composite score +0.67 (NEWS+WHISPER)" },
    { t:"SPOOFING_LOW",       c:"yellow", msg:"CAT: TSLA cancel/fill ratio 3.8 — monitoring" },
    { t:"CAT_EVENT_RECORDED", c:"blue",   msg:"CAT: 2847 events recorded, chain intact" },
  ];
  return types.map((x, i) => ({ ...x, ts: `09:${(32+i).toString().padStart(2,'0')}:${Math.floor(Math.random()*60).toString().padStart(2,'0')}` }));
}

export function makeHealth() {
  return {
    cpu: 18.4, mem: 42.1, eventLoop: 2.1, busLatency: 8.4, wsOk: true, drift: 0,
    cpu_h:  Array.from({length:20},(_,i)=>({ i, v:10+Math.random()*20 })),
    mem_h:  Array.from({length:20},(_,i)=>({ i, v:38+Math.random()*8 })),
    el_h:   Array.from({length:20},(_,i)=>({ i, v:1+Math.random()*4 })),
    // Latency tiers
    latency: {
      tier: "INSTITUTIONAL",
      p50_us: 1840,
      p99_us: 4200,
      budget_us: 5000,
      breach_rate: 0.012,
    },
    // FIX session
    fix: { state:"ACTIVE", out_seq:3847, target:"VENUE-DMA" },
    // CAT
    cat: { events: 2847, alerts: 2, critical: 0 },
  };
}

export function makeFXPositions() {
  const pairs = [
    { base:"EUR", quote:"USD", pip:0.0001, rate:1.0842, entry:1.0810, lots:2.0, swap:-18.4 },
    { base:"GBP", quote:"USD", pip:0.0001, rate:1.2634, entry:1.2680, lots:1.5, swap:-12.1 },
    { base:"USD", quote:"JPY", pip:0.01,   rate:151.42, entry:149.80, lots:1.0, swap:+31.2 },
    { base:"AUD", quote:"USD", pip:0.0001, rate:0.6521, entry:0.6550, lots:3.0, swap:-9.8  },
    { base:"USD", quote:"CHF", pip:0.0001, rate:0.9021, entry:0.9040, lots:1.0, swap:+5.4  },
  ];
  return pairs.map(p => {
    const pipValue = p.lots * 100_000 * p.pip;
    const pnl = (p.rate - p.entry) * p.lots * 100_000;
    const pips = (p.rate - p.entry) / p.pip;
    return {
      pair: `${p.base}/${p.quote}`,
      lots: p.lots,
      entry: p.entry,
      current: p.rate,
      pnl: +pnl.toFixed(2),
      pips: +pips.toFixed(1),
      pipValue: +pipValue.toFixed(2),
      swapPa: p.swap,
      margin: +(p.lots * 100_000 * p.rate * 0.02).toFixed(0),
    };
  });
}

export function makeFuturesPositions() {
  const contracts = [
    { sym:"ES",  name:"E-mini S&P 500", exchange:"CME",   entry:5210.50, current:5224.25, contracts:3, tickSz:0.25, tickVal:12.5, initMargin:12300 },
    { sym:"NQ",  name:"E-mini NASDAQ",  exchange:"CME",   entry:18240.0, current:18186.0, contracts:-1, tickSz:0.25, tickVal:5.0, initMargin:15500 },
    { sym:"CL",  name:"WTI Crude Oil",  exchange:"NYMEX", entry:81.42,   current:82.18,   contracts:2, tickSz:0.01, tickVal:10.0, initMargin:5000 },
    { sym:"GC",  name:"Gold",           exchange:"COMEX", entry:2312.0,  current:2328.0,  contracts:1, tickSz:0.1,  tickVal:10.0, initMargin:8200 },
  ];
  return contracts.map(c => {
    const ticks = (c.current - c.entry) / c.tickSz;
    const pnl = ticks * c.tickVal * c.contracts;
    return {
      symbol: c.sym,
      name: c.name,
      exchange: c.exchange,
      contracts: c.contracts,
      entry: c.entry,
      current: c.current,
      pnl: +pnl.toFixed(2),
      notional: +(Math.abs(c.contracts) * 50 * c.current).toFixed(0),
      margin: +(Math.abs(c.contracts) * c.initMargin).toFixed(0),
    };
  });
}

function _erf(x) {
  const t = 1/(1+0.3275911*Math.abs(x));
  const poly = t*(0.254829592+t*(-0.284496736+t*(1.421413741+t*(-1.453152027+t*1.061405429))));
  const e = 1 - poly*Math.exp(-x*x);
  return x >= 0 ? e : -e;
}
function _normCdf(x) { return 0.5*(1 + _erf(x/Math.SQRT2)); }

export function makeOptionsPositions() {
  const positions = [
    { underlying:"SPY", strike:520, expDays:14, right:"call", contracts:10,  entryPrem:4.80, spot:524.0, sigma:0.18, r:0.05 },
    { underlying:"QQQ", strike:440, expDays:7,  right:"put",  contracts:-5,  entryPrem:3.20, spot:442.0, sigma:0.22, r:0.05 },
    { underlying:"NVDA",strike:850, expDays:21, right:"call", contracts:3,   entryPrem:22.5, spot:862.0, sigma:0.45, r:0.05 },
    { underlying:"AAPL",strike:210, expDays:30, right:"put",  contracts:-10, entryPrem:5.10, spot:213.0, sigma:0.24, r:0.05 },
  ];
  function bsPrice(S, K, T, r, sigma, right) {
    if (T <= 0 || sigma <= 0) return 0;
    const sqrtT = Math.sqrt(T);
    const d1 = (Math.log(S/K) + (r + 0.5*sigma*sigma)*T) / (sigma*sqrtT);
    const d2 = d1 - sigma*sqrtT;
    if (right === "call") return S*_normCdf(d1) - K*Math.exp(-r*T)*_normCdf(d2);
    return K*Math.exp(-r*T)*_normCdf(-d2) - S*_normCdf(-d1);
  }
  function delta(S, K, T, r, sigma, right) {
    if (T <= 0 || sigma <= 0) return right==="call"?1:0;
    const sqrtT = Math.sqrt(T);
    const d1 = (Math.log(S/K) + (r + 0.5*sigma*sigma)*T) / (sigma*sqrtT);
    return right === "call" ? _normCdf(d1) : _normCdf(d1) - 1;
  }
  return positions.map(p => {
    const T = p.expDays / 365.25;
    const price = bsPrice(p.spot, p.strike, T, p.r, p.sigma, p.right);
    const d = delta(p.spot, p.strike, T, p.r, p.sigma, p.right);
    const pnl = (price - p.entryPrem) * p.contracts * 100;
    return {
      underlying: p.underlying,
      strike: p.strike,
      expDays: p.expDays,
      right: p.right,
      contracts: p.contracts,
      entryPrem: p.entryPrem,
      currentPrem: +price.toFixed(2),
      pnl: +pnl.toFixed(2),
      delta: +(d * p.contracts * 100).toFixed(1),
      sigma: p.sigma,
    };
  });
}

export function makeSandboxStrategies() {
  const strategies = [
    { name:"BreakoutV3",    version:"3.1.2", symbols:["NVDA","TSLA","AMD"],   state:"active",      dailyPnl:+342.0, winRate:0.62, trades:8,  signals:24 },
    { name:"VWAPRevert",    version:"2.0.4", symbols:["AAPL","MSFT","SPY"],   state:"active",      dailyPnl:+187.5, winRate:0.58, trades:12, signals:41 },
    { name:"MomBurst_v1",   version:"1.5.0", symbols:["NVDA","META"],         state:"quarantined", dailyPnl:-512.0, winRate:0.38, trades:6,  signals:18 },
    { name:"StatArbPilot",  version:"0.9.1", symbols:["AAPL-QQQ","MSFT-SPY"],state:"paused",      dailyPnl:+45.0,  winRate:0.55, trades:4,  signals:9  },
    { name:"AltSignalAlpha",version:"1.0.0", symbols:["TSLA","RIVN","NIO"],   state:"active",      dailyPnl:+91.0,  winRate:0.64, trades:5,  signals:15 },
  ];
  return strategies.map(s => ({
    ...s,
    strategyId: Math.random().toString(36).slice(2,10),
    drawdownPct: s.state==="quarantined" ? 5.4 : +(Math.random()*1.5).toFixed(2),
  }));
}

export function makeBacktestResults() {
  return {
    strategy: "nexus_v2_breakout",
    n_simulations: 500,
    sharpe: { mean: 2.14, stdev: 0.38, ci_lo: 1.52, ci_hi: 2.76 },
    total_return: { mean_pct: 34.7, stdev_pct: 12.4, ci_lo_pct: 16.2, ci_hi_pct: 53.1 },
    max_drawdown: { mean_pct: 8.2, worst_pct: 18.6 },
    prob_of_profit_pct: 94.2,
    expected_shortfall_5pct: -12.4,
    walk_forward: [
      { fold:"2021", sharpe:2.41, ret_pct:41.2, dd_pct:7.1 },
      { fold:"2022", sharpe:1.23, ret_pct:-8.4, dd_pct:18.6 },
      { fold:"2023", sharpe:2.87, ret_pct:52.1, dd_pct:5.8 },
      { fold:"2024", sharpe:2.31, ret_pct:38.7, dd_pct:9.2 },
    ],
  };
}

// ── HFT Layer Data ────────────────────────────────────────────────────────────

export function makeHFTTransportStats(tick) {
  const base = 420 + Math.sin(tick * 0.3) * 80;
  const hist = Array.from({ length: 20 }, (_, i) => ({
    t: i,
    ns: Math.round(380 + Math.sin((tick + i) * 0.4) * 120 + Math.random() * 40),
  }));
  return {
    ringBuffer: {
      latency_ns: Math.round(base + Math.random() * 60),
      throughput_mps: +(1.2 + Math.random() * 0.4).toFixed(2),
      producer_seq: 4096 * 12 + tick * 7,
      consumer_lag: Math.floor(Math.random() * 3),
      hist,
    },
    sbe: {
      throughput_mps: +(2.8 + Math.random() * 0.6).toFixed(2),
      avg_msg_bytes: 60,
      savings_pct: 73,
      bar: Array.from({ length: 8 }, (_, i) => ({
        label: `T-${7 - i}s`,
        msgs: Math.round(2600 + Math.random() * 800),
      })),
    },
    disruptor: {
      cursor_seq: 65536 * 3 + tick * 13,
      event_processors: 4,
      min_consumer_seq: 65536 * 3 + tick * 13 - Math.floor(Math.random() * 2),
      publish_latency_ns: Math.round(180 + Math.random() * 40),
    },
  };
}

export function makeHFTOrderBook(tick) {
  const mid = 183.20 + Math.sin(tick * 0.07) * 0.15;
  const levels = 8;
  const bids = Array.from({ length: levels }, (_, i) => {
    const price = +(mid - 0.01 * (i + 1)).toFixed(2);
    const qty = Math.round(200 + Math.random() * 800 * (1 / (i + 1)));
    const orders = Math.round(2 + Math.random() * 6);
    return { price, bidQty: qty, askQty: 0, orderCount: orders, spoofRisk: i === 0 ? 'LOW' : 'NONE' };
  });
  const asks = Array.from({ length: levels }, (_, i) => {
    const price = +(mid + 0.01 * (i + 1)).toFixed(2);
    const qty = Math.round(200 + Math.random() * 800 * (1 / (i + 1)));
    const orders = Math.round(2 + Math.random() * 6);
    return { price, bidQty: 0, askQty: qty, orderCount: orders, spoofRisk: i === 2 ? 'MEDIUM' : 'NONE' };
  });
  const spoofVenues = [
    { venue: 'IEX',       risk: 'NONE',   addCancelRatio: 0.12 },
    { venue: 'NASDAQ',    risk: 'LOW',    addCancelRatio: 0.38 },
    { venue: 'BATS',      risk: 'MEDIUM', addCancelRatio: 0.61 },
    { venue: 'DARK_POOL', risk: 'NONE',   addCancelRatio: 0.08 },
  ];
  return { bids, asks, mid: +mid.toFixed(2), spoofVenues };
}

export function makeHFTRouting(tick) {
  return [
    { venue:'IEX',       markout: +(0.18 + Math.sin(tick*0.1)*0.05).toFixed(3),   ucb1: +(2.41 + Math.random()*0.2).toFixed(3), nSelections: 342 + tick,  fillRate: 0.94 },
    { venue:'NASDAQ',    markout: +(0.09 + Math.cos(tick*0.08)*0.04).toFixed(3),  ucb1: +(2.18 + Math.random()*0.3).toFixed(3), nSelections: 218 + tick,  fillRate: 0.89 },
    { venue:'BATS',      markout: +(-0.22 - Math.abs(Math.sin(tick*0.12))*0.1).toFixed(3), ucb1: +(1.74 + Math.random()*0.2).toFixed(3), nSelections: 87 + tick, fillRate: 0.71 },
    { venue:'DARK_POOL', markout: +(0.31 + Math.sin(tick*0.05)*0.06).toFixed(3),  ucb1: +(2.67 + Math.random()*0.1).toFixed(3), nSelections: 156 + tick,  fillRate: 0.97 },
    { venue:'NYSE',      markout: +(0.04 + Math.cos(tick*0.09)*0.03).toFixed(3),  ucb1: +(1.95 + Math.random()*0.25).toFixed(3), nSelections: 64 + tick,  fillRate: 0.82 },
    { venue:'DIRECT',    markout: +(0.44 + Math.sin(tick*0.06)*0.04).toFixed(3),  ucb1: +(3.12 + Math.random()*0.1).toFixed(3), nSelections: 29 + tick,   fillRate: 0.99 },
  ];
}

export function makeHFTConsensus(tick) {
  const leaderIdx = 0;
  return Array.from({ length: 3 }, (_, i) => ({
    nodeId: i,
    state: i === leaderIdx ? 'LEADER' : 'FOLLOWER',
    term: 4 + Math.floor(tick / 120),
    logLength: 1024 + tick * 2 + i * 3,
    heartbeatAgeMs: i === leaderIdx ? 0 : Math.round(20 + Math.random() * 30),
    commitIndex: 1020 + tick * 2,
  }));
}

export function makeHFTSilicon(tick) {
  const fpgaLatencyNs = 80 + Math.round(Math.sin(tick * 0.2) * 5);
  const microwaveMs = +(3.97 + Math.sin(tick * 0.1) * 0.02).toFixed(3);
  const fiberMs = 6.87;
  return {
    fpga: {
      latency_ns: fpgaLatencyNs,
      pipeline_stages: 5,
      clock_mhz: 250,
      frames_processed: 1024 * 8 + tick * 17,
      signals_generated: 342 + tick * 2,
      risk_rejections: 12 + Math.floor(tick / 10),
      throughput_mps: 250,
      vs_software_us: 2.1,
      hist: Array.from({ length: 20 }, (_, i) => ({
        t: i,
        ns: 76 + Math.round(Math.sin((tick + i) * 0.35) * 8 + Math.random() * 4),
      })),
    },
    microwave: {
      latency_ms: microwaveMs,
      fiber_ms: fiberMs,
      advantage_ms: +(fiberMs - microwaveMs).toFixed(3),
      hops: 15,
      delivery_prob: +(0.9985 - Math.random() * 0.0002).toFixed(5),
      weather: 'clear',
      ticks_arbitraged: 287 + tick,
      total_pnl_usd: +(142_000 + tick * 450).toFixed(0),
    },
    dma: {
      latency_ns: 220,
      broker_latency_us: 2000,
      advantage_ns: 1_779_780,
      orders_submitted: 4812 + tick * 3,
      daily_rebate_usd: +(2_340 + tick * 0.8).toFixed(2),
      venues: [
        { venue: 'NASDAQ', rebate_per_share: 0.00200, daily_shares: 412_000 + tick * 100, active: true },
        { venue: 'NYSE',   rebate_per_share: 0.00150, daily_shares: 218_000 + tick * 50,  active: true },
        { venue: 'BATS',   rebate_per_share: 0.00320, daily_shares: 156_000 + tick * 30,  active: true },
      ],
    },
    clearing: {
      gross_trades: 9_814 + tick * 4,
      gross_shares: 8_200_000 + tick * 400,
      net_shares:   124_000 + tick * 10,
      netting_efficiency: +(0.015 + Math.sin(tick * 0.05) * 0.003).toFixed(4),
      fee_savings_usd: +(8_036 + tick * 0.4).toFixed(2),
      symbols_settling: 47,
      guarantee_fund_m: 25,
    },
    onlineLearner: {
      buffer_size: Math.min(1000, 120 + tick * 3),
      retrain_count: 14 + Math.floor(tick / 60),
      swap_count: 9 + Math.floor(tick / 90),
      mean_error: +(0.042 + Math.sin(tick * 0.07) * 0.008).toFixed(4),
      last_retrain_ago_s: Math.round((tick % 900)),
      cluster_gpus: 8,
      cluster_bandwidth_gbps: 200,
      training_hist: Array.from({ length: 12 }, (_, i) => ({
        t: `T-${11-i}m`,
        error: +(0.035 + Math.sin((tick + i) * 0.3) * 0.012 + Math.random() * 0.005).toFixed(4),
      })),
    },
  };
}

export function makeEnterpriseDesk(tick) {
  // VWAP algo execution
  const totalQty = 1_000_000;
  const elapsed = (tick % 360) / 360;
  const filledQty = Math.round(totalQty * elapsed * (0.95 + Math.sin(tick * 0.03) * 0.05));
  const vwapBenchmark = 182.50;
  const avgFill = +(vwapBenchmark + (Math.random() - 0.5) * 0.08).toFixed(4);
  const trackingBps = +((avgFill - vwapBenchmark) / vwapBenchmark * 10000).toFixed(3);
  const vwapHist = Array.from({ length: 40 }, (_, i) => {
    const t = i / 39;
    const cumVol = t * (1 + Math.sin(t * Math.PI) * 0.15);
    return {
      t: `${Math.round(t * 360)}m`,
      target:  +(cumVol * 100).toFixed(1),
      actual:  +(cumVol * 100 + Math.sin((tick + i) * 0.4) * 1.5).toFixed(1),
      volume:  +((0.025 + 0.015 * Math.pow(t - 0.5, 2) * 4) * 100).toFixed(1),
    };
  });

  // PTP clock sync — 5 nodes
  const ptpNodes = Array.from({ length: 5 }, (_, i) => ({
    node_id: i + 2,
    offset_ns: +(Math.sin((tick + i * 37) * 0.17) * 18 + Math.random() * 4).toFixed(2),
    quality: 'ptp_synced',
    sync_count: 2400 + tick * 8 + i * 120,
  }));
  const ptpHist = Array.from({ length: 30 }, (_, i) => ({
    t: i,
    n0: +(Math.sin((tick + i) * 0.23) * 12 + Math.random() * 3).toFixed(1),
    n1: +(Math.sin((tick + i) * 0.19 + 1) * 10 + Math.random() * 3).toFixed(1),
    n2: +(Math.sin((tick + i) * 0.31 + 2) * 14 + Math.random() * 3).toFixed(1),
    n3: +(Math.sin((tick + i) * 0.27 + 3) * 9  + Math.random() * 3).toFixed(1),
    n4: +(Math.sin((tick + i) * 0.21 + 4) * 11 + Math.random() * 3).toFixed(1),
  }));

  // Dark pool ATS
  const totalOrders = 48_200 + tick * 12;
  const internalized = Math.round(totalOrders * 0.724);
  const litRouted = totalOrders - internalized;
  const pfofPaid = +(internalized * 100 * 0.001).toFixed(2);
  const feesSaved = +(internalized * 100 * 0.003).toFixed(2);

  // VaR — Monte Carlo bell curve
  const varUsd = 2_840_000 + Math.round(Math.sin(tick * 0.05) * 120_000);
  const portVal = 47_200_000;
  const varPct = +(varUsd / portVal * 100).toFixed(2);
  const sigma = 0.015;
  const varDist = Array.from({ length: 50 }, (_, i) => {
    const x = (i / 49 - 0.5) * 0.08;
    const y = Math.exp(-0.5 * (x / sigma) ** 2) / (sigma * Math.sqrt(2 * Math.PI));
    return {
      pnl: Math.round(x * portVal / 1000),
      prob: +(y * 0.08 / 50).toFixed(5),
      isVar: i === 1,
    };
  });

  const stressTests = [
    { name:'2008 GFC',          pnl_pct:-34.2, total_pnl_usd:-16_142_000 },
    { name:'2020 COVID',        pnl_pct:-29.8, total_pnl_usd:-14_075_600 },
    { name:'1987 Black Monday', pnl_pct:-21.5, total_pnl_usd:-10_148_000 },
    { name:'Flash Crash 2010',  pnl_pct: -8.7, total_pnl_usd: -4_106_400 },
    { name:'Volmageddon 2018',  pnl_pct: -3.1, total_pnl_usd: -1_463_200 },
  ];

  return {
    algo: {
      symbol: 'AAPL', side: 'buy', algo_type: 'vwap',
      total_qty: totalQty, filled_qty: filledQty,
      fill_rate: +(filledQty / totalQty).toFixed(4),
      slices_sent: Math.round(filledQty / 100),
      avg_fill_price: avgFill,
      tracking_error_bps: trackingBps,
      duration_min: 360,
      child_size: 100,
      status: elapsed >= 1 ? 'completed' : 'active',
      vwapHist,
    },
    ptp: {
      sync_cycles: 28_800 + tick * 8,
      grandmaster_quality: 'gps_locked',
      gps_accuracy_ns: 20,
      max_inter_node_ns: +(Math.abs(Math.sin(tick * 0.13)) * 22 + 3).toFixed(2),
      failover_count: 0,
      nodes: ptpNodes,
      hist: ptpHist,
    },
    darkPool: {
      total_orders: totalOrders,
      internalized,
      lit_routed: litRouted,
      internalization_rate: +(internalized / totalOrders).toFixed(4),
      pfof_paid_usd: pfofPaid,
      fees_saved_usd: feesSaved,
      net_benefit_usd: +(feesSaved - pfofPaid).toFixed(2),
      volume_internalized: internalized * 100,
    },
    var: {
      confidence_level: 0.99,
      horizon_days: 1,
      portfolio_value_usd: portVal,
      var_usd: varUsd,
      cvar_usd: Math.round(varUsd * 1.38),
      var_pct: varPct,
      worst_scenario_usd: Math.round(varUsd * 2.1),
      dist: varDist,
      stress: stressTests,
    },
  };
}
