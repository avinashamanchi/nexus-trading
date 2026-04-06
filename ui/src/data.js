// Mock real-time data — replaces WebSocket feed in demo mode
// In production: connect to ws://localhost:8080/ws

export const AGENTS = [
  { id:0, name:"Edge Research", abbr:"ERA", color:"#0A84FF", desc:"Setup expectancy guardian" },
  { id:1, name:"Market Universe", abbr:"MUA", color:"#30D158", desc:"Symbol filtering & ranking" },
  { id:2, name:"Market Regime", abbr:"MRA", color:"#BF5AF2", desc:"Regime classification gate" },
  { id:3, name:"Data Integrity", abbr:"DIA", color:"#FF9F0A", desc:"Feed anomaly detection" },
  { id:4, name:"Micro Signal", abbr:"MiSA", color:"#64D2FF", desc:"Pattern detection engine" },
  { id:5, name:"Signal Validation", abbr:"SVA", color:"#0A84FF", desc:"5-dimension quality gate" },
  { id:6, name:"TERA", abbr:"TERA", color:"#FF453A", desc:"10 hard risk rules" },
  { id:7, name:"Sizing Plan", abbr:"SPA", color:"#32D74B", desc:"Fully costed trade plans" },
  { id:8, name:"Execution", abbr:"EXA", color:"#FFD60A", desc:"Bracket order placement" },
  { id:9, name:"Broker Recon", abbr:"BRA", color:"#FF9F0A", desc:"State reconciliation" },
  { id:10, name:"Exec Quality", abbr:"EQA", color:"#BF5AF2", desc:"Slippage & fill tracking" },
  { id:11, name:"Micro Monitor", abbr:"MMA", color:"#64D2FF", desc:"Tick-by-tick monitoring" },
  { id:12, name:"Exit & Lock-In", abbr:"ELA", color:"#32D74B", desc:"6 named exit modes" },
  { id:13, name:"Portfolio Sup.", abbr:"PSA", color:"#0A84FF", desc:"Circuit breakers & halt" },
  { id:14, name:"Post-Session", abbr:"PSRA", color:"#FF9F0A", desc:"Performance analysis" },
  { id:15, name:"Tax & Compliance", abbr:"TCA", color:"#BF5AF2", desc:"Wash-sale & PDT tracking" },
  { id:16, name:"Human Gov.", abbr:"HGL", color:"#FFD60A", desc:"Human approval layer" },
  { id:17, name:"Global Clock", abbr:"GCA", color:"#32D74B", desc:"Authoritative time source" },
  { id:18, name:"Shadow Replay", abbr:"SMRE", color:"#64D2FF", desc:"Deterministic replay engine" },
  { id:19, name:"System Health", abbr:"SHA", color:"#FF9F0A", desc:"Infrastructure monitoring" },
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
    { step:"Universe", status:"active", signals:12, icon:"🌐" },
    { step:"Regime", status:"active", signals:1, icon:"📊" },
    { step:"Data Check", status:"active", signals:1, icon:"✅" },
    { step:"Signals", status:"active", signals:3, icon:"⚡" },
    { step:"Validate", status:"active", signals:2, icon:"🔬" },
    { step:"Risk Gate", status:"active", signals:2, icon:"🛡️" },
    { step:"Size Plan", status:"active", signals:1, icon:"📐" },
    { step:"Execute", status:"active", signals:1, icon:"⚙️" },
  ];
}

export function makePositions() {
  return [
    { symbol:"NVDA", direction:"LONG", shares:85, entry:847.20, current:852.40, pnl:+442.0, stop:842.00, target:862.00, time:"04:32", setup:"BREAKOUT" },
    { symbol:"TSLA", direction:"LONG", shares:50, entry:248.50, current:251.80, pnl:+165.0, stop:245.00, target:256.00, time:"02:14", setup:"VWAP_TAP" },
    { symbol:"AAPL", direction:"SHORT", shares:100, entry:218.90, current:217.50, pnl:+140.0, stop:221.00, target:215.00, time:"01:07", setup:"MOMENTUM_BURST" },
  ];
}

export function makeAuditLog() {
  const types = [
    { t:"ORDER_FILLED", c:"green", msg:"NVDA BUY 85 @ $847.20 filled" },
    { t:"RISK_APPROVED", c:"blue", msg:"TSLA LONG signal approved by TERA" },
    { t:"SIGNAL_VALIDATED", c:"purple", msg:"AAPL SHORT: all 5 dimensions PASS" },
    { t:"REGIME_UPDATE", c:"yellow", msg:"Regime → TREND_DAY (VIX: 16.4)" },
    { t:"TRADE_CLOSED", c:"green", msg:"META LONG closed +$284 (PROFIT_TARGET)" },
    { t:"RISK_REJECTED", c:"red", msg:"AMD: sector concentration limit hit" },
    { t:"FEED_CLEAN", c:"green", msg:"Data Integrity: 3 consecutive clean checks" },
    { t:"TIMER_EXPIRED", c:"blue", msg:"GCA: time-stop timer fired for NVDA" },
    { t:"HEALTH_OK", c:"green", msg:"SHA: all metrics nominal" },
    { t:"ORDER_SUBMITTED", c:"blue", msg:"TSLA bracket order submitted" },
  ];
  return types.map((x, i) => ({ ...x, ts: `09:${(32+i).toString().padStart(2,'0')}:${Math.floor(Math.random()*60).toString().padStart(2,'0')}` }));
}

export function makeHealth() {
  return {
    cpu: 18.4, mem: 42.1, eventLoop: 2.1, busLatency: 8.4, wsOk: true, drift: 0,
    cpu_h: Array.from({length:20},(_,i)=>({ i, v:10+Math.random()*20 })),
    mem_h: Array.from({length:20},(_,i)=>({ i, v:38+Math.random()*8 })),
    el_h: Array.from({length:20},(_,i)=>({ i, v:1+Math.random()*4 })),
  };
}
