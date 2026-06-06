import React, { useEffect, useState, useRef, useCallback, useMemo } from 'react'
import ReactFlow, { Background, Handle, Position } from 'reactflow'
import 'reactflow/dist/style.css'

const API = 'http://localhost:8800'
const GREEN = '#0f6e56', AMBER = '#b5791a', RED = '#a3402f', DIM = '#9ca3af', INK = '#141413', PAPER = '#fbfbf9'

// ===========================================================================
//  The cockpit is a SEQUENCED experience, not a dashboard. The system map is
//  the stage; the agent's attention raises ONE focal card at a time over it
//  (diagnose → act → verify → defer → hold → trip). The mind is a dimmed rail;
//  the metrics are a slim top bar. It opens mid-crisis (ungoverned, bleeding)
//  and plays the arc the agent drives.
// ===========================================================================

function Btn({ onClick, label, color, primary, big }) {
  return <button className="nodrag nopan" onClick={(e) => { e.stopPropagation(); onClick() }}
    style={{ fontSize: big ? 14 : 11.5, borderRadius: 7, padding: big ? '8px 18px' : '3px 10px',
      cursor: 'pointer', fontWeight: primary ? 700 : 500,
      border: `1px solid ${color}`, color: primary ? '#fff' : color,
      background: primary ? color : '#fff' }}>{label}</button>
}

// ---- system-map control node ----------------------------------------------
const _STC = { green: GREEN, amber: AMBER, escalate: AMBER, vetoed: '#8f8f86', struct: '#b7b6ae' }
const _STF = { green: '#e6f5ef', amber: '#fbf0db', escalate: '#fbf0db', vetoed: '#efeee8', struct: '#fff' }

function CtrlNode({ data }) {
  const c = _STC[data.state] || '#b7b6ae', fill = _STF[data.state] || '#fff'
  const isClass = data.kind === 'class', isOp = data.kind === 'op'
  return (
    <div style={{ border: `1.5px solid ${c}`, background: fill, borderRadius: isOp ? 9 : 12,
      padding: isOp ? '7px 10px' : '10px 13px', minWidth: isClass ? 188 : isOp ? 124 : 150,
      cursor: 'pointer', boxShadow: isClass ? '0 1px 6px rgba(0,0,0,.08)' : '0 1px 3px rgba(0,0,0,.05)' }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div style={{ fontWeight: isClass ? 800 : 600, fontSize: isClass ? 14.5 : 12.5,
        color: INK, fontFamily: isOp ? 'monospace' : 'inherit' }}>{data.label}</div>
      <div style={{ fontSize: isClass ? 12 : 10.5, color: c, marginTop: 1 }}>{data.sub}</div>
      {data.kind === 'lever' && (
        <div style={{ marginTop: 6, display: 'flex', gap: 5, flexWrap: 'wrap' }}>
          {data.escalated && <>
            <Btn onClick={() => data.act('accept', data.sig)} label="arm it" color={AMBER} primary />
            <Btn onClick={() => data.act('reject', data.sig)} label="not now" color={AMBER} />
          </>}
          {data.active && <Btn onClick={() => data.act('veto', data.sig)} label="veto" color={GREEN} />}
          {data.vetoed && <Btn onClick={() => data.act('enable', data.sig)} label="re-enable" color={AMBER} primary />}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  )
}
const nodeTypes = { ctrl: CtrlNode }

// ---- a number that eases to its new value (semantic motion) ----------------
function useTween(target, dur = 1100) {
  const [v, setV] = useState(target ?? 0)
  const ref = useRef(target ?? 0)
  useEffect(() => {
    if (target == null) return
    const from = ref.current, t0 = performance.now()
    let raf
    const step = (t) => {
      const k = Math.min((t - t0) / dur, 1)
      const nv = from + (target - from) * k
      setV(nv); ref.current = nv
      if (k < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target])
  return v
}

// ===========================================================================
//  App — the sequenced stage
// ===========================================================================
export default function App() {
  const [state, setState] = useState(null)
  const [feed, setFeed] = useState([])
  const [proof, setProof] = useState(null)
  const [evalView, setEvalView] = useState(null)   // consequence popup (arm / trip)
  const [inspectTc, setInspectTc] = useState(null) // per-box inspector — DEFAULT click
  const [lab, setLab] = useState(false)            // the replay-at-scale debugger (sandbox)
  // Ending B is reached deliberately via the inspector's force-route on refunds —
  // no floating controls on the main canvas (that protects the supervise default).

  useEffect(() => {
    const es = new EventSource(`${API}/api/stream`)
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data)
      if (ev.state) setState(ev.state)
      if (ev.narration) setFeed((f) => [{ ...ev.narration, seq: ev.seq }, ...f].slice(0, 120))
    }
    es.onerror = () => {}
    // Open mid-crisis: restart ungoverned so each visit plays the full arc.
    fetch(`${API}/api/reset`, { method: 'POST' }).catch(() => {})
    return () => es.close()
  }, [])

  const act = useCallback((kind, sig) => {
    fetch(`${API}/api/action/${kind}/${encodeURIComponent(sig)}`, { method: 'POST' })
    if (kind === 'accept' && sig && sig.startsWith('route_model')) setEvalView({ key: 'hold', mode: 'arm' })
  }, [])
  const openProof = useCallback((node) => {
    fetch(`${API}/api/proof/${node || 'requests'}`).then((r) => r.json()).then(setProof).catch(() => {})
  }, [])
  // manual control from inside a box — REAL levers, agent stays on watch
  const onForceCache = useCallback((sig) => { act('enable', sig) }, [act])
  const onForceRoute = useCallback((rt) => {
    setInspectTc(null)
    if (rt.risky) setEvalView({ key: rt.eval_key, mode: 'trip' })  // catches it on the real eval
    else act('accept', rt.sig)                                     // arms + opens the hold eval
  }, [act])

  const { nodes, edges } = useMemo(() => buildGraph(state, act), [state, act])
  const scene = useMemo(() => sceneFor(state, feed), [state, feed])
  // the map dims whenever a focal card commands attention
  const dimMap = scene && scene.kind !== 'idle'

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: PAPER }}>
      <TopBar state={state} onLab={() => setLab(true)} />
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        <MindRail feed={feed} step={state?.step} />
        <div style={{ flex: 1, position: 'relative', minHeight: 0, overflow: 'hidden' }}>
          <div style={{ position: 'absolute', inset: 0, filter: dimMap ? 'saturate(.5)' : 'none',
            opacity: dimMap ? 0.32 : 1, transition: 'opacity .5s, filter .5s', pointerEvents: dimMap ? 'none' : 'auto' }}>
            {nodes.length === 0
              ? <div style={{ padding: 24, color: DIM }}>connecting to the live stream…</div>
              : <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView
                  style={{ width: '100%', height: '100%' }}
                  onNodeClick={(e, node) => setInspectTc(node.data.proofNode || node.id)}
                  proOptions={{ hideAttribution: true }} nodesDraggable={false}
                  nodesConnectable={false} elementsSelectable={false} panOnDrag={false}
                  zoomOnScroll={false} zoomOnDoubleClick={false}>
                  <Background color="#e7e7e0" gap={22} />
                </ReactFlow>}
          </div>
          {/* the arc's focal cards rise over the dimmed map, one at a time */}
          <FocalLayer scene={scene} state={state} act={act} openProof={openProof} />
        </div>
      </div>
      {proof && <ProofPanel proof={proof} onClose={() => setProof(null)} />}
      {inspectTc && <DebuggerPanel tc={inspectTc} onClose={() => setInspectTc(null)}
        onForceCache={onForceCache} onForceRoute={onForceRoute} />}
      {evalView && <EvalPopup view={evalView} onClose={() => setEvalView(null)} />}
      {lab && <ReplayLab onClose={() => setLab(false)} />}
    </div>
  )
}

// ---- scene derivation: what the agent is attending to right now ------------
function sceneFor(state, feed) {
  if (!state) return { kind: 'idle' }
  const route = state.levers && state.levers.find((l) => l.sig && l.sig.startsWith('route_model'))
  // the pivot: a deferred decision waits for the human — it PERSISTS until acted
  if (route && route.escalated && !route.active && !route.vetoed) return { kind: 'defer', route }
  const n = feed[0]
  if (!n) return { kind: 'idle' }
  if (n.kind === 'thinking') return { kind: 'diagnose', text: n.text }
  if (n.kind === 'applied') return { kind: 'act', text: n.text }
  if (n.kind === 'verified') return { kind: 'verify', text: n.text }
  // 'holding' raises NO focal card — the settled state lives in the inbox only
  // (one voice, one place). The map simply returns to its calm governed state.
  return { kind: 'idle' }
}

// ---- the focal layer: one card at a time, centred, over the dimmed map -----
function FocalLayer({ scene, state, act, openProof }) {
  if (!scene || scene.kind === 'idle') return null
  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
      justifyContent: 'center', pointerEvents: 'none', padding: 28 }}>
      <div key={scene.kind} style={{ pointerEvents: 'auto', width: scene.kind === 'defer' ? 560 : 600,
        animation: 'rise .45s ease-out' }}>
        {scene.kind === 'diagnose' && <DiagnoseCard text={scene.text} state={state} openProof={openProof} />}
        {scene.kind === 'act' && <ActCard text={scene.text} openProof={openProof} />}
        {scene.kind === 'verify' && <VerifyCard text={scene.text} state={state} />}
        {scene.kind === 'defer' && <DeferCard text={null} route={scene.route} act={act} />}
      </div>
      <style>{`@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}`}</style>
    </div>
  )
}

function Card({ accent, kicker, children, glow }) {
  return (
    <div style={{ background: '#fff', border: `1.5px solid ${accent}`, borderRadius: 16,
      padding: '20px 22px', boxShadow: glow ? `0 12px 44px ${accent}33` : '0 8px 30px rgba(0,0,0,.14)' }}>
      <div style={{ fontSize: 11, letterSpacing: '.1em', textTransform: 'uppercase',
        fontWeight: 800, color: accent }}>{kicker}</div>
      {children}
    </div>
  )
}

function DiagnoseCard({ text, state, openProof }) {
  const top = state?.classes?.find((c) => !c.baseline) || state?.classes?.[0]
  return (
    <Card accent={AMBER} kicker="◇ Diagnose · where the money leaks">
      <div style={{ fontSize: 22, lineHeight: 1.34, color: INK, marginTop: 7 }}>{text}</div>
      {top && (
        <div style={{ display: 'flex', gap: 22, marginTop: 14, paddingTop: 12, borderTop: '1px solid #f0eee6' }}>
          <Stat label={top.label} main={`$${top.cost_per_ticket.toFixed(4)}`} sub="per message" color={AMBER} />
          <Stat label="vs cheapest task" main={`${top.mult}×`} sub="more expensive" color={AMBER} />
          <Stat label="share of spend" main={`${Math.round(top.share * 100)}%`} sub="of the bill" color={AMBER} />
          <div style={{ marginLeft: 'auto', alignSelf: 'flex-end' }}>
            <Btn onClick={() => openProof(top.tc)} label="see the traces ↗" color={AMBER} />
          </div>
        </div>
      )}
    </Card>
  )
}

function ActCard({ text, openProof }) {
  return (
    <Card accent={GREEN} kicker="✓ Act · safe, hands-off" glow>
      <div style={{ fontSize: 22, lineHeight: 1.34, color: INK, marginTop: 7 }}>{text}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 14,
        paddingTop: 12, borderTop: '1px solid #eef4f0' }}>
        <div style={{ fontSize: 13.5, color: '#3b3b37' }}>
          The repeated paid call never runs — served from cache, same result.
        </div>
        <div style={{ marginLeft: 'auto' }}>
          <Btn onClick={() => openProof('tools')} label="open the cached call in Phoenix ↗" color={GREEN} primary />
        </div>
      </div>
    </Card>
  )
}

function VerifyCard({ text, state }) {
  const v = state?.verify
  return (
    <Card accent={GREEN} kicker="✦ Verify · re-measured from the traffic">
      <div style={{ fontSize: 20, lineHeight: 1.34, color: INK, marginTop: 7 }}>{text}</div>
      {v && (
        <div style={{ display: 'flex', gap: 22, marginTop: 14, paddingTop: 12, borderTop: '1px solid #eef4f0' }}>
          <Stat label="$ / message" main={`$${v.baseline_dollars_per_message.toFixed(4)} → $${v.dollars_per_message.toFixed(4)}`} sub="baseline → governed" color={GREEN} />
          <Stat label="saved" main={`$${Number(v.monthly_saving).toLocaleString(undefined, { maximumFractionDigits: 0 })}/mo`} sub="at current volume" color={GREEN} />
        </div>
      )}
    </Card>
  )
}

function DeferCard({ route, act }) {
  return (
    <Card accent={AMBER} kicker="⚑ Defer · the agent stops — your call" glow>
      <div style={{ fontSize: 23, lineHeight: 1.38, color: INK, marginTop: 8 }}>
        Caching was safe — done. Routing to a cheaper model could <b>change the
        answers</b>, and I can't prove it won't. That call is yours.
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 16 }}>
        <div style={{ fontSize: 13.5, color: DIM }}>Arming it takes it live and instruments the consequence.</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 9 }}>
          <Btn onClick={() => act('reject', route.sig)} label="not now" color={AMBER} />
          <Btn onClick={() => act('accept', route.sig)} label="arm it →" color={AMBER} primary big />
        </div>
      </div>
    </Card>
  )
}

function Stat({ label, main, sub, color }) {
  return (
    <div>
      <div style={{ fontSize: 10.5, color: DIM, letterSpacing: '.04em', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 800, color, lineHeight: 1.2 }}>{main}</div>
      <div style={{ fontSize: 10.5, color: DIM }}>{sub}</div>
    </div>
  )
}

// ===========================================================================
//  Top bar — the slim, always-on heartbeat (metrics + the agent's loop)
// ===========================================================================
const _STEP_LABEL = { OBSERVE: 'observe', DIAGNOSE: 'diagnose', DECIDE: 'decide', ACT: 'act', VERIFY: 'verify' }
function MindLoop({ step, steps }) {
  const seq = steps || ['OBSERVE', 'DIAGNOSE', 'DECIDE', 'ACT', 'VERIFY']
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      {seq.map((s, i) => {
        const on = s === step
        return (
          <React.Fragment key={s}>
            <div style={{ fontSize: 10.5, fontWeight: on ? 800 : 500, letterSpacing: '.03em',
              padding: '3px 8px', borderRadius: 999, textTransform: 'uppercase',
              color: on ? '#fff' : DIM, background: on ? GREEN : '#f0efe9',
              border: `1px solid ${on ? GREEN : '#e6e4da'}`, transition: 'all .25s' }}>
              {_STEP_LABEL[s] || s.toLowerCase()}
            </div>
            {i < seq.length - 1 && <span style={{ color: '#cfcdc2', fontSize: 11 }}>→</span>}
          </React.Fragment>
        )
      })}
    </div>
  )
}

function TopBar({ state, onLab }) {
  const dpm = useTween(state?.dollars_per_message)
  const base = state?.baseline_dollars_per_message
  const down = state && state.dollars_per_message < base - 1e-9
  const pct = down ? Math.round((1 - state.dollars_per_message / base) * 100) : 0
  return (
    <div style={{ padding: '10px 22px', borderBottom: '1px solid #eceae0', display: 'flex',
      alignItems: 'center', gap: 26, background: '#fff' }}>
      <div>
        <div style={{ fontSize: 11, color: DIM, letterSpacing: '.04em' }}>AI COST GOVERNANCE</div>
        <div style={{ fontSize: 14.5, fontWeight: 700 }}>An agent governing another agent</div>
      </div>
      <div style={{ borderLeft: '1px solid #eceae0', paddingLeft: 22 }}>
        <div style={{ fontSize: 10.5, color: DIM, marginBottom: 4 }}>the agent's loop</div>
        <MindLoop step={state?.step} steps={state?.steps} />
      </div>
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 26, alignItems: 'flex-end' }}>
        <Metric label="throughput" main={state ? state.throughput_per_sec.toFixed(2) : '—'} unit="msgs/sec" color={INK} />
        <Metric label="$ / message" main={`$${dpm.toFixed(4)}`} unit={down ? `▼${pct}%` : 'baseline'}
          color={down ? GREEN : INK} unitColor={down ? GREEN : DIM} />
        <Metric label="burn" main={`$${state ? (state.burn_per_min < 0.1 ? state.burn_per_min.toFixed(3) : state.burn_per_min.toFixed(2)) : '—'}`} unit="/min" color={DIM} small />
        <Metric label="measured saved" main={`$${state ? state.realized_savings.toFixed(4) : '—'}`} unit="live" color={GREEN} />
      </div>
      <button onClick={onLab} style={{ fontSize: 12.5, fontWeight: 600, cursor: 'pointer',
        border: '1px solid #d8d6cc', borderRadius: 8, padding: '6px 12px',
        color: '#5a5852', background: '#fff' }}>
        🔬 debugger
      </button>
    </div>
  )
}
function Metric({ label, main, unit, color, unitColor, small }) {
  return (
    <div style={{ textAlign: 'right' }}>
      <div style={{ fontSize: 10.5, color: DIM, letterSpacing: '.03em', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: small ? 16 : 24, fontWeight: 800, lineHeight: 1.05, color }}>
        {main}<span style={{ fontSize: 12, fontWeight: 600, color: unitColor || DIM }}> {unit}</span>
      </div>
    </div>
  )
}

// ===========================================================================
//  Mind rail — the agent thinking, receded to a quiet column
// ===========================================================================
function railStyle(kind) {
  if (kind === 'user') return { accent: '#3b3b37', bg: '#efece2', icon: '' }
  if (kind === 'applied') return { accent: GREEN, bg: '#eaf6f0', icon: '✓ ' }
  if (kind === 'verified') return { accent: GREEN, bg: '#eef7f1', icon: '✦ ' }
  if (kind === 'escalate') return { accent: AMBER, bg: '#fbf0db', icon: '⚑ ' }
  if (kind === 'holding') return { accent: DIM, bg: '#f6f6f1', icon: '' }
  return { accent: GREEN, bg: '#f3faf6', icon: '' }
}
// The mind rail shows NOW big and readable; the past collapses behind search.
function MindRail({ feed, step }) {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const now = feed[0]
  const rest = feed.slice(1)
  const shown = q ? rest.filter((c) => c.text.toLowerCase().includes(q.toLowerCase())) : (open ? rest : [])
  const s = now ? railStyle(now.kind) : null
  return (
    <div style={{ width: 380, borderRight: '1px solid #eceae0', background: '#fff',
      display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ padding: '15px 18px 6px' }}>
        <div style={{ fontWeight: 800, fontSize: 18 }}>The agent's mind</div>
        <div style={{ fontSize: 13, color: GREEN }}>● reasoning live · {(step || 'observe').toLowerCase()}</div>
      </div>
      {/* NOW — the current thought, large and prominent */}
      <div style={{ padding: '8px 18px 4px' }}>
        {now ? (
          <div style={{ border: `2px solid ${s.accent}`, background: s.bg, borderRadius: 13, padding: '15px 16px' }}>
            <div style={{ fontSize: 10.5, letterSpacing: '.1em', textTransform: 'uppercase',
              fontWeight: 800, color: s.accent }}>{now.kind === 'user' ? 'your move' : 'now'}</div>
            <div style={{ fontSize: 19, lineHeight: 1.42, color: INK, marginTop: 5 }}>
              {now.kind === 'user' ? <b>You: </b> : s.icon}{now.text}
            </div>
          </div>
        ) : <div style={{ color: DIM, fontSize: 17, padding: '10px 0' }}>Reading the live traffic…</div>}
      </div>
      {/* history, collapsed behind search */}
      <div style={{ padding: '8px 18px 4px', display: 'flex', gap: 8, alignItems: 'center' }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search the agent's history…"
          style={{ flex: 1, padding: '7px 11px', border: '1px solid #e0ded3', borderRadius: 8, fontSize: 13.5 }} />
        {!q && rest.length > 0 &&
          <button onClick={() => setOpen((o) => !o)} style={{ fontSize: 12.5, color: DIM, cursor: 'pointer',
            border: '1px solid #e6e4da', borderRadius: 8, padding: '6px 9px', background: '#fff', whiteSpace: 'nowrap' }}>
            {open ? 'hide' : `${rest.length} earlier`}</button>}
      </div>
      <div style={{ overflowY: 'auto', flex: 1, minHeight: 0, padding: '4px 18px 16px' }}>
        {shown.map((c) => (
          <div key={c.seq} style={{ padding: '8px 0', borderTop: '1px solid #f4f2ea' }}>
            <div style={{ fontSize: 15, color: '#3b3b37', lineHeight: 1.42 }}>
              {c.kind === 'user' ? <b>You: </b> : railStyle(c.kind).icon}{c.text}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ===========================================================================
//  The model-eval consequence popup (ARM → HOLD, and the staged TRIP)
// ===========================================================================
function qColor(b, e) { return e >= b ? GREEN : AMBER }

function EvalPopup({ view, onClose }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [revealed, setRevealed] = useState(0)
  const trip = view.mode === 'trip'

  useEffect(() => {
    fetch(`${API}/api/eval/${view.key}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setData).catch(setErr)
  }, [view.key])

  useEffect(() => {
    if (!data) return
    const n = data.rows.length
    setRevealed(0)
    const per = Math.max(900, Math.round(10000 / n))
    let i = 0
    const id = setInterval(() => { i += 1; setRevealed(i); if (i >= n) clearInterval(id) }, per)
    return () => clearInterval(id)
  }, [data])

  const shown = data ? data.rows.slice(0, revealed) : []
  const done = data && revealed >= data.rows.length
  const k = shown.length || 1
  const mBase = shown.reduce((s, r) => s + r.baseline_quality, 0) / k
  const mEcon = shown.reduce((s, r) => s + r.economy_quality, 0) / k
  const equiv = shown.filter((r) => r.equivalent).length
  const clar = shown.filter((r) => r.clarified).length
  const refused = shown.filter((r) => r.refused_escalated).length
  const hold = data && data.verdict === 'hold'
  const accent = hold ? GREEN : RED

  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.45)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 60 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: '#fff', borderRadius: 14, padding: 24,
        width: 660, maxHeight: '88vh', overflowY: 'auto', boxShadow: '0 14px 50px rgba(0,0,0,.3)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div style={{ fontSize: 11, letterSpacing: '.09em', color: trip ? RED : AMBER, fontWeight: 800 }}>
              {trip ? 'DELIBERATELY RISKIER CALL · the agent is checking it' : 'YOUR DECISION · ARMED & LIVE'}
            </div>
            <div style={{ fontSize: 21, fontWeight: 800, marginTop: 2 }}>
              {trip ? 'Route refunds → economy model' : 'Route simple tickets → economy model'}
            </div>
            <div style={{ fontSize: 13, color: DIM, marginTop: 3 }}>
              Accelerated eval · real replays, LLM-judge scored · clock compressed to ~10s
            </div>
          </div>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 24, cursor: 'pointer', color: DIM }}>×</button>
        </div>

        {err && <div style={{ color: AMBER, padding: '16px 0' }}>No cached eval yet — pre-run it off-stage.</div>}
        {!data && !err && <div style={{ color: DIM, padding: '16px 0' }}>Loading the eval…</div>}

        {data && (
          <>
            <div style={{ height: 4, background: '#eee', borderRadius: 3, margin: '16px 0 14px', overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${(revealed / data.rows.length) * 100}%`,
                background: accent, transition: 'width .4s' }} />
            </div>

            <div style={{ display: 'flex', gap: 10, marginBottom: 14 }}>
              <Tile label="answer quality" main={`${mBase.toFixed(1)} → ${mEcon.toFixed(1)}`}
                color={qColor(mBase, mEcon)} sub="baseline → economy" />
              <Tile label="same resolution" main={`${equiv}/${shown.length || 0}`}
                color={equiv === shown.length ? GREEN : AMBER} sub="judged equivalent" />
              <Tile label="new clarifications" main={`${clar}`} color={clar ? AMBER : GREEN} sub="economy asked back" />
              <Tile label="refused / escalated" main={`${refused}`} color={refused ? AMBER : GREEN} sub="instead of resolving" />
            </div>

            <div style={{ border: '1px solid #eceae0', borderRadius: 10, overflow: 'hidden' }}>
              {shown.map((r, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px',
                  borderTop: i ? '1px solid #f1efe8' : 'none', fontSize: 14.5 }}>
                  <span style={{ flex: 1, color: '#23231f' }}>{r.ticket.replace(/Account [A-Z]+-\d+\.?/g, '').slice(0, 50)}</span>
                  <span style={{ fontWeight: 700, color: qColor(r.baseline_quality, r.economy_quality) }}>
                    {r.baseline_quality} → {r.economy_quality}
                  </span>
                  {r.equivalent
                    ? <span style={{ fontSize: 12, color: GREEN }}>● equivalent</span>
                    : <span style={{ fontSize: 12, color: AMBER }}>● {r.clarified ? 'clarified' : r.refused_escalated ? 'escalated' : 'differs'}</span>}
                  {r.phoenix_url && <a href={r.phoenix_url} target="_blank" rel="noreferrer"
                    style={{ fontSize: 13, color: GREEN, textDecoration: 'none' }}>trace ↗</a>}
                </div>
              ))}
            </div>

            {done && (
              <div style={{ marginTop: 16, border: `1.5px solid ${accent}`,
                background: hold ? '#eef7f1' : '#f9ede9', borderRadius: 12, padding: '14px 16px' }}>
                <div style={{ fontSize: 11.5, letterSpacing: '.08em', fontWeight: 800, color: accent }}>THE AGENT'S VERDICT</div>
                <div style={{ fontSize: 18, color: INK, marginTop: 5, lineHeight: 1.4 }}>
                  {hold
                    ? '✦ Quality held — keep it live, the agent keeps watching.'
                    : '⚑ Quality collapses on refunds — the agent recommends against this call.'}
                </div>
                <div style={{ fontSize: 12, color: DIM, marginTop: 7 }}>
                  My judgment over the signals — Phoenix surfaces the evidence, I render the verdict.
                </div>
                {!hold && <div style={{ marginTop: 12 }}>
                  <Btn onClick={onClose} label="stand down — don't route refunds" color={RED} primary big />
                </div>}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
function Tile({ label, main, sub, color }) {
  return (
    <div style={{ flex: 1, border: '1px solid #eceae0', borderRadius: 10, padding: '10px 12px' }}>
      <div style={{ fontSize: 11, color: DIM, letterSpacing: '.04em', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 800, color, lineHeight: 1.2 }}>{main}</div>
      <div style={{ fontSize: 11, color: DIM }}>{sub}</div>
    </div>
  )
}

// ===========================================================================
//  The debugger — a deliberate MODE; drop into any box. Per-call cost with its
//  SOURCE (LLM measured by Phoenix vs tool at your editable rate), and real
//  manual controls. Editing a rate recomputes $/message everywhere; manual
//  actions keep the agent on watch (force-route runs the eval).
// ===========================================================================
function RateInput({ value, onCommit }) {
  const [v, setV] = useState(String(value))
  useEffect(() => { setV(String(value)) }, [value])
  const commit = () => { const n = parseFloat(v); if (n >= 0 && n !== value) onCommit(n) }
  return (
    <input value={v} onChange={(e) => setV(e.target.value)} onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur() }}
      style={{ width: 72, padding: '2px 6px', fontSize: 13, textAlign: 'right',
        border: `1px solid ${AMBER}55`, borderRadius: 6, color: '#5a4815', background: '#fdfaf3' }} />
  )
}

function DebuggerPanel({ tc, onClose, onForceCache, onForceRoute }) {
  const [d, setD] = useState(null)
  const load = useCallback(() => fetch(`${API}/api/debug/${tc}`).then((r) => r.json()).then(setD).catch(() => {}), [tc])
  useEffect(() => { load() }, [load])
  const setRate = (tool, rate) =>
    fetch(`${API}/api/tool_rate?tool=${encodeURIComponent(tool)}&rate=${rate}`, { method: 'POST' }).then(load)

  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 55 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: PAPER, borderRadius: 14, padding: '22px 24px',
        width: 640, maxHeight: '88vh', overflowY: 'auto', boxShadow: '0 14px 50px rgba(0,0,0,.3)' }}>
        {!d ? <div style={{ color: DIM, padding: 16 }}>Dropping into the box…</div> : <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <span style={{ fontSize: 10.5, fontWeight: 800, color: '#fff', background: '#5a4815',
                borderRadius: 9, padding: '3px 10px', letterSpacing: '.06em' }}>INSPECT</span>
              <div style={{ fontSize: 21, fontWeight: 700, marginTop: 10 }}>{d.title}</div>
              <div style={{ fontSize: 13, color: '#3b3b37', marginTop: 2 }}>
                {Math.round(d.share * 100)}% of spend · ${d.cost_per_message.toFixed(4)} / message
              </div>
            </div>
            <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 24, cursor: 'pointer', color: DIM }}>×</button>
          </div>

          {d.pattern && <>
            <div style={{ fontSize: 11, color: DIM, letterSpacing: '.06em', marginTop: 18 }}>WHAT'S HAPPENING</div>
            <div style={{ fontSize: 15, color: INK, marginTop: 5 }} dangerouslySetInnerHTML={{ __html: mdCode(d.pattern) }} />
          </>}

          <div style={{ fontSize: 11, color: DIM, letterSpacing: '.06em', marginTop: 18, display: 'flex' }}>
            <span style={{ flex: 1 }}>CALLS &amp; COST · this conversation</span>
            <span style={{ width: 80, textAlign: 'right' }}>cost</span>
            <span style={{ width: 168, textAlign: 'right' }}>source</span>
          </div>
          <div style={{ borderTop: '1px solid #e6e1d4', marginTop: 6 }}>
            {/* LLM — measured by Phoenix */}
            <div style={{ display: 'flex', alignItems: 'center', padding: '9px 0', borderBottom: '1px solid #efe9da' }}>
              <span style={{ flex: 1, fontSize: 14.5, color: INK }}>premium model · LLM</span>
              <span style={{ width: 80, textAlign: 'right', fontSize: 14.5, color: INK }}>${d.llm_cost.toFixed(4)}</span>
              <span style={{ width: 168, textAlign: 'right', fontSize: 12.5, color: GREEN }}>
                measured · Phoenix {d.llm_url && <a href={d.llm_url} target="_blank" rel="noreferrer" style={{ color: GREEN }}>trace ↗</a>}
              </span>
            </div>
            {/* tools — your editable rates */}
            {d.tool_rows.map((r) => (
              <div key={r.tool} style={{ display: 'flex', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #efe9da' }}>
                <span style={{ flex: 1, fontSize: 14.5, color: INK }}>{r.tool} ×{r.count} · tool</span>
                <span style={{ width: 80, textAlign: 'right', fontSize: 14.5, color: INK }}>${r.cost.toFixed(4)}</span>
                <span style={{ width: 168, textAlign: 'right', fontSize: 12.5, color: AMBER,
                  display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 6 }}>
                  your rate <RateInput value={r.rate} onCommit={(n) => setRate(r.tool, n)} /> ✎
                </span>
              </div>
            ))}
            {/* total */}
            <div style={{ display: 'flex', alignItems: 'center', padding: '10px 0' }}>
              <span style={{ flex: 1, fontSize: 14.5, fontWeight: 700, color: INK }}>total</span>
              <span style={{ width: 80, textAlign: 'right', fontSize: 15.5, fontWeight: 700, color: INK }}>${d.cost_per_message.toFixed(4)}</span>
              <span style={{ width: 168, textAlign: 'right', fontSize: 11.5, color: DIM }}>LLM measured · tools your rate</span>
            </div>
          </div>

          <div style={{ fontSize: 11, color: DIM, letterSpacing: '.06em', marginTop: 16 }}>TAKE MANUAL CONTROL</div>
          <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
            {d.cache && (d.cache.active
              ? <div style={{ flex: 1, textAlign: 'center', fontSize: 13, color: GREEN, border: `1px solid ${GREEN}`,
                  borderRadius: 8, padding: '8px 12px', background: '#eef7f1' }}>✓ repeated lookups cached</div>
              : <button onClick={() => onForceCache(d.cache.sig)} style={mbtn(GREEN)}>force-cache repeated lookups</button>)}
            <button onClick={() => onForceRoute(d.route)} style={mbtn(AMBER)}>route → economy model</button>
          </div>
          <div style={{ fontSize: 12.5, color: '#3b3b37', marginTop: 12 }}>
            The agent keeps watching — it runs the eval and flags you if quality drops.
          </div>
        </>}
      </div>
    </div>
  )
}
function mbtn(color) {
  return { flex: 1, fontSize: 13, fontWeight: 600, cursor: 'pointer', color, background: '#fff',
    border: `1px solid ${color}`, borderRadius: 8, padding: '8px 12px' }
}
function mdCode(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/`([^`]+)`/g, '<code style="background:#efe9da;border-radius:4px;padding:0 4px">$1</code>')
}

// ===========================================================================
//  The replay-at-scale lab — a sandbox modal OVER the live system (which keeps
//  running underneath). Same connected-box canvas; the candidate is highlighted
//  in place (model node "TESTING"). Replays REAL past conversations at scale,
//  shows the held/degraded DISTRIBUTION + conditional cost + the agent's
//  recommendation. Real 'test'-tagged Phoenix traces; never touches production.
// ===========================================================================
const LAB_USE_CASES = [
  { key: 'account_question', label: 'Account questions' },
  { key: 'refund_handling', label: 'Refund tickets' },
]

function buildLabGraph(uc, data) {
  const base = data?.cost?.baseline
  const title = uc === 'refund_handling' ? 'Refund tickets' : 'Account questions'
  const ops = uc === 'refund_handling'
    ? [['web_search ×3', 'cached · $0'], ['kb_lookup', 'cached · $0']]
    : [['kb_lookup', 'cached · $0']]
  const nodes = [nd('lane', 8, 54, { kind: 'class', label: title,
    sub: base ? `$${base.toFixed(4)}/msg` : '—', state: 'struct' })]
  const edges = []
  let prev = 'lane', x = 230
  ops.forEach(([l, s], i) => {
    const id = 'op' + i
    nodes.push(nd(id, x, 58, { kind: 'op', label: l, sub: s, state: 'green' }))
    edges.push(ed('e' + id, prev, id, '#c8c7c0')); prev = id; x += 168
  })
  nodes.push(nd('model', x, 50, { kind: 'op', label: 'model · TESTING', sub: 'premium → economy', state: 'escalate' }))
  edges.push(ed('emodel', prev, 'model', AMBER))
  return { nodes, edges }
}

function ReplayLab({ onClose }) {
  const [uc, setUc] = useState('account_question')
  const [data, setData] = useState(null)
  const [ran, setRan] = useState(false)
  const [trickle, setTrickle] = useState(null)
  const [waiting, setWaiting] = useState(false)

  const load = useCallback((u) => {
    setData(null); setRan(false); setTrickle(null)
    fetch(`${API}/api/lab/${u}`).then((r) => (r.ok ? r.json() : null)).then(setData).catch(() => {})
  }, [])
  useEffect(() => { load(uc) }, [uc, load])

  const run = () => {
    setRan(true); setTrickle(null); setWaiting(true)
    fetch(`${API}/api/lab/${uc}/trickle?idx=${Math.floor((data?.n || 4) / 2)}`)
      .then((r) => (r.ok ? r.json() : null)).then((t) => { setTrickle(t); setWaiting(false) })
      .catch(() => setWaiting(false))
  }

  const { nodes, edges } = useMemo(() => buildLabGraph(uc, data), [uc, data])
  const held = data ? Math.round(data.held_pct * 100) : 0
  const deg = data ? Math.round(data.degraded_pct * 100) : 0
  const c = data?.cost
  const projUrl = 'https://app.phoenix.arize.com/s/tomas'

  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.35)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 65 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: '#fff', borderRadius: 14, padding: '20px 24px',
        width: 720, maxHeight: '92vh', overflowY: 'auto', boxShadow: '0 16px 56px rgba(0,0,0,.34)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div><b style={{ fontSize: 16 }}>DEBUGGER</b> <span style={{ color: DIM, fontSize: 13 }}>· sandbox</span></div>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 24, cursor: 'pointer', color: DIM }}>×</button>
        </div>

        {/* lab setup — same canvas, candidate highlighted in place */}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 14, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12.5, color: DIM }}>use case</span>
          <select value={uc} onChange={(e) => setUc(e.target.value)}
            style={{ fontSize: 13, padding: '5px 9px', borderRadius: 8, border: '1px solid #d8d6cc' }}>
            {LAB_USE_CASES.map((u) => <option key={u.key} value={u.key}>{u.label}</option>)}
          </select>
          <span style={labChip(false)}>replay {data ? data.n : '…'} real</span>
          <span style={labChip(true)}>candidate: → economy</span>
          <button onClick={run} disabled={!data} style={{ marginLeft: 'auto', fontSize: 13, fontWeight: 700,
            cursor: data ? 'pointer' : 'default', color: '#fff', background: GREEN, border: 'none',
            borderRadius: 8, padding: '7px 16px', opacity: data ? 1 : 0.5 }}>▶ run</button>
        </div>
        <div style={{ fontSize: 12, color: DIM, marginTop: 8 }}>
          replaying real past conversations · live system untouched · every trace tagged 'test'
        </div>

        {/* the same connected-box canvas, candidate node TESTING */}
        <div style={{ height: 130, marginTop: 8, borderTop: '1px solid #eceae0', borderBottom: '1px solid #eceae0' }}>
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView
            proOptions={{ hideAttribution: true }} nodesDraggable={false} nodesConnectable={false}
            elementsSelectable={false} panOnDrag={false} zoomOnScroll={false} zoomOnDoubleClick={false}>
            <Background color="#eeede6" gap={20} />
          </ReactFlow>
        </div>

        {!ran && <div style={{ color: DIM, fontSize: 14, padding: '20px 0', textAlign: 'center' }}>
          {data ? `Press ▶ run to replay ${data.n} real ${LAB_USE_CASES.find((u) => u.key === uc)?.label.toLowerCase()} through the candidate.`
            : 'No pre-run batch for this use case yet.'}
        </div>}

        {ran && data && <>
          <div style={{ fontSize: 11, color: DIM, letterSpacing: '.06em', marginTop: 16 }}>
            RESULT — does it hold across volume &amp; variety?
          </div>
          <div style={{ display: 'flex', gap: 28, marginTop: 10 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13, color: INK, marginBottom: 7 }}>answer quality across {data.n} real replays</div>
              <div style={{ display: 'flex', height: 16, borderRadius: 5, overflow: 'hidden', background: '#f0eee6' }}>
                <div style={{ width: `${held}%`, background: GREEN, opacity: 0.65, transition: 'width 1.1s ease-out' }} />
                <div style={{ width: `${deg}%`, background: AMBER, opacity: 0.6, transition: 'width 1.1s ease-out' }} />
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 7, fontSize: 12.5 }}>
                <span style={{ color: GREEN }}>held {held}%</span>
                <span style={{ color: AMBER }}>degraded {deg}%{data.degraded_dominant_sub ? ` · ${data.degraded_dominant_sub} cases` : ''}</span>
              </div>
            </div>
            {c && <div style={{ minWidth: 170 }}>
              <div style={{ fontSize: 13, color: INK }}>$ / message</div>
              <div style={{ fontSize: 17, fontWeight: 700, color: GREEN, marginTop: 4 }}>
                ${c.baseline.toFixed(4)} → ${c.projected.toFixed(4)}
              </div>
              <div style={{ fontSize: 12, color: GREEN }}>−{c.pct}% (if it shipped)</div>
            </div>}
          </div>

          {/* the agent's recommendation — judgment over the evidence, no Phoenix link */}
          <div style={{ marginTop: 16, border: `1px solid ${AMBER}`, background: '#faeeda', borderRadius: 10, padding: '13px 15px' }}>
            <div style={{ fontSize: 14.5, fontWeight: 600, color: INK }}>⚑ The agent: {data.recommendation}</div>
            <div style={{ fontSize: 12, color: '#85540b', marginTop: 5 }}>
              my judgment over the replayed evidence — found safely, before anything touched live traffic
            </div>
          </div>

          {/* a small LIVE trickle so it doesn't feel canned */}
          <div style={{ marginTop: 12, fontSize: 12.5, color: DIM }}>
            {waiting && <span>● running one more replay live…</span>}
            {trickle && <span>● live replay just landed: <b style={{ color: trickle.held ? GREEN : AMBER }}>
              {trickle.held ? 'held' : 'degraded'}</b> (economy quality {trickle.economy_quality}/5)
              {trickle.phoenix_url && <> · <a href={trickle.phoenix_url} target="_blank" rel="noreferrer" style={{ color: GREEN }}>trace ↗</a></>}</span>}
          </div>

          {/* facts → Phoenix; the 'test' traces are real and filterable */}
          <div style={{ marginTop: 14, borderTop: '1px solid #eceae0', paddingTop: 10 }}>
            <a href={projUrl} target="_blank" rel="noreferrer" style={{ fontSize: 12.5, color: GREEN, fontWeight: 600 }}>
              {data.n} 'test' traces in Phoenix ↗
            </a>
            <div style={{ fontSize: 11.5, color: DIM, marginTop: 3 }}>
              tagged <code style={{ background: '#efe9da', borderRadius: 4, padding: '0 4px' }}>{data.test_tag}</code> · filter them out of production in one click · pre-run batch, real result (not generated live)
            </div>
          </div>
        </>}
      </div>
    </div>
  )
}
function labChip(amber) {
  return { fontSize: 12.5, padding: '5px 11px', borderRadius: 999,
    border: `1px solid ${amber ? AMBER : '#d8d6cc'}`, color: amber ? AMBER : '#5a5852' }
}

// ---- the trace drill-down (per node / per workload) -----------------------
function ProofPanel({ proof, onClose }) {
  const pair = proof.pair
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.35)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: '#fff', borderRadius: 12, padding: 22,
        width: 580, maxHeight: '84vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,.25)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <b>{proof.title} — trace insight</b>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 24, cursor: 'pointer', color: DIM }}>×</button>
        </div>
        {pair && (
          <>
            <div style={{ fontSize: 14, margin: '8px 0' }}>
              Same ticket, two ways: baseline <b>${pair.baseline.total_usd.toFixed(4)}</b> → governed <b>${pair.governed.total_usd.toFixed(4)}</b>,
              {' '}{pair.skipped_calls} paid calls skipped, saved <b>${pair.saved_usd.toFixed(4)}</b>.
            </div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, marginBottom: 14 }}>
              <thead><tr style={{ color: DIM, textAlign: 'left' }}><th>call</th><th style={{ textAlign: 'right' }}>baseline</th><th>governed</th></tr></thead>
              <tbody>
                {pair.rows.map((r, i) => {
                  const cached = r.status === 'cached'
                  return (<tr key={i} style={{ background: cached ? '#fbf0db' : 'transparent' }}>
                    <td style={{ fontFamily: 'monospace', padding: '2px 4px' }}>{r.op}</td>
                    <td style={{ textAlign: 'right', padding: '2px 4px', color: cached ? AMBER : '#555' }}>${r.baseline.cost.toFixed(4)}</td>
                    <td style={{ padding: '2px 4px', color: cached ? GREEN : '#555' }}>{cached ? 'cached · $0' : `$${r.governed.cost.toFixed(4)}`}</td>
                  </tr>)
                })}
              </tbody>
            </table>
          </>
        )}
        <div style={{ fontSize: 13, color: DIM, margin: '4px 0 6px' }}>
          Real traces through this node — {proof.classes.join(', ').replace(/_/g, ' ')}
          {proof.stats && proof.stats.n ? ` · ${proof.stats.n} tickets, $${proof.stats.min?.toFixed(5)}–$${proof.stats.max?.toFixed(5)} each` : ''}.
        </div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead><tr style={{ color: DIM, textAlign: 'left' }}><th>trace</th><th style={{ textAlign: 'right' }}>cost</th><th>Phoenix</th></tr></thead>
          <tbody>
            {proof.traces.map((t, i) => (
              <tr key={i}>
                <td style={{ fontFamily: 'monospace', padding: '2px 4px' }}>{t.trace_id.slice(0, 12)}…</td>
                <td style={{ textAlign: 'right', padding: '2px 4px' }}>${t.total.toFixed(5)}</td>
                <td style={{ padding: '2px 4px' }}>{t.phoenix_url && <a href={t.phoenix_url} target="_blank" rel="noreferrer">open ↗</a>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ fontSize: 12, color: DIM, marginTop: 10 }}>System behaviour only — span names, counts, cost. No prompt text or PII.</div>
      </div>
    </div>
  )
}

// ---- the system map: a LANE per workload, its ops, the lever on each -------
const leverState = (l) => l.active ? 'green' : l.escalated ? 'escalate' : l.vetoed ? 'vetoed' : 'amber'
function opLabel(op) {
  if (op.kind === 'model') return 'model'
  return op.count ? `${op.op} ×${Math.round(op.count)}` : op.op
}
function opSub(op, lever) {
  if (op.governed) return op.kind === 'model' ? 'routed → economy' : 'cached · $0'
  if (lever?.escalated) return 'answer-affecting · your call'
  if (lever?.vetoed) return 'off — you vetoed this'
  if (op.kind === 'model') return lever ? 'premium · routable' : 'premium model'
  return lever ? 'paying — the agent will cache' : 'paying'
}
function buildGraph(state, act) {
  if (!state || !state.classes) return { nodes: [], edges: [] }
  const leverBySig = Object.fromEntries((state.levers || []).map((l) => [l.sig, l]))
  const nodes = [], edges = []
  const laneH = 92
  state.classes.forEach((cls, i) => {
    const y = 22 + i * laneH
    const cid = 'cls_' + cls.tc
    nodes.push(nd(cid, 12, y, {
      kind: 'class', label: cls.label, proofNode: cls.tc,
      sub: `$${cls.cost_per_ticket.toFixed(4)}/ticket · ${Math.round(cls.share * 100)}% of spend`,
      state: cls.governed === true ? 'green' : cls.governed === false ? 'amber' : 'struct',
    }))
    let prev = cid
    cls.ops.forEach((op, j) => {
      const oid = cid + '_' + op.op
      const lever = op.lever ? leverBySig[op.lever] : null
      nodes.push(nd(oid, 250 + j * 168, y, {
        kind: lever ? 'lever' : 'op', label: opLabel(op), sub: opSub(op, lever),
        state: op.governed ? 'green' : lever ? leverState(lever) : (op.kind === 'model' ? 'struct' : 'amber'),
        sig: op.lever, active: lever?.active, vetoed: lever?.vetoed, escalated: lever?.escalated,
        act, proofNode: cls.tc,
      }))
      edges.push(ed(oid + '_e', prev, oid, op.governed ? GREEN : '#c8c7c0'))
      prev = oid
    })
  })
  return { nodes, edges }
}
function nd(id, x, y, data) { return { id, type: 'ctrl', position: { x, y }, data, draggable: false } }
function ed(id, s, t, color) {
  return { id, source: s, target: t, animated: true, style: { stroke: color, strokeWidth: 2 } }
}
