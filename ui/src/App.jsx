import { useState, useEffect } from 'react'
import {
  AreaChart, Area, LineChart, Line, BarChart, Bar,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine
} from 'recharts'
import {
  AGENTS, makeLivePnl, makePipeline, makePositions, makeAuditLog, makeHealth,
  makeMMPositions, makeStatArbPositions, makeAltDataSignals, makeCATSummary, makeBacktestResults,
  makeFXPositions, makeFuturesPositions, makeOptionsPositions, makeSandboxStrategies
} from './data'
import './App.css'

// ── Mini sparkline ──────────────────────────────────────────────────────────
function Spark({ data, color = '#32D74B', h = 36, dataKey = 'v' }) {
  return (
    <ResponsiveContainer width="100%" height={h}>
      <AreaChart data={data} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id={`sg-${color.replace('#','')}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={color} stopOpacity={0.3} />
            <stop offset="95%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <Area type="monotone" dataKey={dataKey} stroke={color} strokeWidth={1.5}
          fill={`url(#sg-${color.replace('#','')})`} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  )
}

// ── Ring progress ───────────────────────────────────────────────────────────
function Ring({ pct, color = '#32D74B', size = 72, stroke = 5, label, sub }) {
  const r = (size - stroke * 2) / 2
  const circ = 2 * Math.PI * r
  const offset = circ - (pct / 100) * circ
  return (
    <div style={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
      <svg width={size} height={size} style={{ transform: 'rotate(-90deg)' }}>
        <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="rgba(255,255,255,0.07)" strokeWidth={stroke} />
        <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={color} strokeWidth={stroke}
          strokeDasharray={circ} strokeDashoffset={offset} strokeLinecap="round"
          style={{ transition: 'stroke-dashoffset 1s cubic-bezier(.22,1,.36,1)', filter: `drop-shadow(0 0 6px ${color})` }} />
      </svg>
      <div style={{ position:'absolute', inset:0, display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center' }}>
        <span style={{ fontSize: 14, fontWeight: 700, color: 'rgba(255,255,255,.92)', lineHeight: 1 }}>{label}</span>
        {sub && <span style={{ fontSize: 10, color: 'rgba(255,255,255,.45)', marginTop: 2 }}>{sub}</span>}
      </div>
    </div>
  )
}

// ── Kill Switch modal ───────────────────────────────────────────────────────
function KillSwitchModal({ onClose }) {
  const [confirm, setConfirm] = useState('')
  const triggered = confirm === 'FLATTEN ALL'
  return (
    <div style={{ position:'fixed', inset:0, zIndex:9000, display:'flex', alignItems:'center', justifyContent:'center', background:'rgba(0,0,0,0.85)', backdropFilter:'blur(12px)' }}
      onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="glass anim-up" style={{ maxWidth:440, width:'90%', padding:32, textAlign:'center' }}>
        <div style={{ width:72, height:72, borderRadius:'50%', background:'rgba(255,69,58,0.15)', border:'2px solid rgba(255,69,58,0.4)',
          display:'flex', alignItems:'center', justifyContent:'center', margin:'0 auto 20px', fontSize:28,
          boxShadow:'0 0 40px rgba(255,69,58,0.3)', animation:'ks-pulse 1.5s ease-in-out infinite' }}>⚡</div>
        <h2 style={{ fontSize:22, fontWeight:700, marginBottom:8, color:'var(--red)' }}>Emergency Kill Switch</h2>
        <p style={{ color:'var(--t2)', fontSize:13, marginBottom:24, lineHeight:1.6 }}>
          Cancels <strong style={{color:'var(--t1)'}}>all orders</strong> and flattens <strong style={{color:'var(--t1)'}}>all positions</strong> — including MM quotes and stat-arb pairs — via direct broker API.
        </p>
        <div style={{ background:'rgba(255,69,58,0.08)', border:'1px solid rgba(255,69,58,0.2)', borderRadius:12, padding:16, marginBottom:20, textAlign:'left' }}>
          <p style={{ fontSize:12, color:'var(--red)', marginBottom:10, fontWeight:600, letterSpacing:'.04em', textTransform:'uppercase' }}>SLO Guarantees</p>
          {[['ACK response','≤ 250ms'],['PSA state transition','≤ 500ms'],['Cancel dispatch','≤ 1s'],['Full flatten','≤ 10s p95']].map(([k,v]) => (
            <div key={k} style={{ display:'flex', justifyContent:'space-between', padding:'4px 0', borderBottom:'1px solid rgba(255,255,255,0.05)' }}>
              <span style={{ color:'var(--t2)', fontSize:12 }}>{k}</span>
              <span style={{ color:'var(--t1)', fontSize:12, fontWeight:600 }}>{v}</span>
            </div>
          ))}
        </div>
        <p style={{ fontSize:12, color:'var(--t2)', marginBottom:10 }}>Type <strong style={{color:'var(--t1)', letterSpacing:2}}>FLATTEN ALL</strong> to confirm:</p>
        <input value={confirm} onChange={e => setConfirm(e.target.value)}
          placeholder="FLATTEN ALL" autoFocus
          style={{ width:'100%', padding:'10px 14px', background:'rgba(255,255,255,0.06)', border:'1px solid var(--border2)',
            borderRadius:10, color:'var(--t1)', fontSize:14, fontFamily:'var(--font)', outline:'none', textAlign:'center',
            letterSpacing:2, fontWeight:700, marginBottom:20 }} />
        <div style={{ display:'flex', gap:10 }}>
          <button className="btn btn-ghost" style={{ flex:1 }} onClick={onClose}>Cancel</button>
          <button className="btn btn-danger" style={{ flex:1, opacity: triggered ? 1 : 0.4, cursor: triggered ? 'pointer' : 'not-allowed' }}
            disabled={!triggered}
            onClick={() => { alert('KILL SWITCH TRIGGERED — all positions being flattened'); onClose(); }}>
            🔴 EXECUTE FLATTEN
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Agent Card ──────────────────────────────────────────────────────────────
function AgentCard({ agent, status }) {
  const c = { active:'green', warning:'yellow', error:'red', idle:'grey' }[status] || 'grey'
  const labels = { active:'ACTIVE', warning:'WARN', error:'ERROR', idle:'IDLE' }
  const tierColors = { pipeline:'var(--blue)', infra:'var(--green)', institutional:'var(--purple)' }
  return (
    <div className="glass-sm" style={{ padding:'12px 14px', display:'flex', alignItems:'center', gap:12,
      borderColor: status === 'active' ? 'rgba(50,215,75,0.15)' : 'var(--border)' }}>
      <div style={{ width:36, height:36, borderRadius:10, background:agent.color+'22',
        border:`1px solid ${agent.color}33`, display:'flex', alignItems:'center', justifyContent:'center',
        fontSize:11, fontWeight:700, color:agent.color, flexShrink:0 }}>{agent.abbr.slice(0,4)}</div>
      <div style={{ flex:1, minWidth:0 }}>
        <div style={{ fontSize:12, fontWeight:600, color:'var(--t1)', whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{agent.name}</div>
        <div style={{ fontSize:10, color:'var(--t2)', whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{agent.desc}</div>
      </div>
      <div style={{ display:'flex', flexDirection:'column', alignItems:'flex-end', gap:3, flexShrink:0 }}>
        <div style={{ display:'flex', alignItems:'center', gap:4 }}>
          <div className={`sdot sdot-${c}${status === 'active' ? ' sdot-pulse' : ''}`}></div>
          <span style={{ fontSize:10, fontWeight:600, color: status==='active'?'var(--green)':status==='error'?'var(--red)':'var(--t2)', letterSpacing:'.04em' }}>{labels[status]||'IDLE'}</span>
        </div>
        <span style={{ fontSize:9, color:tierColors[agent.tier]||'var(--t3)', letterSpacing:'.04em', textTransform:'uppercase' }}>{agent.tier}</span>
      </div>
    </div>
  )
}

// ── Position row ────────────────────────────────────────────────────────────
function PositionRow({ pos }) {
  const pct = ((pos.current - pos.entry) / pos.entry * (pos.direction === 'LONG' ? 1 : -1) * 100).toFixed(2)
  const progress = Math.min(100, Math.max(0, ((pos.current - pos.stop) / (pos.target - pos.stop)) * 100))
  return (
    <div style={{ padding:'12px 16px', borderBottom:'1px solid var(--border)', display:'flex', gap:16, alignItems:'center' }}>
      <div style={{ width:44 }}>
        <div style={{ fontSize:13, fontWeight:700 }}>{pos.symbol}</div>
        <span className={`badge badge-${pos.direction==='LONG'?'green':'red'}`} style={{ fontSize:9 }}>{pos.direction}</span>
      </div>
      <div style={{ flex:1 }}>
        <div style={{ display:'flex', justifyContent:'space-between', fontSize:11, color:'var(--t2)', marginBottom:4 }}>
          <span>Stop ${pos.stop.toFixed(2)}</span><span>Target ${pos.target.toFixed(2)}</span>
        </div>
        <div style={{ height:4, borderRadius:2, background:'rgba(255,255,255,0.07)', overflow:'hidden' }}>
          <div style={{ height:'100%', width:`${progress}%`, background:'linear-gradient(90deg, var(--green), #30d158)', borderRadius:2 }}></div>
        </div>
      </div>
      <div style={{ textAlign:'right', width:80 }}>
        <div style={{ fontSize:14, fontWeight:700, color: pos.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
          {pos.pnl >= 0 ? '+' : ''}${pos.pnl.toFixed(0)}
        </div>
        <div style={{ fontSize:11, color:'var(--t2)' }}>{pct}%</div>
      </div>
      <div style={{ textAlign:'right', fontSize:11, color:'var(--t2)', width:48 }}>
        <div>{pos.shares}sh</div><div>${pos.current.toFixed(2)}</div>
      </div>
    </div>
  )
}

// ── Audit entry ─────────────────────────────────────────────────────────────
function AuditEntry({ entry, delay }) {
  return (
    <div style={{ display:'flex', gap:10, padding:'8px 0', borderBottom:'1px solid var(--border)', animation:`slide-up .3s ${delay}s both` }}>
      <div style={{ fontSize:10, color:'var(--t3)', flexShrink:0, marginTop:1, fontVariantNumeric:'tabular-nums' }}>{entry.ts}</div>
      <div className={`sdot sdot-${entry.c}`} style={{ marginTop:5, flexShrink:0 }}></div>
      <div>
        <div style={{ fontSize:11, fontWeight:600, color:'var(--t2)', letterSpacing:'.04em', textTransform:'uppercase' }}>{entry.t}</div>
        <div style={{ fontSize:12, color:'var(--t1)' }}>{entry.msg}</div>
      </div>
    </div>
  )
}

// ── Pipeline flow ────────────────────────────────────────────────────────────
function PipelineFlow({ steps }) {
  return (
    <div style={{ display:'flex', alignItems:'center', gap:0, overflowX:'auto', padding:'4px 0' }}>
      {steps.map((s, i) => (
        <div key={i} style={{ display:'flex', alignItems:'center', flexShrink:0 }}>
          <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:4 }}>
            <div style={{ width:44, height:44, borderRadius:12, background:'rgba(50,215,75,0.12)',
              border:'1px solid rgba(50,215,75,0.25)', display:'flex', alignItems:'center', justifyContent:'center',
              fontSize:18 }}>{s.icon}</div>
            <div style={{ fontSize:10, color:'var(--t2)', fontWeight:500, textAlign:'center', maxWidth:52 }}>{s.step}</div>
            {s.signals > 0 && <div style={{ fontSize:9, color:'var(--green)', fontWeight:700 }}>{s.signals}→</div>}
          </div>
          {i < steps.length-1 && (
            <svg width={32} height={2} style={{ flexShrink:0 }}>
              <line x1="0" y1="1" x2="32" y2="1" stroke="rgba(50,215,75,0.35)" strokeWidth="1.5" strokeDasharray="4 2">
                <animate attributeName="stroke-dashoffset" from="6" to="0" dur="1s" repeatCount="indefinite" />
              </line>
            </svg>
          )}
        </div>
      ))}
    </div>
  )
}

// ── Main App ────────────────────────────────────────────────────────────────
const TABS = ['dashboard','agents','positions','institutional','multi-asset','audit','health']

export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [ksOpen, setKsOpen] = useState(false)
  const [time, setTime] = useState(new Date())
  const [pnlData, setPnlData] = useState(makeLivePnl())
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const t = setInterval(() => { setTime(new Date()); setTick(n=>n+1) }, 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    if (tick % 4 === 0) {
      setPnlData(prev => {
        const last = prev[prev.length-1]
        const next = { t: last.t+1, v: last.v + (Math.random()-0.42)*35 }
        return [...prev.slice(-60), next]
      })
    }
  }, [tick])

  const agentStatuses = AGENTS.map((a, i) => 'active')

  const positions = makePositions()
  const audit = makeAuditLog()
  const pipeline = makePipeline()
  const health = makeHealth()
  const totalPnl = positions.reduce((s, p) => s + p.pnl, 0) + 1847
  const pnlPositive = totalPnl >= 0

  const timeStr = time.toLocaleTimeString('en-US', { hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit' })
  const etTime = new Date(time.toLocaleString('en-US', { timeZone: 'America/New_York' }))
  const isMarketOpen = etTime.getHours() >= 9 && etTime.getHours() < 16

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100vh', overflow:'hidden' }}>

      {/* ── Topbar ── */}
      <div style={{ display:'flex', alignItems:'center', gap:16, padding:'12px 20px',
        background:'rgba(0,0,0,0.45)', backdropFilter:'blur(32px)', borderBottom:'1px solid var(--border)',
        flexShrink:0, zIndex:100 }}>

        <div style={{ display:'flex', alignItems:'center', gap:10 }}>
          <div style={{ width:32, height:32, borderRadius:10, background:'linear-gradient(135deg,#0A84FF,#BF5AF2)',
            display:'flex', alignItems:'center', justifyContent:'center', fontSize:16, boxShadow:'0 0 24px rgba(10,132,255,0.4)' }}>⚡</div>
          <div>
            <div style={{ fontSize:13, fontWeight:700, letterSpacing:'-0.01em' }}>NEXUS TRADING</div>
            <div style={{ fontSize:10, color:'var(--t2)' }}>24-Agent Institutional AI</div>
          </div>
        </div>

        <div style={{ flex:1 }} />

        <div className="tab-bar" style={{ display:'flex' }}>
          {TABS.map(t => (
            <button key={t} className={`tab${tab===t?' active':''}`} onClick={()=>setTab(t)}>
              {t.split('-').map(w=>w.charAt(0).toUpperCase()+w.slice(1)).join('-')}
            </button>
          ))}
        </div>

        <div style={{ flex:1 }} />

        <div style={{ display:'flex', alignItems:'center', gap:12 }}>
          <div style={{ textAlign:'right' }}>
            <div style={{ fontSize:12, fontWeight:700, fontVariantNumeric:'tabular-nums' }}>{timeStr}</div>
            <div style={{ fontSize:10, color:'var(--t2)' }}>ET {isMarketOpen ? 'OPEN' : 'CLOSED'}</div>
          </div>
          <span className={`badge ${isMarketOpen ? 'badge-green' : 'badge-yellow'}`}>
            <div className={`sdot sdot-${isMarketOpen?'green':'yellow'}${isMarketOpen?' sdot-pulse':''}`}></div>
            {isMarketOpen ? 'MARKET OPEN' : 'PRE-SESSION'}
          </span>
          <span className="badge badge-blue"><div className="sdot sdot-blue sdot-pulse"></div>ACTIVE</span>
          <button style={{ width:40, height:40, borderRadius:12, background:'rgba(255,69,58,0.12)',
            border:'1.5px solid rgba(255,69,58,0.35)', cursor:'pointer', fontSize:16,
            display:'flex',alignItems:'center',justifyContent:'center', boxShadow:'0 0 16px rgba(255,69,58,0.2)' }}
            onClick={() => setKsOpen(true)} title="Kill Switch">⚡</button>
        </div>
      </div>

      {/* ── Content ── */}
      <div style={{ flex:1, overflow:'hidden', display:'flex' }}>
        {tab === 'dashboard'     && <DashboardTab pnlData={pnlData} totalPnl={totalPnl} positions={positions} audit={audit} pipeline={pipeline} health={health} tick={tick} />}
        {tab === 'agents'        && <AgentsTab agents={AGENTS} statuses={agentStatuses} />}
        {tab === 'positions'     && <PositionsTab positions={positions} />}
        {tab === 'institutional' && <InstitutionalTab />}
        {tab === 'multi-asset'   && <MultiAssetTab />}
        {tab === 'audit'         && <AuditTab audit={audit} />}
        {tab === 'health'        && <HealthTab health={health} tick={tick} />}
      </div>

      {ksOpen && <KillSwitchModal onClose={() => setKsOpen(false)} />}
    </div>
  )
}

// ── Dashboard Tab ────────────────────────────────────────────────────────────
function DashboardTab({ pnlData, totalPnl, positions, audit, pipeline, health, tick }) {
  const pnlPos = totalPnl >= 0
  const winRate = 68.4
  const maxDD = 1.2
  const equity = 30000 + totalPnl

  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr 1fr', gap:16 }}>
        {[
          { label:'Session P&L',    value:`${pnlPos?'+':''}$${Math.abs(totalPnl).toLocaleString()}`, color:pnlPos?'var(--green)':'var(--red)', sub:'Today' },
          { label:'Account Equity', value:`$${equity.toLocaleString(undefined,{minimumFractionDigits:0})}`,  color:'var(--t1)',    sub:'Paper account' },
          { label:'Win Rate',       value:`${winRate}%`, color:'var(--blue)',   sub:'14 trades today' },
          { label:'Max Drawdown',   value:`${maxDD}%`,   color:'var(--yellow)', sub:'vs 2.0% cap' },
        ].map((m, i) => (
          <div key={i} className="glass" style={{ padding:20, animation:`slide-up .4s ${i*0.08}s both` }}>
            <div style={{ fontSize:11, color:'var(--t2)', fontWeight:500, marginBottom:6, textTransform:'uppercase', letterSpacing:'.06em' }}>{m.label}</div>
            <div style={{ fontSize:28, fontWeight:800, color:m.color, letterSpacing:'-0.02em', lineHeight:1 }}>{m.value}</div>
            <div style={{ fontSize:11, color:'var(--t3)', marginTop:4 }}>{m.sub}</div>
          </div>
        ))}
      </div>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 320px', gap:16 }}>
        <div className="glass" style={{ padding:20 }}>
          <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:16 }}>
            <div>
              <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:2 }}>Intraday P&L Curve</div>
              <div style={{ fontSize:22, fontWeight:800, color:pnlPos?'var(--green)':'var(--red)' }}>{pnlPos?'+':''}${Math.abs(totalPnl).toLocaleString()}</div>
            </div>
            <span className="badge badge-green">Session Active</span>
          </div>
          <ResponsiveContainer width="100%" height={160}>
            <AreaChart data={pnlData}>
              <defs>
                <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={pnlPos?'#32D74B':'#FF453A'} stopOpacity={0.3} />
                  <stop offset="95%" stopColor={pnlPos?'#32D74B':'#FF453A'} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.04)" />
              <XAxis dataKey="t" hide /><YAxis hide />
              <ReferenceLine y={0} stroke="rgba(255,255,255,0.1)" />
              <Tooltip contentStyle={{ background:'rgba(15,15,24,0.9)', border:'1px solid rgba(255,255,255,0.1)', borderRadius:10, fontSize:12 }}
                formatter={(v) => [`$${v.toFixed(0)}`, 'P&L']} labelFormatter={() => ''} />
              <Area type="monotone" dataKey="v" stroke={pnlPos?'#32D74B':'#FF453A'}
                strokeWidth={2} fill="url(#pnlGrad)" dot={false} animationDuration={300} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className="glass" style={{ padding:20, display:'flex', flexDirection:'column', gap:16 }}>
          <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>Session Metrics</div>
          <div style={{ display:'flex', justifyContent:'space-around', alignItems:'center', flex:1 }}>
            <Ring pct={winRate} color="var(--green)" size={80} label={`${winRate}%`} sub="Win rate" />
            <Ring pct={(maxDD/2)*100} color="var(--yellow)" size={80} label={`${maxDD}%`} sub="Drawdown" />
          </div>
          <div className="divider" />
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:8 }}>
            {[
              { label:'Positions', value:'3', color:'var(--blue)' },
              { label:'MM Quotes', value:'20', color:'var(--purple)' },
              { label:'Stat Arb', value:'3 pairs', color:'var(--blue)' },
              { label:'Regime', value:'TREND', color:'var(--green)' },
            ].map((s,i) => (
              <div key={i} style={{ padding:'8px 10px', background:'rgba(255,255,255,0.035)', borderRadius:10 }}>
                <div style={{ fontSize:10, color:'var(--t2)', marginBottom:2 }}>{s.label}</div>
                <div style={{ fontSize:14, fontWeight:700, color:s.color }}>{s.value}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap:16 }}>
        <div className="glass" style={{ padding:20 }}>
          <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:12 }}>Live Pipeline</div>
          <PipelineFlow steps={pipeline} />
          <div className="divider" style={{ margin:'12px 0' }} />
          <span className="badge badge-green"><div className="sdot sdot-green sdot-pulse"></div>FSM: ACTIVE</span>
        </div>
        <div className="glass" style={{ overflow:'hidden' }}>
          <div style={{ padding:'16px 16px 12px', display:'flex', justifyContent:'space-between', alignItems:'center' }}>
            <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>Open Positions</div>
            <span className="badge badge-blue">{positions.length}</span>
          </div>
          <div className="divider" />
          {positions.map((p,i) => <PositionRow key={i} pos={p} />)}
        </div>
        <div className="glass" style={{ overflow:'hidden', display:'flex', flexDirection:'column' }}>
          <div style={{ padding:'16px 16px 12px', display:'flex', justifyContent:'space-between', alignItems:'center', flexShrink:0 }}>
            <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>Audit Log</div>
            <div className="sdot sdot-green sdot-pulse"></div>
          </div>
          <div className="divider" />
          <div style={{ flex:1, overflow:'auto', padding:'0 16px' }}>
            {audit.slice(0,8).map((e,i) => <AuditEntry key={i} entry={e} delay={i*0.03} />)}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Agents Tab ───────────────────────────────────────────────────────────────
function AgentsTab({ agents, statuses }) {
  const [tierFilter, setTierFilter] = useState('all')
  const tiers = ['all','pipeline','infra','institutional']
  const filtered = tierFilter === 'all' ? agents : agents.filter(a => a.tier === tierFilter)
  const active = statuses.filter(s=>s==='active').length

  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      <div style={{ display:'flex', gap:12, alignItems:'center' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>Agent Monitor</h2>
        <span className="badge badge-green">{active}/{agents.length} Active</span>
      </div>
      <div className="tab-bar">
        {tiers.map(t => (
          <button key={t} className={`tab${tierFilter===t?' active':''}`} onClick={()=>setTierFilter(t)}>
            {t.charAt(0).toUpperCase()+t.slice(1)}
          </button>
        ))}
      </div>
      <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))', gap:8 }}>
        {filtered.map((a,i) => <AgentCard key={a.id} agent={a} status={statuses[a.id]} />)}
      </div>
    </div>
  )
}

// ── Positions Tab ────────────────────────────────────────────────────────────
function PositionsTab({ positions }) {
  const totalPnl = positions.reduce((s,p)=>s+p.pnl, 0)
  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      <div style={{ display:'flex', gap:12, alignItems:'center' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>Open Positions</h2>
        <span style={{ fontSize:20, fontWeight:800, color: totalPnl>=0?'var(--green)':'var(--red)' }}>
          {totalPnl>=0?'+':''}${totalPnl.toFixed(0)} unrealized
        </span>
      </div>
      <div className="glass" style={{ overflow:'hidden' }}>
        <div style={{ display:'grid', gridTemplateColumns:'60px 1fr 120px 120px 100px 80px', padding:'10px 16px',
          background:'rgba(255,255,255,0.03)', fontSize:11, color:'var(--t2)', fontWeight:600, letterSpacing:'.05em', textTransform:'uppercase', gap:16 }}>
          <span>Symbol</span><span>Progress</span><span>Entry</span><span>Current</span><span>P&L</span><span>Shares</span>
        </div>
        <div className="divider" />
        {positions.map((p,i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'60px 1fr 120px 120px 100px 80px',
            padding:'14px 16px', borderBottom:'1px solid var(--border)', alignItems:'center', gap:16 }}>
            <div>
              <div style={{ fontWeight:700 }}>{p.symbol}</div>
              <span className={`badge badge-${p.direction==='LONG'?'green':'red'}`} style={{fontSize:9}}>{p.direction}</span>
            </div>
            <div>
              <div style={{ display:'flex', justifyContent:'space-between', fontSize:10, color:'var(--t2)', marginBottom:4 }}>
                <span>Stop ${p.stop.toFixed(2)}</span><span>Target ${p.target.toFixed(2)}</span>
              </div>
              <div style={{ height:6, background:'rgba(255,255,255,0.06)', borderRadius:3, overflow:'hidden' }}>
                <div style={{ height:'100%', width:`${Math.min(100,Math.max(0,((p.current-p.stop)/(p.target-p.stop))*100))}%`,
                  background:'linear-gradient(90deg,var(--green),#30d158)', borderRadius:3 }} />
              </div>
              <span className="badge badge-blue" style={{fontSize:9,marginTop:4}}>{p.setup}</span>
            </div>
            <div style={{ fontVariantNumeric:'tabular-nums' }}>${p.entry.toFixed(2)}</div>
            <div style={{ fontVariantNumeric:'tabular-nums' }}>${p.current.toFixed(2)}</div>
            <div style={{ fontWeight:700, color:p.pnl>=0?'var(--green)':'var(--red)' }}>{p.pnl>=0?'+':''}${p.pnl.toFixed(0)}</div>
            <div style={{ color:'var(--t2)' }}>{p.shares}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Institutional Tab ─────────────────────────────────────────────────────────
function InstitutionalTab() {
  const [subTab, setSubTab] = useState('market_making')
  const mmPositions = makeMMPositions()
  const statArb = makeStatArbPositions()
  const altData = makeAltDataSignals()
  const cat = makeCATSummary()
  const backtest = makeBacktestResults()

  const mmPnl = mmPositions.reduce((s,p)=>s+p.pnl, 0)
  const saOpen = statArb.filter(p=>p.status==='open')
  const saPnl = statArb.reduce((s,p)=>s+p.pnl, 0)
  const actionable = altData.filter(d=>d.signal!=='NEUTRAL').length

  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      {/* Sub-tab bar */}
      <div style={{ display:'flex', gap:12, alignItems:'center', flexWrap:'wrap' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>Institutional</h2>
        <div className="tab-bar">
          {[['market_making','Market Making'],['stat_arb','Stat Arb'],['alt_data','Alt Data'],['cat','CAT Compliance'],['backtest','Backtest']].map(([k,v]) => (
            <button key={k} className={`tab${subTab===k?' active':''}`} onClick={()=>setSubTab(k)}>{v}</button>
          ))}
        </div>
      </div>

      {subTab === 'market_making' && (
        <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:12 }}>
            {[
              { label:'MM P&L Today', value:`${mmPnl>=0?'+':''}$${Math.abs(mmPnl).toFixed(0)}`, color:mmPnl>=0?'var(--green)':'var(--red)' },
              { label:'Active Symbols', value:`${mmPositions.length}`, color:'var(--blue)' },
              { label:'Fills Today', value:`${mmPositions.reduce((s,p)=>s+p.fills,0)}`, color:'var(--t1)' },
              { label:'Latency Tier', value:'INSTITUTIONAL', color:'var(--purple)' },
            ].map((m,i) => (
              <div key={i} className="glass" style={{ padding:16 }}>
                <div style={{ fontSize:11, color:'var(--t2)', marginBottom:4, textTransform:'uppercase', letterSpacing:'.06em' }}>{m.label}</div>
                <div style={{ fontSize:22, fontWeight:800, color:m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
          <div className="glass" style={{ overflow:'hidden' }}>
            <div style={{ padding:'12px 16px', fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>
              Avellaneda-Stoikov Quotes — Live Inventory
            </div>
            <div className="divider" />
            <div style={{ display:'grid', gridTemplateColumns:'64px 80px 90px 90px 60px 80px 70px 80px', padding:'8px 16px',
              fontSize:10, color:'var(--t3)', fontWeight:600, textTransform:'uppercase', letterSpacing:'.05em', gap:8 }}>
              <span>Symbol</span><span>Inventory</span><span>Bid</span><span>Ask</span><span>Spread</span><span>P&L</span><span>Fills</span><span>Action</span>
            </div>
            <div className="divider" />
            {mmPositions.map((p,i) => (
              <div key={i} style={{ display:'grid', gridTemplateColumns:'64px 80px 90px 90px 60px 80px 70px 80px',
                padding:'9px 16px', borderBottom:'1px solid var(--border)', alignItems:'center', gap:8, fontSize:12 }}>
                <span style={{ fontWeight:700 }}>{p.symbol}</span>
                <span style={{ color:p.inventory>0?'var(--green)':p.inventory<0?'var(--red)':'var(--t2)', fontWeight:600 }}>
                  {p.inventory>0?'+':''}{p.inventory}
                </span>
                <span style={{ color:'var(--green)', fontVariantNumeric:'tabular-nums' }}>${p.bid}</span>
                <span style={{ color:'var(--red)',   fontVariantNumeric:'tabular-nums' }}>${p.ask}</span>
                <span style={{ color:'var(--t2)' }}>{p.spread_bps}bps</span>
                <span style={{ color:p.pnl>=0?'var(--green)':'var(--red)', fontWeight:600 }}>{p.pnl>=0?'+':''}${p.pnl.toFixed(0)}</span>
                <span style={{ color:'var(--t2)' }}>{p.fills}</span>
                <span className={`badge badge-${p.action==='NEUTRAL'?'blue':p.action.includes('BID')?'green':'yellow'}`} style={{fontSize:9}}>
                  {p.action.replace('_',' ')}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {subTab === 'stat_arb' && (
        <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:12 }}>
            {[
              { label:'Stat Arb P&L', value:`${saPnl>=0?'+':''}$${saPnl.toFixed(0)}`, color:saPnl>=0?'var(--green)':'var(--red)' },
              { label:'Open Pairs', value:`${saOpen.length}`, color:'var(--blue)' },
              { label:'Qualified Pairs', value:'12', color:'var(--t1)' },
              { label:'Model', value:'E-G + Kalman', color:'var(--purple)' },
            ].map((m,i) => (
              <div key={i} className="glass" style={{ padding:16 }}>
                <div style={{ fontSize:11, color:'var(--t2)', marginBottom:4, textTransform:'uppercase', letterSpacing:'.06em' }}>{m.label}</div>
                <div style={{ fontSize:22, fontWeight:800, color:m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
          <div className="glass" style={{ overflow:'hidden' }}>
            <div style={{ padding:'12px 16px', fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>
              Cointegrated Pairs — Kalman Spread Z-Score
            </div>
            <div className="divider" />
            {statArb.map((p,i) => {
              const zAbs = Math.abs(p.currentZ)
              const reverted = Math.abs(p.currentZ) < Math.abs(p.entryZ) * 0.5
              return (
                <div key={i} style={{ padding:'14px 16px', borderBottom:'1px solid var(--border)', display:'flex', gap:16, alignItems:'center' }}>
                  <div style={{ width:100 }}>
                    <div style={{ fontWeight:700, fontSize:13 }}>{p.pair}</div>
                    <span className={`badge badge-${p.dir===1?'green':'red'}`} style={{fontSize:9}}>{p.dir===1?'LONG SPREAD':'SHORT SPREAD'}</span>
                  </div>
                  <div style={{ flex:1 }}>
                    <div style={{ display:'flex', justifyContent:'space-between', fontSize:10, color:'var(--t2)', marginBottom:4 }}>
                      <span>Entry z={p.entryZ.toFixed(2)}</span>
                      <span>Current z={p.currentZ.toFixed(2)}</span>
                      <span>HL={p.hl.toFixed(1)}min</span>
                    </div>
                    <div style={{ height:6, background:'rgba(255,255,255,0.06)', borderRadius:3, overflow:'hidden' }}>
                      <div style={{ height:'100%', width:`${Math.min(100,(1-Math.abs(p.currentZ)/Math.abs(p.entryZ))*100)}%`,
                        background:`linear-gradient(90deg,var(--purple),var(--blue))`, borderRadius:3 }} />
                    </div>
                  </div>
                  <div style={{ textAlign:'right', width:80 }}>
                    <div style={{ fontSize:14, fontWeight:700, color:p.pnl>=0?'var(--green)':'var(--red)' }}>{p.pnl>=0?'+':''}${p.pnl}</div>
                    <div style={{ fontSize:10, color:'var(--t2)' }}>β={p.hedgeRatio.toFixed(3)}</div>
                  </div>
                  <div style={{ width:60, textAlign:'right' }}>
                    <span className={`badge badge-${p.status==='open'?reverted?'green':'blue':'grey'}`} style={{fontSize:9}}>
                      {p.status==='open'?reverted?'REVERTING':'OPEN':'CLOSED'}
                    </span>
                    <div style={{ fontSize:10, color:'var(--t3)', marginTop:3 }}>{p.age}</div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {subTab === 'alt_data' && (
        <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:12 }}>
            {[
              { label:'Actionable Signals', value:`${actionable}/${altData.length}`, color:'var(--blue)' },
              { label:'Bullish', value:`${altData.filter(d=>d.signal==='BULLISH').length}`, color:'var(--green)' },
              { label:'Bearish', value:`${altData.filter(d=>d.signal==='BEARISH').length}`, color:'var(--red)' },
              { label:'Sources Active', value:'4', color:'var(--purple)' },
            ].map((m,i) => (
              <div key={i} className="glass" style={{ padding:16 }}>
                <div style={{ fontSize:11, color:'var(--t2)', marginBottom:4, textTransform:'uppercase', letterSpacing:'.06em' }}>{m.label}</div>
                <div style={{ fontSize:22, fontWeight:800, color:m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
          <div className="glass" style={{ overflow:'hidden' }}>
            <div style={{ padding:'12px 16px', fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>
              Composite Alt-Data Signals — News · Social · Satellite · Earnings Whisper
            </div>
            <div className="divider" />
            {altData.map((d,i) => (
              <div key={i} style={{ padding:'12px 16px', borderBottom:'1px solid var(--border)', display:'flex', gap:16, alignItems:'center' }}>
                <div style={{ width:56, fontWeight:700 }}>{d.symbol}</div>
                <span className={`badge badge-${d.signal==='BULLISH'?'green':d.signal==='BEARISH'?'red':'yellow'}`} style={{width:68,justifyContent:'center'}}>{d.signal}</span>
                <div style={{ flex:1 }}>
                  <div style={{ display:'flex', gap:4 }}>
                    {d.breakdown.map((src,j) => (
                      <div key={j} style={{ flex:1, padding:'3px 6px', background:'rgba(255,255,255,0.04)', borderRadius:6, textAlign:'center' }}>
                        <div style={{ fontSize:9, color:'var(--t3)', textTransform:'uppercase' }}>{src.source.replace('_',' ').slice(0,8)}</div>
                        <div style={{ fontSize:11, fontWeight:600, color:src.score>0.1?'var(--green)':src.score<-0.1?'var(--red)':'var(--t2)' }}>
                          {src.score>=0?'+':''}{src.score}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
                <div style={{ width:60, textAlign:'right' }}>
                  <div style={{ fontSize:14, fontWeight:700, color:d.score>0?'var(--green)':d.score<0?'var(--red)':'var(--t2)' }}>
                    {d.score>=0?'+':''}{d.score.toFixed(2)}
                  </div>
                  <div style={{ fontSize:10, color:'var(--t3)' }}>{d.sources} src</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {subTab === 'cat' && (
        <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:12 }}>
            {[
              { label:'Events Today', value:`${cat.eventsToday.toLocaleString()}`, color:'var(--blue)' },
              { label:'Spoofing Alerts', value:`${cat.spoofingAlerts}`, color:'var(--yellow)' },
              { label:'Critical Escalations', value:`${cat.criticalEscalations}`, color:cat.criticalEscalations>0?'var(--red)':'var(--green)' },
              { label:'Held Symbols', value:`${cat.heldSymbols.length}`, color:cat.heldSymbols.length>0?'var(--red)':'var(--green)' },
            ].map((m,i) => (
              <div key={i} className="glass" style={{ padding:16 }}>
                <div style={{ fontSize:11, color:'var(--t2)', marginBottom:4, textTransform:'uppercase', letterSpacing:'.06em' }}>{m.label}</div>
                <div style={{ fontSize:22, fontWeight:800, color:m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
          <div className="glass" style={{ overflow:'hidden' }}>
            <div style={{ padding:'12px 16px', fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>
              Spoofing Detection — Cancel/Fill Ratio · Rapid Cancel Count
            </div>
            <div className="divider" />
            <div style={{ display:'grid', gridTemplateColumns:'80px 1fr 80px 80px 80px', padding:'8px 16px',
              fontSize:10, color:'var(--t3)', fontWeight:600, textTransform:'uppercase', letterSpacing:'.05em', gap:12 }}>
              <span>Symbol</span><span>Risk Meter</span><span>C/F Ratio</span><span>Rapid ×</span><span>Risk</span>
            </div>
            <div className="divider" />
            {cat.symbols.map((s,i) => {
              const riskColor = { NONE:'var(--green)', LOW:'var(--yellow)', MEDIUM:'var(--orange)', HIGH:'var(--red)', CRITICAL:'var(--red)' }[s.risk] || 'var(--t2)'
              const riskPct = { NONE:5, LOW:25, MEDIUM:55, HIGH:80, CRITICAL:100 }[s.risk] || 0
              return (
                <div key={i} style={{ display:'grid', gridTemplateColumns:'80px 1fr 80px 80px 80px',
                  padding:'12px 16px', borderBottom:'1px solid var(--border)', alignItems:'center', gap:12 }}>
                  <span style={{ fontWeight:700 }}>{s.symbol}</span>
                  <div>
                    <div style={{ height:6, background:'rgba(255,255,255,0.06)', borderRadius:3, overflow:'hidden' }}>
                      <div style={{ height:'100%', width:`${riskPct}%`, background:riskColor, borderRadius:3, transition:'width .8s' }} />
                    </div>
                  </div>
                  <span style={{ color:'var(--t2)', fontVariantNumeric:'tabular-nums' }}>{s.cfr.toFixed(1)}×</span>
                  <span style={{ color:'var(--t2)' }}>{s.rapidCancels}</span>
                  <span className={`badge badge-${s.risk==='NONE'?'green':s.risk==='LOW'?'yellow':s.risk==='HIGH'?'red':'purple'}`} style={{fontSize:9}}>{s.risk}</span>
                </div>
              )
            })}
          </div>
          <div className="glass" style={{ padding:16 }}>
            <div style={{ fontSize:11, color:'var(--t2)', marginBottom:12, textTransform:'uppercase', letterSpacing:'.06em' }}>Daily CAT Submission</div>
            <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:12 }}>
              {[
                { label:'Total Orders', value:cat.ordersTotal },
                { label:'Fills', value:cat.fills },
                { label:'Cancels', value:cat.cancels },
              ].map((m,i) => (
                <div key={i} style={{ padding:'10px 12px', background:'rgba(255,255,255,0.04)', borderRadius:10 }}>
                  <div style={{ fontSize:10, color:'var(--t2)', marginBottom:2 }}>{m.label}</div>
                  <div style={{ fontSize:18, fontWeight:700 }}>{m.value}</div>
                </div>
              ))}
            </div>
            <div style={{ marginTop:12, display:'flex', gap:8 }}>
              <span className="badge badge-green">Hash chain intact</span>
              <span className="badge badge-blue">FIX: ACTIVE seq=3847</span>
            </div>
          </div>
        </div>
      )}

      {subTab === 'backtest' && (
        <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:12 }}>
            {[
              { label:'Sharpe (mean)', value:`${backtest.sharpe.mean.toFixed(2)} ±${backtest.sharpe.stdev.toFixed(2)}`, color:'var(--green)' },
              { label:'Return (mean)', value:`+${backtest.total_return.mean_pct.toFixed(1)}%`, color:'var(--blue)' },
              { label:'Max Drawdown', value:`${backtest.max_drawdown.mean_pct.toFixed(1)}%`, color:'var(--yellow)' },
              { label:'Prob of Profit', value:`${backtest.prob_of_profit_pct.toFixed(1)}%`, color:'var(--green)' },
            ].map((m,i) => (
              <div key={i} className="glass" style={{ padding:16 }}>
                <div style={{ fontSize:11, color:'var(--t2)', marginBottom:4, textTransform:'uppercase', letterSpacing:'.06em' }}>{m.label}</div>
                <div style={{ fontSize:22, fontWeight:800, color:m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
            <div className="glass" style={{ padding:20 }}>
              <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:12 }}>
                Monte Carlo — 500 Simulations
              </div>
              <div style={{ marginBottom:12 }}>
                <div style={{ display:'flex', justifyContent:'space-between', fontSize:12, marginBottom:6 }}>
                  <span style={{ color:'var(--t2)' }}>Sharpe 95% CI</span>
                  <span style={{ color:'var(--t1)', fontWeight:600 }}>[{backtest.sharpe.ci_lo.toFixed(2)}, {backtest.sharpe.ci_hi.toFixed(2)}]</span>
                </div>
                <div style={{ display:'flex', justifyContent:'space-between', fontSize:12, marginBottom:6 }}>
                  <span style={{ color:'var(--t2)' }}>Return 95% CI</span>
                  <span style={{ color:'var(--t1)', fontWeight:600 }}>[{backtest.total_return.ci_lo_pct.toFixed(1)}%, {backtest.total_return.ci_hi_pct.toFixed(1)}%]</span>
                </div>
                <div style={{ display:'flex', justifyContent:'space-between', fontSize:12, marginBottom:6 }}>
                  <span style={{ color:'var(--t2)' }}>Worst DD (p100)</span>
                  <span style={{ color:'var(--red)', fontWeight:600 }}>{backtest.max_drawdown.worst_pct.toFixed(1)}%</span>
                </div>
                <div style={{ display:'flex', justifyContent:'space-between', fontSize:12 }}>
                  <span style={{ color:'var(--t2)' }}>CVaR (5% tail)</span>
                  <span style={{ color:'var(--red)', fontWeight:600 }}>{backtest.expected_shortfall_5pct.toFixed(1)}%</span>
                </div>
              </div>
              <div style={{ height:100 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={backtest.walk_forward}>
                    <XAxis dataKey="fold" tick={{fontSize:10, fill:'rgba(255,255,255,.4)'}} />
                    <YAxis hide />
                    <Tooltip contentStyle={{ background:'rgba(15,15,24,.95)', border:'1px solid rgba(255,255,255,.1)', borderRadius:8, fontSize:11 }}
                      formatter={(v,n)=>[v.toFixed(2),n]} />
                    <Bar dataKey="sharpe" fill="#32D74B" radius={[4,4,0,0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div style={{ fontSize:10, color:'var(--t3)', textAlign:'center', marginTop:4 }}>Sharpe per walk-forward fold</div>
            </div>
            <div className="glass" style={{ padding:20 }}>
              <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:12 }}>
                Walk-Forward Analysis
              </div>
              {backtest.walk_forward.map((f,i) => (
                <div key={i} style={{ display:'flex', gap:12, padding:'8px 0', borderBottom:'1px solid var(--border)', alignItems:'center' }}>
                  <span style={{ color:'var(--t2)', width:36, fontSize:12 }}>{f.fold}</span>
                  <div style={{ flex:1 }}>
                    <div style={{ height:4, background:'rgba(255,255,255,0.06)', borderRadius:2, overflow:'hidden' }}>
                      <div style={{ height:'100%', width:`${Math.min(100,Math.max(0,(f.sharpe/3)*100))}%`,
                        background:f.ret_pct>=0?'var(--green)':'var(--red)', borderRadius:2 }} />
                    </div>
                  </div>
                  <span style={{ fontSize:12, fontWeight:600, color:f.ret_pct>=0?'var(--green)':'var(--red)', width:56, textAlign:'right' }}>
                    {f.ret_pct>=0?'+':''}{f.ret_pct.toFixed(1)}%
                  </span>
                  <span style={{ fontSize:11, color:'var(--t2)', width:40 }}>S={f.sharpe.toFixed(2)}</span>
                  <span style={{ fontSize:11, color:'var(--yellow)', width:40 }}>DD={f.dd_pct.toFixed(1)}%</span>
                </div>
              ))}
              <div style={{ marginTop:12 }}>
                <span className="badge badge-blue">{backtest.n_simulations} Monte Carlo runs</span>
                <span className="badge badge-purple" style={{marginLeft:8}}>4-fold walk-forward</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Multi-Asset Tab ──────────────────────────────────────────────────────────
function MultiAssetTab() {
  const [subTab, setSubTab] = useState('fx')
  const fxPositions = makeFXPositions()
  const futPositions = makeFuturesPositions()
  const optPositions = makeOptionsPositions()
  const sandboxStrats = makeSandboxStrategies()

  const totalFxPnl  = fxPositions.reduce((s,p) => s + p.pnl, 0)
  const totalFutPnl = futPositions.reduce((s,p) => s + p.pnl, 0)
  const totalOptPnl = optPositions.reduce((s,p) => s + p.pnl, 0)
  const sandboxActive = sandboxStrats.filter(s => s.state === 'active').length
  const sandboxPnl   = sandboxStrats.reduce((s,p) => s + p.dailyPnl, 0)
  const netDelta = optPositions.reduce((s,p) => s + p.delta, 0)

  const stateColors = { active:'green', paused:'yellow', quarantined:'red', stopped:'grey' }

  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>

      {/* Header */}
      <div style={{ display:'flex', gap:12, alignItems:'center', flexWrap:'wrap' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>Multi-Asset Portfolio</h2>
        <span className="badge badge-blue">FX · Futures · Options</span>
        <span className="badge badge-purple">Quant Sandbox</span>
      </div>

      {/* Summary bar */}
      <div style={{ display:'grid', gridTemplateColumns:'repeat(5,1fr)', gap:12 }}>
        {[
          { label:'FX P&L',         value:`${totalFxPnl>=0?'+':''}$${totalFxPnl.toFixed(0)}`,  color:totalFxPnl>=0?'var(--green)':'var(--red)' },
          { label:'Futures P&L',    value:`${totalFutPnl>=0?'+':''}$${totalFutPnl.toFixed(0)}`, color:totalFutPnl>=0?'var(--green)':'var(--red)' },
          { label:'Options P&L',    value:`${totalOptPnl>=0?'+':''}$${totalOptPnl.toFixed(0)}`, color:totalOptPnl>=0?'var(--green)':'var(--red)' },
          { label:'Net Delta (opt)',value:netDelta.toFixed(0),                                  color:'var(--blue)' },
          { label:'Sandbox P&L',   value:`${sandboxPnl>=0?'+':''}$${sandboxPnl.toFixed(0)}`,   color:sandboxPnl>=0?'var(--green)':'var(--red)' },
        ].map((m,i) => (
          <div key={i} className="glass" style={{ padding:'14px 16px' }}>
            <div style={{ fontSize:10, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:4 }}>{m.label}</div>
            <div style={{ fontSize:20, fontWeight:800, color:m.color }}>{m.value}</div>
          </div>
        ))}
      </div>

      {/* Sub-tab bar */}
      <div className="tab-bar">
        {['fx','futures','options','sandbox'].map(t => (
          <button key={t} className={`tab${subTab===t?' active':''}`} onClick={()=>setSubTab(t)}>
            {t==='fx'?'FX Spot':t==='futures'?'Futures':t==='options'?'Options (Greeks)':'Quant Sandbox'}
          </button>
        ))}
      </div>

      {/* FX Positions */}
      {subTab === 'fx' && (
        <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
          <div className="glass" style={{ overflow:'hidden' }}>
            <div style={{ display:'grid', gridTemplateColumns:'110px 60px 90px 90px 80px 60px 80px 80px',
              padding:'10px 16px', gap:12, background:'rgba(255,255,255,0.03)',
              fontSize:11, color:'var(--t2)', fontWeight:600, textTransform:'uppercase', letterSpacing:'.04em' }}>
              {['Pair','Lots','Entry','Current','P&L','Pips','Pip Val','Margin'].map(h=><span key={h}>{h}</span>)}
            </div>
            <div className="divider" />
            {fxPositions.map((p,i) => (
              <div key={i} style={{ display:'grid', gridTemplateColumns:'110px 60px 90px 90px 80px 60px 80px 80px',
                padding:'12px 16px', gap:12, borderBottom:'1px solid var(--border)', alignItems:'center',
                animation:`slide-up .2s ${i*.04}s both` }}>
                <span style={{ fontSize:13, fontWeight:700, color:'var(--blue)' }}>{p.pair}</span>
                <span style={{ fontSize:12 }}>{p.lots}</span>
                <span style={{ fontSize:12, fontVariantNumeric:'tabular-nums' }}>{p.entry.toFixed(4)}</span>
                <span style={{ fontSize:12, fontVariantNumeric:'tabular-nums' }}>{p.current.toFixed(4)}</span>
                <span style={{ fontSize:13, fontWeight:700, color:p.pnl>=0?'var(--green)':'var(--red)' }}>
                  {p.pnl>=0?'+':''}${p.pnl.toFixed(0)}
                </span>
                <span style={{ fontSize:12, color:p.pips>=0?'var(--green)':'var(--red)' }}>
                  {p.pips>=0?'+':''}{p.pips.toFixed(1)}
                </span>
                <span style={{ fontSize:12, color:'var(--t2)' }}>${p.pipValue.toFixed(2)}</span>
                <span style={{ fontSize:12, color:'var(--t2)' }}>${p.margin.toFixed(0)}</span>
              </div>
            ))}
          </div>
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:12 }}>
            <div className="glass" style={{ padding:16 }}>
              <div style={{ fontSize:11, color:'var(--t2)', marginBottom:8, textTransform:'uppercase', letterSpacing:'.06em' }}>Interest Rate Parity</div>
              <p style={{ fontSize:12, color:'var(--t2)', lineHeight:1.6 }}>
                Forward rate F = S × e<sup>(r<sub>q</sub>−r<sub>b</sub>)T</sup> · Swap points displayed per-pair based on carry differential. USD/JPY long positive carry; EUR/USD long negative carry.
              </p>
            </div>
            <div className="glass" style={{ padding:16 }}>
              <div style={{ fontSize:11, color:'var(--t2)', marginBottom:8, textTransform:'uppercase', letterSpacing:'.06em' }}>Margin Summary</div>
              <div style={{ fontSize:20, fontWeight:800, color:'var(--yellow)' }}>
                ${fxPositions.reduce((s,p)=>s+p.margin,0).toFixed(0)}
              </div>
              <div style={{ fontSize:11, color:'var(--t2)', marginTop:4 }}>Total FX margin requirement (2% per lot)</div>
            </div>
          </div>
        </div>
      )}

      {/* Futures Positions */}
      {subTab === 'futures' && (
        <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
          <div className="glass" style={{ overflow:'hidden' }}>
            <div style={{ display:'grid', gridTemplateColumns:'60px 160px 80px 90px 90px 80px 90px 90px',
              padding:'10px 16px', gap:12, background:'rgba(255,255,255,0.03)',
              fontSize:11, color:'var(--t2)', fontWeight:600, textTransform:'uppercase', letterSpacing:'.04em' }}>
              {['Sym','Name','Exchange','Ctrs','Entry','Current','P&L','Margin'].map(h=><span key={h}>{h}</span>)}
            </div>
            <div className="divider" />
            {futPositions.map((p,i) => (
              <div key={i} style={{ display:'grid', gridTemplateColumns:'60px 160px 80px 90px 90px 90px 80px 90px',
                padding:'12px 16px', gap:12, borderBottom:'1px solid var(--border)', alignItems:'center',
                animation:`slide-up .2s ${i*.04}s both` }}>
                <span style={{ fontSize:13, fontWeight:700, color:'var(--yellow)' }}>{p.symbol}</span>
                <span style={{ fontSize:11, color:'var(--t2)' }}>{p.name}</span>
                <span className="badge badge-blue" style={{ fontSize:9 }}>{p.exchange}</span>
                <span style={{ fontSize:13, fontWeight:700 }}>{p.contracts}</span>
                <span style={{ fontSize:12, fontVariantNumeric:'tabular-nums' }}>{p.entry.toFixed(2)}</span>
                <span style={{ fontSize:12, fontVariantNumeric:'tabular-nums' }}>{p.current.toFixed(2)}</span>
                <span style={{ fontSize:13, fontWeight:700, color:p.pnl>=0?'var(--green)':'var(--red)' }}>
                  {p.pnl>=0?'+':''}${p.pnl.toFixed(0)}
                </span>
                <span style={{ fontSize:12, color:'var(--t2)' }}>${p.margin.toLocaleString()}</span>
              </div>
            ))}
          </div>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:12 }}>
            {[
              { label:'Total Futures P&L', value:`${totalFutPnl>=0?'+':''}$${totalFutPnl.toFixed(0)}`, color:totalFutPnl>=0?'var(--green)':'var(--red)' },
              { label:'Total Margin',      value:`$${futPositions.reduce((s,p)=>s+p.margin,0).toLocaleString()}`, color:'var(--yellow)' },
              { label:'Net Notional',      value:`$${(futPositions.reduce((s,p)=>s+p.notional,0)/1000).toFixed(0)}k`, color:'var(--blue)' },
            ].map((m,i) => (
              <div key={i} className="glass" style={{ padding:16 }}>
                <div style={{ fontSize:10, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:4 }}>{m.label}</div>
                <div style={{ fontSize:22, fontWeight:800, color:m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Options Greeks */}
      {subTab === 'options' && (
        <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
          <div className="glass" style={{ overflow:'hidden' }}>
            <div style={{ display:'grid', gridTemplateColumns:'80px 60px 60px 60px 70px 80px 80px 60px 80px',
              padding:'10px 16px', gap:10, background:'rgba(255,255,255,0.03)',
              fontSize:11, color:'var(--t2)', fontWeight:600, textTransform:'uppercase', letterSpacing:'.04em' }}>
              {['Underlying','Strike','Exp','Right','Ctrs','Entry','Current','P&L','Δ Delta'].map(h=><span key={h}>{h}</span>)}
            </div>
            <div className="divider" />
            {optPositions.map((p,i) => (
              <div key={i} style={{ display:'grid', gridTemplateColumns:'80px 60px 60px 60px 70px 80px 80px 60px 80px',
                padding:'12px 16px', gap:10, borderBottom:'1px solid var(--border)', alignItems:'center',
                animation:`slide-up .2s ${i*.04}s both` }}>
                <span style={{ fontSize:13, fontWeight:700 }}>{p.underlying}</span>
                <span style={{ fontSize:12 }}>${p.strike}</span>
                <span style={{ fontSize:11, color:'var(--t2)' }}>{p.expDays}d</span>
                <span className={`badge badge-${p.right==='call'?'green':'red'}`} style={{ fontSize:9, textTransform:'uppercase' }}>{p.right}</span>
                <span style={{ fontSize:13, fontWeight:700, color:p.contracts>0?'var(--green)':'var(--red)' }}>{p.contracts}</span>
                <span style={{ fontSize:12, fontVariantNumeric:'tabular-nums' }}>${p.entryPrem.toFixed(2)}</span>
                <span style={{ fontSize:12, fontVariantNumeric:'tabular-nums' }}>${p.currentPrem.toFixed(2)}</span>
                <span style={{ fontSize:13, fontWeight:700, color:p.pnl>=0?'var(--green)':'var(--red)' }}>
                  {p.pnl>=0?'+':''}${p.pnl.toFixed(0)}
                </span>
                <span style={{ fontSize:12, color:'var(--blue)', fontWeight:600 }}>{p.delta.toFixed(1)}</span>
              </div>
            ))}
          </div>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:12 }}>
            {[
              { label:'Net Delta',     value:netDelta.toFixed(1),                              color:'var(--blue)' },
              { label:'Total Options P&L', value:`${totalOptPnl>=0?'+':''}$${totalOptPnl.toFixed(0)}`, color:totalOptPnl>=0?'var(--green)':'var(--red)' },
              { label:'Long Positions', value:optPositions.filter(p=>p.contracts>0).length,    color:'var(--green)' },
              { label:'Short Positions',value:optPositions.filter(p=>p.contracts<0).length,    color:'var(--red)' },
            ].map((m,i) => (
              <div key={i} className="glass" style={{ padding:16 }}>
                <div style={{ fontSize:10, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:4 }}>{m.label}</div>
                <div style={{ fontSize:22, fontWeight:800, color:m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
          <div className="glass" style={{ padding:14 }}>
            <div style={{ fontSize:11, color:'var(--t2)', marginBottom:8, textTransform:'uppercase', letterSpacing:'.06em' }}>Black-Scholes Model</div>
            <p style={{ fontSize:12, color:'var(--t2)', lineHeight:1.6 }}>
              European vanilla pricing · Delta = ∂V/∂S · Vega per 1pp vol · Theta per calendar day · IV solved via Newton-Raphson (max error {'<'} 1e-6) · Put-call parity enforced
            </p>
          </div>
        </div>
      )}

      {/* Quant Sandbox */}
      {subTab === 'sandbox' && (
        <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
          <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:12 }}>
            {[
              { label:'Active Strategies', value:sandboxActive,                          color:'var(--green)' },
              { label:'Total Registered',  value:sandboxStrats.length,                  color:'var(--blue)' },
              { label:'Sandbox P&L',       value:`${sandboxPnl>=0?'+':''}$${sandboxPnl.toFixed(0)}`, color:sandboxPnl>=0?'var(--green)':'var(--red)' },
              { label:'Quarantined',       value:sandboxStrats.filter(s=>s.state==='quarantined').length, color:'var(--red)' },
            ].map((m,i) => (
              <div key={i} className="glass" style={{ padding:16 }}>
                <div style={{ fontSize:10, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:4 }}>{m.label}</div>
                <div style={{ fontSize:22, fontWeight:800, color:m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
          <div className="glass" style={{ overflow:'hidden' }}>
            <div style={{ display:'grid', gridTemplateColumns:'140px 60px 180px 80px 70px 80px 60px',
              padding:'10px 16px', gap:12, background:'rgba(255,255,255,0.03)',
              fontSize:11, color:'var(--t2)', fontWeight:600, textTransform:'uppercase', letterSpacing:'.04em' }}>
              {['Name','Ver','Symbols','State','Daily P&L','Win Rate','Signals'].map(h=><span key={h}>{h}</span>)}
            </div>
            <div className="divider" />
            {sandboxStrats.map((s,i) => (
              <div key={i} style={{ display:'grid', gridTemplateColumns:'140px 60px 180px 80px 70px 80px 60px',
                padding:'12px 16px', gap:12, borderBottom:'1px solid var(--border)', alignItems:'center',
                animation:`slide-up .2s ${i*.04}s both`,
                background: s.state==='quarantined' ? 'rgba(255,69,58,0.05)' : 'transparent' }}>
                <span style={{ fontSize:12, fontWeight:700 }}>{s.name}</span>
                <span style={{ fontSize:11, color:'var(--t3)' }}>v{s.version}</span>
                <span style={{ fontSize:10, color:'var(--t2)' }}>{s.symbols.join(' · ')}</span>
                <span className={`badge badge-${stateColors[s.state]||'grey'}`} style={{ fontSize:9 }}>{s.state.toUpperCase()}</span>
                <span style={{ fontSize:13, fontWeight:700, color:s.dailyPnl>=0?'var(--green)':'var(--red)' }}>
                  {s.dailyPnl>=0?'+':''}${s.dailyPnl.toFixed(0)}
                </span>
                <span style={{ fontSize:12, color:(s.winRate||0)>=0.55?'var(--green)':'var(--yellow)' }}>
                  {((s.winRate||0)*100).toFixed(0)}%
                </span>
                <span style={{ fontSize:12, color:'var(--t2)' }}>{s.signals}</span>
              </div>
            ))}
          </div>
          <div className="glass" style={{ padding:16 }}>
            <div style={{ fontSize:11, color:'var(--t2)', marginBottom:8, textTransform:'uppercase', letterSpacing:'.06em' }}>Sandbox Architecture</div>
            <div style={{ display:'flex', flexWrap:'wrap', gap:8 }}>
              {['Isolated P&L book','Daily loss cap per strategy','Max drawdown quarantine','Hot-reload (no restart)','Signal rate limiter','Risk-limit enforcement','Consumer broadcast'].map(f => (
                <span key={f} className="badge badge-blue" style={{ fontSize:10 }}>{f}</span>
              ))}
            </div>
          </div>
        </div>
      )}

    </div>
  )
}

// ── Audit Tab ────────────────────────────────────────────────────────────────
function AuditTab({ audit }) {
  const [filter, setFilter] = useState('ALL')
  const types = ['ALL','ORDER','RISK','SIGNAL','REGIME','TRADE','MM','STAT','CAT']
  const filtered = filter === 'ALL' ? audit : audit.filter(e => e.t.includes(filter))
  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      <div style={{ display:'flex', gap:12, alignItems:'center' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>Immutable Audit Log</h2>
        <span className="badge badge-green">Append-only · SHA-256 chained</span>
      </div>
      <div className="tab-bar">
        {types.map(t => <button key={t} className={`tab${filter===t?' active':''}`} onClick={()=>setFilter(t)}>{t}</button>)}
      </div>
      <div className="glass" style={{ overflow:'hidden' }}>
        <div style={{ display:'grid', gridTemplateColumns:'80px 12px 160px 1fr', padding:'10px 16px', gap:12,
          background:'rgba(255,255,255,0.03)', fontSize:11, color:'var(--t2)', fontWeight:600, textTransform:'uppercase', letterSpacing:'.05em' }}>
          <span>Time</span><span></span><span>Event Type</span><span>Detail</span>
        </div>
        <div className="divider" />
        {filtered.map((e,i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'80px 12px 160px 1fr', padding:'10px 16px',
            borderBottom:'1px solid var(--border)', gap:12, alignItems:'center', animation:`slide-up .3s ${i*.02}s both` }}>
            <div style={{ fontSize:11, color:'var(--t3)', fontVariantNumeric:'tabular-nums' }}>{e.ts}</div>
            <div className={`sdot sdot-${e.c}`} />
            <div style={{ fontSize:11, fontWeight:600, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.04em' }}>{e.t}</div>
            <div style={{ fontSize:13 }}>{e.msg}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Health Tab ───────────────────────────────────────────────────────────────
function HealthTab({ health, tick }) {
  const metrics = [
    { label:'CPU',             value:health.cpu,         unit:'%',  color:'var(--blue)',   hist:health.cpu_h,  warn:70,  crit:90 },
    { label:'Memory',          value:health.mem,         unit:'%',  color:'var(--purple)', hist:health.mem_h,  warn:75,  crit:90 },
    { label:'Event Loop Lag',  value:health.eventLoop,   unit:'ms', color:'var(--yellow)', hist:health.el_h,   warn:50,  crit:200 },
    { label:'Bus Latency',     value:health.busLatency,  unit:'ms', color:'var(--green)',  hist:null,          warn:100, crit:500 },
  ]
  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      <div style={{ display:'flex', gap:12, alignItems:'center' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>System Health</h2>
        <span className="badge badge-green"><div className="sdot sdot-green sdot-pulse" />All Nominal</span>
      </div>
      <div style={{ display:'grid', gridTemplateColumns:'repeat(2,1fr)', gap:16 }}>
        {metrics.map((m,i) => {
          const sev = m.value >= m.crit ? 'red' : m.value >= m.warn ? 'yellow' : 'green'
          const sevColor = sev==='red'?'var(--red)':sev==='yellow'?'var(--yellow)':'var(--green)'
          return (
            <div key={i} className="glass" style={{ padding:20, animation:`slide-up .35s ${i*.1}s both` }}>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start', marginBottom:12 }}>
                <div>
                  <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:4 }}>{m.label}</div>
                  <div style={{ fontSize:32, fontWeight:800, color:sevColor, letterSpacing:'-0.02em' }}>
                    {m.value.toFixed(1)}<span style={{fontSize:14,fontWeight:500,color:'var(--t2)',marginLeft:2}}>{m.unit}</span>
                  </div>
                </div>
                <span className={`badge badge-${sev}`}>{sev.toUpperCase()}</span>
              </div>
              {m.hist && <Spark data={m.hist} color={sevColor} h={48} dataKey="v" />}
              <div style={{ height:4, background:'rgba(255,255,255,0.06)', borderRadius:2, overflow:'hidden', marginTop:8 }}>
                <div style={{ height:'100%', width:`${Math.min(100,(m.value/m.crit)*100)}%`, background:sevColor, borderRadius:2, transition:'width .8s' }} />
              </div>
              <div style={{ display:'flex', justifyContent:'space-between', fontSize:10, color:'var(--t3)', marginTop:4 }}>
                <span>0</span><span>Warn: {m.warn}{m.unit}</span><span>Crit: {m.crit}{m.unit}</span>
              </div>
            </div>
          )
        })}
      </div>
      <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:16 }}>
        {[
          { label:'WebSocket', value:health.wsOk?'CONNECTED':'DISCONNECTED', color:health.wsOk?'var(--green)':'var(--red)', icon:'📡' },
          { label:'FIX Session', value:health.fix.state, color:'var(--green)', icon:'⚡', sub:`seq=${health.fix.out_seq}` },
          { label:'Latency Tier', value:health.latency.tier, color:'var(--purple)', icon:'⏱', sub:`p99=${(health.latency.p99_us/1000).toFixed(1)}ms` },
        ].map((s,i) => (
          <div key={i} className="glass" style={{ padding:20, textAlign:'center' }}>
            <div style={{ fontSize:28, marginBottom:8 }}>{s.icon}</div>
            <div style={{ fontSize:11, color:'var(--t2)', marginBottom:4 }}>{s.label}</div>
            <div style={{ fontSize:16, fontWeight:700, color:s.color }}>{s.value}</div>
            {s.sub && <div style={{ fontSize:10, color:'var(--t3)', marginTop:2 }}>{s.sub}</div>}
          </div>
        ))}
      </div>
      <div className="glass" style={{ padding:20 }}>
        <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:12 }}>Latency Budget — {health.latency.tier}</div>
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr 1fr', gap:12 }}>
          {[
            { label:'p50', value:`${(health.latency.p50_us/1000).toFixed(2)}ms`, sub:`budget ${(health.latency.budget_us/1000).toFixed(0)}ms` },
            { label:'p99', value:`${(health.latency.p99_us/1000).toFixed(2)}ms`, color: health.latency.p99_us > health.latency.budget_us ? 'var(--red)':'var(--green)' },
            { label:'Breach Rate', value:`${(health.latency.breach_rate*100).toFixed(1)}%`, color:health.latency.breach_rate>0.05?'var(--yellow)':'var(--green)' },
            { label:'CAT Events', value:health.cat.events.toLocaleString(), sub:`${health.cat.alerts} alerts` },
          ].map((m,i) => (
            <div key={i} style={{ padding:'12px 14px', background:'rgba(255,255,255,0.04)', borderRadius:12 }}>
              <div style={{ fontSize:10, color:'var(--t2)', marginBottom:4 }}>{m.label}</div>
              <div style={{ fontSize:18, fontWeight:700, color:m.color||'var(--t1)' }}>{m.value}</div>
              {m.sub && <div style={{ fontSize:10, color:'var(--t3)', marginTop:2 }}>{m.sub}</div>}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
