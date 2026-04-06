import { useState, useEffect, useRef, useCallback } from 'react'
import {
  AreaChart, Area, LineChart, Line, BarChart, Bar,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid
} from 'recharts'
import { AGENTS, makeLivePnl, makePipeline, makePositions, makeAuditLog, makeHealth } from './data'
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
  const [armed, setArmed] = useState(false)
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
          This will <strong style={{color:'var(--t1)'}}>immediately cancel all orders</strong> and <strong style={{color:'var(--t1)'}}>flatten all positions</strong> via direct broker API, bypassing the message bus.
        </p>
        <div style={{ background:'rgba(255,69,58,0.08)', border:'1px solid rgba(255,69,58,0.2)', borderRadius:12, padding:16, marginBottom:20, textAlign:'left' }}>
          <p style={{ fontSize:12, color:'var(--red)', marginBottom:10, fontWeight:600, letterSpacing:'.04em', textTransform:'uppercase' }}>SLO Guarantees</p>
          {[['ACK response', '≤ 250ms'],['PSA state transition','≤ 500ms'],['Cancel dispatch','≤ 1s'],['Full flatten','≤ 10s p95']].map(([k,v]) => (
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
            onClick={() => { alert('KILL SWITCH TRIGGERED — positions being flattened'); onClose(); }}>
            🔴 EXECUTE FLATTEN
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Agent Card ──────────────────────────────────────────────────────────────
function AgentCard({ agent, status }) {
  const colors = { active:'green', warning:'yellow', error:'red', idle:'grey' }
  const c = colors[status] || 'grey'
  const labels = { active:'ACTIVE', warning:'WARN', error:'ERROR', idle:'IDLE' }
  return (
    <div className="glass-sm" style={{ padding:'12px 14px', display:'flex', alignItems:'center', gap:12,
      cursor:'default', transition:'all .18s', borderColor: status === 'active' ? 'rgba(50,215,75,0.15)' : 'var(--border)' }}>
      <div style={{ width:36, height:36, borderRadius:10, background:agent.color+'22',
        border:`1px solid ${agent.color}33`, display:'flex', alignItems:'center', justifyContent:'center',
        fontSize:11, fontWeight:700, color:agent.color, flexShrink:0 }}>{agent.abbr.slice(0,3)}</div>
      <div style={{ flex:1, minWidth:0 }}>
        <div style={{ fontSize:12, fontWeight:600, color:'var(--t1)', whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{agent.name}</div>
        <div style={{ fontSize:10, color:'var(--t2)', whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{agent.desc}</div>
      </div>
      <div style={{ display:'flex', alignItems:'center', gap:5, flexShrink:0 }}>
        <div className={`sdot sdot-${c}${status === 'active' ? ' sdot-pulse' : ''}`}></div>
        <span style={{ fontSize:10, fontWeight:600, color: status === 'active' ? 'var(--green)' : status === 'error' ? 'var(--red)' : 'var(--t2)', letterSpacing:'.04em' }}>{labels[status] || 'IDLE'}</span>
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
          <div style={{ height:'100%', width:`${progress}%`, background:'linear-gradient(90deg, var(--green), #30d158)', borderRadius:2, transition:'width 1s' }}></div>
        </div>
      </div>
      <div style={{ textAlign:'right', width:80 }}>
        <div style={{ fontSize:14, fontWeight:700, color: pos.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
          {pos.pnl >= 0 ? '+' : ''}${pos.pnl.toFixed(0)}
        </div>
        <div style={{ fontSize:11, color:'var(--t2)' }}>{pct}%</div>
      </div>
      <div style={{ textAlign:'right', fontSize:11, color:'var(--t2)', width:48 }}>
        <div>{pos.shares}sh</div>
        <div>${pos.current.toFixed(2)}</div>
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
              fontSize:18, boxShadow:'0 0 20px rgba(50,215,75,0.1)' }}>{s.icon}</div>
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

  const agentStatuses = AGENTS.map((a, i) => {
    if (i >= 17) return 'active'
    if (i === 6 || i === 13) return 'active'
    return i < 16 ? 'active' : 'idle'
  })

  const positions = makePositions()
  const audit = makeAuditLog()
  const pipeline = makePipeline()
  const health = makeHealth()
  const totalPnl = positions.reduce((s, p) => s + p.pnl, 0) + 1847
  const pnlPositive = totalPnl >= 0

  const etTime = new Date(time.toLocaleString('en-US', { timeZone: 'America/New_York' }))
  const isMarketOpen = etTime.getHours() >= 9 && (etTime.getHours() < 16 || (etTime.getHours() === 9 && etTime.getMinutes() >= 30))
  const timeStr = time.toLocaleTimeString('en-US', { hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit' })

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
            <div style={{ fontSize:10, color:'var(--t2)' }}>19-Agent Cooperative AI</div>
          </div>
        </div>

        <div style={{ flex:1 }} />

        <div className="tab-bar" style={{ display:'flex' }}>
          {['dashboard','agents','positions','audit','health'].map(t => (
            <button key={t} className={`tab${tab===t?' active':''}`} onClick={()=>setTab(t)}>
              {t.charAt(0).toUpperCase()+t.slice(1)}
            </button>
          ))}
        </div>

        <div style={{ flex:1 }} />

        <div style={{ display:'flex', alignItems:'center', gap:12 }}>
          <div style={{ textAlign:'right' }}>
            <div style={{ fontSize:12, fontWeight:700, fontVariantNumeric:'tabular-nums' }}>{timeStr}</div>
            <div style={{ fontSize:10, color:'var(--t2)' }}>UTC {new Date().getTimezoneOffset() <= 0 ? '+' : '-'}{Math.abs(new Date().getTimezoneOffset()/60)}</div>
          </div>

          <span className={`badge ${isMarketOpen ? 'badge-green' : 'badge-yellow'}`}>
            <div className={`sdot sdot-${isMarketOpen?'green':'yellow'}${isMarketOpen?' sdot-pulse':''}`}></div>
            {isMarketOpen ? 'MARKET OPEN' : 'PRE-SESSION'}
          </span>

          <span className="badge badge-blue">
            <div className="sdot sdot-blue sdot-pulse"></div>
            ACTIVE
          </span>

          <button style={{ width:40, height:40, borderRadius:12, background:'rgba(255,69,58,0.12)',
            border:'1.5px solid rgba(255,69,58,0.35)', cursor:'pointer', fontSize:16,
            display:'flex',alignItems:'center',justifyContent:'center',
            transition:'all .2s', boxShadow:'0 0 16px rgba(255,69,58,0.2)' }}
            onClick={() => setKsOpen(true)}
            title="Kill Switch — Emergency Flatten">⚡</button>
        </div>
      </div>

      {/* ── Content ── */}
      <div style={{ flex:1, overflow:'hidden', display:'flex' }}>

        {tab === 'dashboard' && <DashboardTab pnlData={pnlData} totalPnl={totalPnl} positions={positions} audit={audit} pipeline={pipeline} health={health} tick={tick} />}
        {tab === 'agents' && <AgentsTab agents={AGENTS} statuses={agentStatuses} />}
        {tab === 'positions' && <PositionsTab positions={positions} />}
        {tab === 'audit' && <AuditTab audit={audit} />}
        {tab === 'health' && <HealthTab health={health} tick={tick} />}

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
  const tradeCount = 14
  const equity = 30000 + totalPnl

  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>

      {/* ── Top metrics row ── */}
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr 1fr', gap:16 }}>
        {[
          { label:'Session P&L', value:`${pnlPos?'+':''}$${Math.abs(totalPnl).toLocaleString()}`, color:pnlPos?'var(--green)':'var(--red)', sub:'Today' },
          { label:'Account Equity', value:`$${equity.toLocaleString(undefined,{minimumFractionDigits:0,maximumFractionDigits:0})}`, color:'var(--t1)', sub:'Paper account' },
          { label:'Win Rate', value:`${winRate}%`, color:'var(--blue)', sub:`${tradeCount} trades today` },
          { label:'Max Drawdown', value:`${maxDD}%`, color:'var(--yellow)', sub:'vs 2.0% cap' },
        ].map((m, i) => (
          <div key={i} className="glass" style={{ padding:20, animation:`slide-up .4s ${i*0.08}s both` }}>
            <div style={{ fontSize:11, color:'var(--t2)', fontWeight:500, marginBottom:6, textTransform:'uppercase', letterSpacing:'.06em' }}>{m.label}</div>
            <div style={{ fontSize:28, fontWeight:800, color:m.color, letterSpacing:'-0.02em', lineHeight:1 }}>{m.value}</div>
            <div style={{ fontSize:11, color:'var(--t3)', marginTop:4 }}>{m.sub}</div>
          </div>
        ))}
      </div>

      {/* ── P&L Chart + Rings ── */}
      <div style={{ display:'grid', gridTemplateColumns:'1fr 320px', gap:16 }}>

        {/* P&L Chart */}
        <div className="glass" style={{ padding:20 }}>
          <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:16 }}>
            <div>
              <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:2 }}>Intraday P&L Curve</div>
              <div style={{ fontSize:22, fontWeight:800, color:pnlPos?'var(--green)':'var(--red)' }}>
                {pnlPos?'+':''}${Math.abs(totalPnl).toLocaleString()}
              </div>
            </div>
            <div style={{ display:'flex', gap:8 }}>
              <span className="badge badge-green">Session Active</span>
            </div>
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
              <XAxis dataKey="t" hide />
              <YAxis hide />
              <Tooltip contentStyle={{ background:'rgba(15,15,24,0.9)', border:'1px solid rgba(255,255,255,0.1)', borderRadius:10, fontSize:12 }}
                formatter={(v) => [`$${v.toFixed(0)}`, 'P&L']} labelFormatter={() => ''} />
              <Area type="monotone" dataKey="v" stroke={pnlPos?'#32D74B':'#FF453A'}
                strokeWidth={2} fill="url(#pnlGrad)" dot={false} animationDuration={300} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* Stats rings */}
        <div className="glass" style={{ padding:20, display:'flex', flexDirection:'column', gap:16 }}>
          <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>Session Metrics</div>
          <div style={{ display:'flex', justifyContent:'space-around', alignItems:'center', flex:1 }}>
            <div style={{ textAlign:'center' }}>
              <Ring pct={winRate} color="var(--green)" size={80} label={`${winRate}%`} sub="Win rate" />
            </div>
            <div style={{ textAlign:'center' }}>
              <Ring pct={(maxDD/2)*100} color="var(--yellow)" size={80} label={`${maxDD}%`} sub="Drawdown" />
            </div>
          </div>
          <div className="divider" />
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:8 }}>
            {[
              { label:'Open Positions', value:'3', color:'var(--blue)' },
              { label:'Trades Today', value:'14', color:'var(--t1)' },
              { label:'Risk Used', value:'62%', color:'var(--yellow)' },
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

      {/* ── Pipeline + Positions + Audit ── */}
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap:16 }}>

        {/* Pipeline */}
        <div className="glass" style={{ padding:20 }}>
          <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em', marginBottom:12 }}>Live Pipeline</div>
          <PipelineFlow steps={pipeline} />
          <div className="divider" style={{ margin:'12px 0' }} />
          <div style={{ fontSize:11, color:'var(--t2)', marginBottom:6 }}>State Machine</div>
          <div style={{ display:'flex', gap:6, flexWrap:'wrap' }}>
            {['BOOTING','DATA_CHECK','REGIME','ACTIVE','PAUSED','HALTED'].map((s,i) => (
              <span key={s} className={`badge ${s==='ACTIVE'?'badge-green':i<3?'badge-blue':'badge-yellow'}`} style={{ fontSize:9 }}>{s}</span>
            ))}
          </div>
          <div style={{ marginTop:8 }}>
            <span className="badge badge-green" style={{ fontSize:10 }}>
              <div className="sdot sdot-green sdot-pulse"></div>
              Currently: ACTIVE
            </span>
          </div>
        </div>

        {/* Open positions */}
        <div className="glass" style={{ overflow:'hidden' }}>
          <div style={{ padding:'16px 16px 12px', display:'flex', justifyContent:'space-between', alignItems:'center' }}>
            <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>Open Positions</div>
            <span className="badge badge-blue">{positions.length}</span>
          </div>
          <div className="divider" />
          {positions.map((p,i) => <PositionRow key={i} pos={p} />)}
        </div>

        {/* Audit log */}
        <div className="glass" style={{ overflow:'hidden', display:'flex', flexDirection:'column' }}>
          <div style={{ padding:'16px 16px 12px', display:'flex', justifyContent:'space-between', alignItems:'center', flexShrink:0 }}>
            <div style={{ fontSize:11, color:'var(--t2)', textTransform:'uppercase', letterSpacing:'.06em' }}>Audit Log</div>
            <div className="sdot sdot-green sdot-pulse"></div>
          </div>
          <div className="divider" />
          <div style={{ flex:1, overflow:'auto', padding:'0 16px' }}>
            {audit.map((e,i) => <AuditEntry key={i} entry={e} delay={i*0.03} />)}
          </div>
        </div>

      </div>
    </div>
  )
}

// ── Agents Tab ───────────────────────────────────────────────────────────────
function AgentsTab({ agents, statuses }) {
  const active = statuses.filter(s=>s==='active').length
  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      <div style={{ display:'flex', gap:12, alignItems:'center' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>Agent Monitor</h2>
        <span className="badge badge-green">{active}/19 Active</span>
      </div>
      <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))', gap:8 }}>
        {agents.map((a,i) => <AgentCard key={i} agent={a} status={statuses[i]} />)}
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
          <span>Symbol</span><span>Progress (Stop → Target)</span><span>Entry</span><span>Current</span><span>P&L</span><span>Shares</span>
        </div>
        <div className="divider" />
        {positions.map((p,i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'60px 1fr 120px 120px 100px 80px',
            padding:'14px 16px', borderBottom:'1px solid var(--border)', alignItems:'center', gap:16 }}>
            <div>
              <div style={{ fontWeight:700 }}>{p.symbol}</div>
              <div style={{ fontSize:10 }}><span className={`badge badge-${p.direction==='LONG'?'green':'red'}`} style={{fontSize:9}}>{p.direction}</span></div>
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

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap:16 }}>
        {[
          { label:'Total Unrealized P&L', value:`+$${totalPnl.toFixed(0)}`, color:'var(--green)' },
          { label:'Max Concurrent (cap 5)', value:'3 / 5', color:'var(--blue)' },
          { label:'Largest Position Risk', value:'$127 (0.42%)', color:'var(--yellow)' },
        ].map((s,i) => (
          <div key={i} className="glass" style={{ padding:16 }}>
            <div style={{ fontSize:11, color:'var(--t2)', marginBottom:4 }}>{s.label}</div>
            <div style={{ fontSize:20, fontWeight:700, color:s.color }}>{s.value}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Audit Tab ────────────────────────────────────────────────────────────────
function AuditTab({ audit }) {
  const [filter, setFilter] = useState('ALL')
  const types = ['ALL','ORDER','RISK','SIGNAL','REGIME','TRADE','SYSTEM']
  const filtered = filter === 'ALL' ? audit : audit.filter(e => e.t.includes(filter))
  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      <div style={{ display:'flex', gap:12, alignItems:'center' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>Immutable Audit Log</h2>
        <span className="badge badge-green">Append-only</span>
      </div>
      <div className="tab-bar">
        {types.map(t => <button key={t} className={`tab${filter===t?' active':''}`} onClick={()=>setFilter(t)}>{t}</button>)}
      </div>
      <div className="glass" style={{ overflow:'hidden' }}>
        <div style={{ display:'grid', gridTemplateColumns:'80px 12px 120px 1fr', padding:'10px 16px', gap:12,
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
    { label:'CPU', value:health.cpu, unit:'%', color:'var(--blue)', hist:health.cpu_h, warn:70, crit:90 },
    { label:'Memory', value:health.mem, unit:'%', color:'var(--purple)', hist:health.mem_h, warn:75, crit:90 },
    { label:'Event Loop Lag', value:health.eventLoop, unit:'ms', color:'var(--yellow)', hist:health.el_h, warn:50, crit:200 },
    { label:'Bus Latency', value:health.busLatency, unit:'ms', color:'var(--green)', hist:null, warn:100, crit:500 },
  ]
  return (
    <div style={{ flex:1, overflow:'auto', padding:20, display:'flex', flexDirection:'column', gap:16 }}>
      <div style={{ display:'flex', gap:12, alignItems:'center' }}>
        <h2 style={{ fontSize:20, fontWeight:700 }}>System Health</h2>
        <span className="badge badge-green"><div className="sdot sdot-green sdot-pulse" />All Systems Nominal</span>
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
                  <div style={{ fontSize:32, fontWeight:800, color:sevColor, letterSpacing:'-0.02em' }}>{m.value.toFixed(1)}<span style={{fontSize:14,fontWeight:500,color:'var(--t2)',marginLeft:2}}>{m.unit}</span></div>
                </div>
                <span className={`badge badge-${sev}`}>{sev.toUpperCase()}</span>
              </div>
              {m.hist && <Spark data={m.hist} color={sevColor} h={48} dataKey="v" />}
              <div style={{ display:'flex', gap:8, marginTop:8 }}>
                <div style={{ flex:1, height:4, background:'rgba(255,255,255,0.06)', borderRadius:2, overflow:'hidden' }}>
                  <div style={{ height:'100%', width:`${Math.min(100,(m.value/m.crit)*100)}%`, background:sevColor, borderRadius:2, transition:'width .8s' }} />
                </div>
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
          { label:'Recon Drift', value:`${health.drift} positions`, color:'var(--green)', icon:'⚖️' },
          { label:'Agents Alive', value:'19 / 19', color:'var(--green)', icon:'🤖' },
        ].map((s,i) => (
          <div key={i} className="glass" style={{ padding:20, textAlign:'center' }}>
            <div style={{ fontSize:28, marginBottom:8 }}>{s.icon}</div>
            <div style={{ fontSize:11, color:'var(--t2)', marginBottom:4 }}>{s.label}</div>
            <div style={{ fontSize:16, fontWeight:700, color:s.color }}>{s.value}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
