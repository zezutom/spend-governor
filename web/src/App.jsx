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
//  The DEBUGGER — a sandbox lab over the live system. Set up, RUN (real past
//  conversations stream through the canvas, distribution accumulates live),
//  PAUSE / STEP call-by-call (the cursor lingers inside a ×N box so you watch
//  the same call fire repeatedly — the waste), with a shared inspector that
//  follows the cursor and, at the model call, shows premium-vs-economy quality.
//  Real 'test'-tagged Phoenix traces; never touches production.
// ===========================================================================
const LAB_USE_CASES = [
  { key: 'account_question', label: 'Account questions' },
  { key: 'refund_handling', label: 'Refund tickets' },
]

// group a conversation's calls into canvas boxes (consecutive same-tool → ×N),
// each box remembering which call indices live inside it (so a ×N box stays lit
// while the cursor steps through its repeats).
function convBoxes(calls) {
  if (!calls) return []
  const boxes = []
  calls.forEach((c, i) => {
    if (c.kind === 'tool') {
      const last = boxes[boxes.length - 1]
      if (last && last.kind === 'tool' && last.tool === c.tool) {
        last.count++; last.calls.push(i); last.dup = last.dup || c.dup
      } else boxes.push({ kind: 'tool', tool: c.tool, count: 1, calls: [i], dup: c.dup })
    } else if (c.kind === 'model') boxes.push({ kind: 'model', calls: [i] })
  })
  return boxes
}

function buildStepGraph(boxes, litIdx) {
  const nodes = [], edges = []
  let x = 8
  boxes.forEach((b, i) => {
    const lit = i === litIdx
    const label = b.kind === 'model' ? 'model' : `${b.tool}${b.count > 1 ? ' ×' + b.count : ''}`
    const sub = b.kind === 'model' ? 'premium → economy' : (b.dup ? '⚠ repeated' : 'tool')
    const state = lit ? 'escalate' : (b.kind === 'model' ? 'amber' : 'struct')
    nodes.push(nd('b' + i, x, 44, { kind: 'op', label, sub, state }))
    if (i > 0) edges.push(ed('e' + i, 'b' + (i - 1), 'b' + i, lit ? AMBER : '#c8c7c0'))
    x += 150
  })
  return { nodes, edges }
}

function phoenixTestUrl(gid) {
  // the project's spans table (reliable); Phoenix doesn't honor a filter via URL,
  // so we land on the table and tell the operator the tag to filter by.
  const base = 'https://app.phoenix.arize.com/s/tomas'
  return gid ? `${base}/projects/${gid}/spans` : base
}

// derive any {cache, economy} config's impact from ONE run's rows — no re-run.
// cache removes duplicate tool costs (output-preserving); economy swaps the model
// cost and carries the judge verdict. Baseline = all levers OFF (premium, no cache).
function deriveImpact(rows, config) {
  let base = 0, cfg = 0, held = 0
  const degraded = []
  rows.forEach((r, i) => {
    const tools = r.calls.filter((c) => c.kind === 'tool')
    const toolAll = tools.reduce((s, c) => s + (c.cost || 0), 0)
    const toolKept = tools.reduce((s, c) => s + ((config.cache && c.dup) ? 0 : (c.cost || 0)), 0)
    base += (r.baseline_model_cost || 0) + toolAll
    cfg += (config.economy ? (r.economy_model_cost || 0) : (r.baseline_model_cost || 0)) + toolKept
    const rowHeld = config.economy ? r.held : true   // cache is output-preserving
    if (rowHeld) held++; else degraded.push({ ...r, _i: i })
  })
  const n = rows.length || 1
  return { savedPct: base ? Math.max(0, 1 - cfg / base) : 0, heldPct: held / n,
    degradedPct: 1 - held / n, degraded, savedPerMsg: (base - cfg) / n }
}

function Toggle({ on, onClick, title, sub, risky }) {
  const accent = risky ? AMBER : GREEN
  return (
    <div onClick={onClick} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 11px',
      cursor: 'pointer', borderRadius: 10, marginBottom: 8,
      border: `1px solid ${on ? accent : '#e0ded3'}`, background: on ? (risky ? '#faeeda' : '#eef7f1') : '#fff' }}>
      <div style={{ width: 34, height: 20, borderRadius: 999, background: on ? accent : '#cfcdc2', position: 'relative', transition: 'background .2s' }}>
        <div style={{ position: 'absolute', top: 2, left: on ? 16 : 2, width: 16, height: 16, borderRadius: 999, background: '#fff', transition: 'left .2s' }} />
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 13.5, fontWeight: 600, color: INK }}>{title}</div>
        <div style={{ fontSize: 11.5, color: on ? (risky ? '#85540b' : '#1a4f40') : DIM }}>{sub}</div>
      </div>
    </div>
  )
}

function ReplayLab({ onClose }) {
  const [uc, setUc] = useState('account_question')
  const [data, setData] = useState(null)
  const [config, setConfig] = useState({ cache: true, economy: false })  // candidate; toggles instant
  const [selConv, setSelConv] = useState(0)
  const [cursorCall, setCursorCall] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [bp, setBp] = useState('none')
  const [source, setSource] = useState('replay')   // replay | synthetic
  const [N, setN] = useState(12)                    // adjustable run size
  const [ran, setRan] = useState(false)            // a load test has been run
  const [running, setRunning] = useState(false)
  const [runRows, setRunRows] = useState([])        // rows from the live run
  const [runSource, setRunSource] = useState('replay')
  const [trickle, setTrickle] = useState(null)
  const [confirm, setConfirm] = useState(false)    // close-to-apply
  const esRef = useRef(null)

  useEffect(() => {
    setData(null); setRan(false); setRunRows([]); esRef.current?.close()
    fetch(`${API}/api/lab/${uc}`).then((r) => (r.ok ? r.json() : null)).then(setData).catch(() => {})
  }, [uc])
  useEffect(() => () => esRef.current?.close(), [])
  useEffect(() => {   // default to a representative degraded-with-dup conversation
    if (!data) return
    let i = data.rows.findIndex((r) => !r.held && r.calls.some((c) => c.dup))
    if (i < 0) i = data.rows.findIndex((r) => !r.held)
    if (i < 0) i = 0
    setSelConv(i); setCursorCall(0); setPlaying(false)
  }, [data])

  // active dataset for the STEPPER — never empty once data loads (the live run's
  // rows once it produces any, else the pre-run), so selRow is always valid. The
  // impact panel guards separately on the run state to avoid showing stale numbers.
  const rows = runRows.length ? runRows : (data?.rows || [])
  const selRow = rows[Math.min(selConv, Math.max(0, rows.length - 1))]
  useEffect(() => {   // play walks the chosen conversation call-by-call
    if (!playing || !selRow) return
    if (cursorCall >= selRow.calls.length - 1) { setPlaying(false); return }
    const t = setTimeout(() => {
      const next = cursorCall + 1, nc = selRow.calls[next]
      if (bp === 'model' && nc.kind === 'model') { setCursorCall(next); setPlaying(false); return }
      if (bp === 'dup' && nc.dup) { setCursorCall(next); setPlaying(false); return }
      setCursorCall(next)
    }, 750)
    return () => clearTimeout(t)
  }, [playing, cursorCall, selRow, bp])

  const playC = () => { if (selRow && cursorCall >= selRow.calls.length - 1) setCursorCall(0); setPlaying(true) }
  const stepC = () => { setPlaying(false); setCursorCall((c) => Math.min(c + 1, (selRow?.calls.length || 1) - 1)) }
  const restartC = () => { setPlaying(false); setCursorCall(0) }
  const pickConv = (i) => { setSelConv(i); setCursorCall(0); setPlaying(false) }
  const run = () => {   // EXECUTE a fresh load test live; rows stream in as they land
    setRan(true); setRunning(true); setRunRows([]); setRunSource(source)
    setSelConv(0); setCursorCall(0); setPlaying(false)
    esRef.current?.close()
    const es = new EventSource(`${API}/api/lab/${uc}/run?n=${N}&source=${source}`)
    esRef.current = es
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data)
      if (ev.row) setRunRows((rs) => [...rs, ev.row])
      if (ev.done) { es.close(); setRunning(false) }
    }
    es.onerror = () => { es.close(); setRunning(false) }
  }

  const boxes = useMemo(() => convBoxes(selRow?.calls), [selRow])
  const litBox = boxes.findIndex((b) => b.calls.includes(cursorCall))
  const { nodes, edges } = useMemo(() => buildStepGraph(boxes, litBox), [boxes, litBox])
  const call = selRow?.calls?.[cursorCall]
  const imp = useMemo(() => (rows.length ? deriveImpact(rows, config) : null), [rows, config])
  const proj = imp && data?.monthly_volume ? imp.savedPerMsg * data.monthly_volume : null
  const anyOn = config.cache || config.economy
  const tryClose = () => { if (anyOn) setConfirm(true); else onClose() }
  const apply = () => {
    const q = `use_case=${uc}&cache=${config.cache}&economy=${config.economy}&held_pct=${imp ? imp.heldPct : ''}&n=${rows.length || data?.n || ''}`
    fetch(`${API}/api/lab/apply?${q}`, { method: 'POST' }).catch(() => {}).finally(onClose)
  }
  const openDegraded = (i) => { setSelConv(i); setCursorCall(0); setPlaying(false) }

  return (
    <div onClick={tryClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 65 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: '#fff', borderRadius: 14, padding: '16px 20px',
        width: 'min(96vw, 1460px)', height: '90vh', display: 'flex', flexDirection: 'column', position: 'relative',
        boxShadow: '0 16px 56px rgba(0,0,0,.34)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div><b style={{ fontSize: 16 }}>DEBUGGER</b> <span style={{ color: DIM, fontSize: 13 }}>· sandbox · your lab</span></div>
          <button onClick={tryClose} style={{ border: 'none', background: 'none', fontSize: 24, cursor: 'pointer', color: DIM }}>×</button>
        </div>

        {/* setup + transport (drives left/center) */}
        <div style={{ display: 'flex', gap: 9, alignItems: 'center', marginTop: 12, flexWrap: 'wrap' }}>
          <select value={uc} onChange={(e) => setUc(e.target.value)}
            style={{ fontSize: 13, padding: '5px 9px', borderRadius: 999, border: '1px solid #d8d6cc' }}>
            {LAB_USE_CASES.map((u) => <option key={u.key} value={u.key}>{u.label}</option>)}
          </select>
          {rows.length > 0 && <select value={Math.min(selConv, rows.length - 1)} onChange={(e) => pickConv(+e.target.value)}
            style={{ fontSize: 13, padding: '5px 9px', borderRadius: 8, border: '1px solid #d8d6cc', maxWidth: 320 }}>
            {rows.map((r, i) => <option key={i} value={i}>#{r.conv_id} · {r.ticket.slice(0, 30)} · {r.held ? 'held' : 'degraded'}</option>)}
          </select>}
          <div style={{ width: 1, height: 22, background: '#e6e4da', margin: '0 4px' }} />
          <button onClick={playC} style={transBtn(playing)}>▶ play</button>
          <button onClick={() => setPlaying(false)} style={transBtn(!playing && cursorCall > 0)}>⏸ pause</button>
          <button onClick={stepC} style={transBtn(false)}>⏭ step</button>
          <button onClick={restartC} style={transBtn(false)}>⟲ restart</button>
          <select value={bp} onChange={(e) => setBp(e.target.value)} title="breakpoint"
            style={{ fontSize: 12, padding: '5px 8px', borderRadius: 999, border: '1px solid #d8d6cc', color: bp === 'none' ? DIM : AMBER }}>
            <option value="none">● no breakpoint</option>
            <option value="model">● break on the model call</option>
            <option value="dup">● break on a duplicate call</option>
          </select>
          <span style={{ marginLeft: 'auto', fontSize: 11.5, color: DIM }}>sandbox · live untouched · traces tagged 'test'</span>
        </div>

        {!data ? <div style={{ color: DIM, padding: 24 }}>Loading the pre-run batch…</div> : (
          <div style={{ flex: 1, minHeight: 0, display: 'flex', gap: 14, marginTop: 12, borderTop: '1px solid #eceae0', paddingTop: 12 }}>
            {/* LEFT — call list (UNCHANGED) */}
            <div style={{ width: 290, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
              <div style={{ fontSize: 11, color: DIM, letterSpacing: '.05em' }}>
                conv #{selRow.conv_id} · call {cursorCall + 1} of {selRow.calls.length}
              </div>
              <div style={{ overflowY: 'auto', flex: 1, marginTop: 8 }}>
                {selRow.calls.map((c, i) => {
                  const here = i === cursorCall
                  return (
                    <div key={i} onClick={() => { setPlaying(false); setCursorCall(i) }}
                      style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 9px', cursor: 'pointer',
                        borderRadius: 7, marginBottom: 2, background: here ? '#f5f4ed' : 'transparent',
                        border: here ? '1px solid #d8d6cc' : '1px solid transparent' }}>
                      <span style={{ width: 16, color: DIM, fontSize: 12 }}>{i + 1}</span>
                      <span style={{ flex: 1, fontSize: 13, fontWeight: here ? 600 : 400, color: INK }}>
                        {c.kind === 'user' ? c.label : c.kind === 'tool' ? c.tool : c.kind === 'model' ? 'model · respond' : 'reply sent'}
                      </span>
                      {c.dup && <span style={{ fontSize: 11, color: AMBER }}>⚠ dup</span>}
                      {c.cost ? <span style={{ fontSize: 12, color: '#555' }}>${c.cost.toFixed(4)}</span> : null}
                      {here && <span style={{ fontSize: 11, fontWeight: 700, color: INK }}>◀ HERE</span>}
                    </div>
                  )
                })}
              </div>
            </div>
            {/* CENTER — path + span detail (UNCHANGED) */}
            <div style={{ flex: 1, minWidth: 360, display: 'flex', flexDirection: 'column', borderLeft: '1px solid #eceae0', paddingLeft: 14 }}>
              <div style={{ fontSize: 11, color: DIM, letterSpacing: '.05em' }}>conv #{selRow.conv_id} · path · current call lit</div>
              <div style={{ height: 110 }}>
                <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView
                  proOptions={{ hideAttribution: true }} nodesDraggable={false} nodesConnectable={false}
                  onNodeClick={(e, n) => { const b = boxes[+n.id.slice(1)]; if (b) { setPlaying(false); setCursorCall(b.calls[0]) } }}
                  panOnDrag={false} zoomOnScroll={false} zoomOnDoubleClick={false}>
                  <Background color="#eeede6" gap={18} />
                </ReactFlow>
              </div>
              <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', marginTop: 8 }}>
                <CallInspector call={call} row={selRow} />
              </div>
            </div>
            {/* RIGHT — NEW: lever rail + run + impact + degraded */}
            <div style={{ width: 380, display: 'flex', flexDirection: 'column', minHeight: 0, borderLeft: '1px solid #eceae0', paddingLeft: 14 }}>
              <div style={{ overflowY: 'auto', flex: 1, minHeight: 0, paddingRight: 4 }}>
                <div style={{ fontSize: 11, color: DIM, letterSpacing: '.05em', marginBottom: 8 }}>EXPERIMENT · levers apply instantly</div>
                <Toggle on={config.cache} onClick={() => setConfig((c) => ({ ...c, cache: !c.cache }))}
                  title="cache repeated calls" sub="safe · keeps answers" />
                <Toggle on={config.economy} onClick={() => setConfig((c) => ({ ...c, economy: !c.economy }))}
                  title="economy model" sub="affects answers" risky />

                {/* run controls — a real, live re-measure */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                  <button onClick={run} disabled={running} style={{ fontSize: 13, fontWeight: 700,
                    cursor: running ? 'default' : 'pointer', color: '#fff', background: running ? '#9bbcae' : GREEN,
                    border: 'none', borderRadius: 8, padding: '7px 14px' }}>
                    {running ? `running ${runRows.length}/${N}…` : '▶ run load test'}</button>
                  <span style={{ fontSize: 12, color: DIM }}>N</span>
                  <select value={N} onChange={(e) => setN(+e.target.value)} disabled={running}
                    style={{ fontSize: 12.5, padding: '4px 7px', borderRadius: 7, border: '1px solid #d8d6cc' }}>
                    {[6, 12, 18, 24].map((v) => <option key={v} value={v}>{v}</option>)}
                  </select>
                  <select value={source} onChange={(e) => setSource(e.target.value)} disabled={running}
                    style={{ fontSize: 12.5, padding: '4px 7px', borderRadius: 7, border: `1px solid ${source === 'synthetic' ? AMBER : '#d8d6cc'}`, color: source === 'synthetic' ? AMBER : INK }}>
                    <option value="replay">replay real</option>
                    <option value="synthetic">synthetic</option>
                  </select>
                </div>

                {!ran && !running && <div style={{ color: DIM, fontSize: 12.5, padding: '16px 2px' }}>Run the load test to measure this config across N real {source === 'synthetic' ? 'synthetic' : 'replayed'} conversations.</div>}

                {/* running banner — shows IMMEDIATELY on click, before the first row */}
                {running && <div style={{ margin: '14px 0 6px', border: `1px solid ${GREEN}`, background: '#eef7f1', borderRadius: 10, padding: '11px 12px' }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: GREEN }}>
                    <span style={{ animation: 'pulse 1s infinite' }}>●</span> running live — {runRows.length}/{N} {runSource === 'synthetic' ? 'synthetic' : 'real'} replays
                  </div>
                  <div style={{ height: 6, borderRadius: 4, background: '#dceee6', marginTop: 8, overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${Math.round((runRows.length / N) * 100)}%`, background: GREEN, transition: 'width .3s' }} />
                  </div>
                  <div style={{ fontSize: 11, color: DIM, marginTop: 6 }}>
                    {runRows.length === 0 ? 'executing the first replays — each takes a few seconds, results stream in as they land' : 'impact updates as each replay lands'}
                  </div>
                  <style>{'@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}'}</style>
                </div>}

                {ran && imp && (runRows.length > 0 || !running) && <>
                  {/* COST IMPACT — measured vs estimated, visually distinct */}
                  <div style={{ fontSize: 11, color: DIM, letterSpacing: '.05em', margin: '14px 0 8px' }}>COST IMPACT</div>
                  <div style={{ display: 'flex', gap: 10 }}>
                    <div style={{ flex: 1, border: `1.5px solid ${GREEN}`, background: '#eef7f1', borderRadius: 10, padding: '11px 12px' }}>
                      <div style={{ fontSize: 26, fontWeight: 800, color: GREEN, lineHeight: 1 }}>−{Math.round(imp.savedPct * 100)}%</div>
                      <div style={{ fontSize: 11.5, color: '#1a4f40', marginTop: 4 }}>saved vs baseline</div>
                      <div style={{ fontSize: 10.5, color: DIM }}>measured · {rows.length} {(ran ? runSource : source) === 'synthetic' ? 'synthetic' : 'real'} replays</div>
                    </div>
                    <div style={{ flex: 1, border: '1.5px dashed #c8b88f', background: '#fbf8f1', borderRadius: 10, padding: '11px 12px' }}>
                      <div style={{ fontSize: 22, fontWeight: 800, color: '#85540b', lineHeight: 1 }}>~${proj != null ? Math.round(proj).toLocaleString() : '—'}<span style={{ fontSize: 12, fontWeight: 600 }}>/mo</span></div>
                      <div style={{ fontSize: 11.5, color: '#85540b', marginTop: 4 }}>projected in production</div>
                      <div style={{ fontSize: 10.5, color: DIM }}>estimate, not Phoenix-backed</div>
                    </div>
                  </div>

                  {/* QUALITY IMPACT */}
                  <div style={{ fontSize: 11, color: DIM, letterSpacing: '.05em', margin: '16px 0 7px' }}>QUALITY IMPACT</div>
                  <div style={{ display: 'flex', height: 14, borderRadius: 5, overflow: 'hidden', background: '#f0eee6' }}>
                    <div style={{ width: `${Math.round(imp.heldPct * 100)}%`, background: GREEN, opacity: 0.6 }} />
                    <div style={{ width: `${Math.round(imp.degradedPct * 100)}%`, background: AMBER, opacity: 0.55 }} />
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontSize: 12.5 }}>
                    <span style={{ color: GREEN }}>held {Math.round(imp.heldPct * 100)}%</span>
                    <span style={{ color: AMBER }}>degraded {Math.round(imp.degradedPct * 100)}%</span>
                  </div>
                  {!config.economy && <div style={{ fontSize: 11.5, color: DIM, marginTop: 6 }}>cache is output-preserving — quality holds by construction.</div>}

                  {/* degraded drill-down */}
                  {imp.degraded.length > 0 && <>
                    <div style={{ fontSize: 11, color: DIM, letterSpacing: '.05em', margin: '14px 0 6px' }}>DEGRADED CASES ({imp.degraded.length})</div>
                    {imp.degraded.map((r) => (
                      <div key={r._i} style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '6px 8px', fontSize: 12.5,
                        borderBottom: '1px solid #f1efe8' }}>
                        <span style={{ flex: 1, color: '#3b3b37' }}>#{r.conv_id} · {r.ticket.slice(0, 22)}</span>
                        <span style={{ color: AMBER, fontWeight: 600 }}>q{r.economy_quality}</span>
                        <button onClick={() => openDegraded(r._i)} style={{ fontSize: 11.5, color: GREEN, fontWeight: 600,
                          background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>open</button>
                        {r.phoenix_url && <SpanLink url={redirectUrl({}, r)} label="trace ↗" />}
                      </div>
                    ))}
                  </>}

                  {trickle && <div style={{ marginTop: 12, fontSize: 11.5, color: DIM }}>
                    ● live replay landed: <b style={{ color: trickle.held ? GREEN : AMBER }}>{trickle.held ? 'held' : 'degraded'}</b> (q{trickle.economy_quality}) — real, not canned
                  </div>}
                </>}
              </div>

              {/* source + Phoenix (facts) */}
              <div style={{ borderTop: '1px solid #eceae0', paddingTop: 8, marginTop: 8 }}>
                <span style={{ fontSize: 10.5, color: (ran ? runSource : source) === 'synthetic' ? AMBER : DIM, fontWeight: 600 }}>
                  source: {(ran ? runSource : source) === 'synthetic' ? 'synthetic · exploration (not real-traffic proof)' : 'real replays · evidence'}
                </span>
                <div style={{ marginTop: 4 }}>
                  <a href={phoenixTestUrl(data.project_gid)} target="_blank" rel="noreferrer"
                    style={{ fontSize: 11.5, color: GREEN, fontWeight: 600 }}>this run's 'test' traces ↗</a>
                  <span style={{ fontSize: 10.5, color: DIM, marginLeft: 8 }}>{data.test_tag || 'accountant.run_type = test'}</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* CLOSE-TO-APPLY confirmation */}
        {confirm && data && <div style={{ position: 'absolute', inset: 0, background: 'rgba(255,255,255,.86)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: 14 }}>
          <div style={{ width: 460, background: '#fff', border: '1px solid #e0ded3', borderRadius: 14, padding: 22, boxShadow: '0 12px 44px rgba(0,0,0,.2)' }}>
            <div style={{ fontSize: 17, fontWeight: 700 }}>Apply your changes to real data?</div>
            <div style={{ fontSize: 13, color: '#3b3b37', marginTop: 10 }}>
              Applying to <b>{LAB_USE_CASES.find((u) => u.key === uc)?.label}</b>:
            </div>
            <ul style={{ fontSize: 13.5, color: INK, margin: '6px 0 0 18px' }}>
              {config.cache && <li>cache repeated calls <span style={{ color: GREEN }}>· safe</span></li>}
              {config.economy && <li>economy model <span style={{ color: AMBER }}>· affects answers</span></li>}
            </ul>
            {config.economy && imp && <div style={{ fontSize: 12.5, color: '#85540b', background: '#faeeda',
              border: `1px solid ${AMBER}`, borderRadius: 9, padding: '10px 12px', marginTop: 12 }}>
              ⚑ economy held {Math.round(imp.heldPct * 100)}% / {rows.length || data.n} replays — I'd keep premium. You can apply anyway; I'll watch live and flag if quality slips.
            </div>}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 18 }}>
              <button onClick={onClose} style={{ fontSize: 13, cursor: 'pointer', border: '1px solid #d8d6cc', background: '#fff', borderRadius: 8, padding: '7px 14px', color: '#5a5852' }}>close without applying</button>
              <button onClick={apply} style={{ fontSize: 13, fontWeight: 700, cursor: 'pointer', border: 'none', background: GREEN, color: '#fff', borderRadius: 8, padding: '7px 16px' }}>apply to production</button>
            </div>
          </div>
        </div>}
      </div>
    </div>
  )
}
// the shared inspector — renders whatever the cursor points at; the model call
// is the payoff (premium vs economy side by side).
function CallInspector({ call, row }) {
  if (!call) return <div style={{ color: DIM, fontSize: 13 }}>Step or click a box to inspect a call.</div>
  // each call links to its OWN span via Phoenix's /redirects/spans/<otel-id> route;
  // user/reply and the model call (no captured span id) open the trace.
  const span = redirectUrl(call, row)
  const spanLabel = call.span_id ? 'open this span in Phoenix ↗' : 'open the trace in Phoenix ↗'
  if (call.kind === 'user')
    return <InsShell title="user message" accent={DIM} span={span} spanLabel={spanLabel}>
      <div style={{ fontSize: 14.5, color: INK, lineHeight: 1.45 }}>{row.ticket}</div></InsShell>
  if (call.kind === 'reply')
    return <InsShell title="reply sent · what the customer got" accent={DIM} span={span} spanLabel={spanLabel}>
      <div style={{ fontSize: 13.5, color: '#3b3b37', lineHeight: 1.45 }}>{row.economy_answer}</div></InsShell>
  if (call.kind === 'tool')
    return <InsShell title={`${call.tool} · tool call`} accent={AMBER} span={span} spanLabel={spanLabel}>
      <FactGrid facts={[
        ['cost', <>${(call.cost || 0).toFixed(4)} <span style={{ color: AMBER }}>your rate</span></>],
        ['source', 'configured rate · not Phoenix-priced'],
        ['latency', 'served instantly (synthetic tool)'],
        ['repeat', call.dup ? <span style={{ color: AMBER }}>⚠ duplicate this conversation</span> : 'first call'],
      ]} />
      <Field label="input — what the agent asked">{call.input}</Field>
      <Field label="output — what came back (trimmed)">{call.output}</Field>
      {call.dup && <div style={{ marginTop: 8, fontSize: 12.5, color: AMBER }}>
        ⚠ the same lookup already fired earlier in this conversation — exactly the redundant call the cache removes.</div>}
    </InsShell>
  // model call — the payoff: the candidate diff + the real model telemetry
  const bites = row.economy_quality < row.baseline_quality
  return <InsShell title="model · respond — where the candidate bites" accent={bites ? AMBER : GREEN} span={span} spanLabel={spanLabel}>
    <div style={{ border: `1px solid ${GREEN}`, background: '#e1f5ee', borderRadius: 8, padding: '10px 12px' }}>
      <div style={{ fontSize: 12.5, fontWeight: 700 }}>premium (baseline) · judge quality {row.baseline_quality}/5</div>
      <div style={{ fontSize: 13, color: '#1a4f40', marginTop: 4, lineHeight: 1.4 }}>{row.baseline_answer}</div>
    </div>
    <div style={{ border: `1px solid ${AMBER}`, background: '#faeeda', borderRadius: 8, padding: '10px 12px', marginTop: 8 }}>
      <div style={{ fontSize: 12.5, fontWeight: 700 }}>economy (candidate) · judge quality {row.economy_quality}/5 {bites ? '⚠' : ''}</div>
      <div style={{ fontSize: 13, color: '#85540b', marginTop: 4, lineHeight: 1.4 }}>{row.economy_answer}</div>
    </div>
    <div style={{ marginTop: 10 }}><FactGrid facts={[
      ['model', call.model || '—'],
      ['round-trip', call.latency_ms != null ? `${call.latency_ms} ms` : '—'],
      ['tokens', call.in_tokens != null ? `${call.in_tokens} in → ${call.out_tokens} out` : '—'],
      ['cost', <>${(call.cost || 0).toFixed(5)} <span style={{ color: GREEN }}>Phoenix-measured</span></>],
    ]} /></div>
    <div style={{ fontSize: 12.5, color: bites ? AMBER : GREEN, marginTop: 8 }}>
      → this replay {row.held ? 'holds' : 'degrades'} under the candidate
    </div>
  </InsShell>
}
function Field({ label, children }) {
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 10.5, color: DIM, letterSpacing: '.04em', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 12.5, fontFamily: 'monospace', color: '#3b3b37', background: '#f7f6f1',
        border: '1px solid #eceae0', borderRadius: 7, padding: '7px 9px', marginTop: 3,
        whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.4 }}>{children || '—'}</div>
    </div>
  )
}
function FactGrid({ facts }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px 16px' }}>
      {facts.map(([k, v], i) => (
        <div key={i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13,
          borderBottom: '1px solid #f1efe8', paddingBottom: 3 }}>
          <span style={{ color: DIM }}>{k}</span><span style={{ color: INK, textAlign: 'right' }}>{v}</span>
        </div>
      ))}
    </div>
  )
}
// Phoenix's supported deeplink route: /redirects/spans/<otel-span-id> resolves a
// span by its standard OTel id server-side (and /redirects/traces/<otel-trace-id>
// for a trace). Unlike the ?selectedSpanNodeId= deeplink — which Phoenix drops on
// a clicked link — the redirect route survives a click. We already capture the
// OTel span_id per tool call, so we point straight at it.
function redirectUrl(call, row) {
  const base = (row.phoenix_url || '').split('/projects/')[0]   // …/s/tomas
  if (!base) return null
  if (call.span_id) return `${base}/redirects/spans/${call.span_id}`
  const tid = (row.phoenix_url || '').split('/spans/')[1]?.split('?')[0]
  return tid ? `${base}/redirects/traces/${tid}` : null
}
function SpanLink({ url, label }) {
  return (
    <a href={url} target="_blank" rel="noreferrer"
      style={{ fontSize: 12, color: GREEN, fontWeight: 600, whiteSpace: 'nowrap' }}>{label || 'open in Phoenix ↗'}</a>
  )
}
function InsShell({ title, accent, span, spanLabel, children }) {
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div style={{ fontSize: 11, letterSpacing: '.05em', color: accent, fontWeight: 800, textTransform: 'uppercase' }}>{title}</div>
        {span && <SpanLink url={span} label={spanLabel} />}
      </div>
      <div style={{ marginTop: 8 }}>{children}</div>
    </div>
  )
}
function labChip(amber) {
  return { fontSize: 12.5, padding: '5px 11px', borderRadius: 999,
    border: `1px solid ${amber ? AMBER : '#d8d6cc'}`, color: amber ? AMBER : '#5a5852' }
}
function transBtn(active) {
  return { fontSize: 12, fontWeight: 600, cursor: 'pointer', padding: '5px 11px', borderRadius: 999,
    border: `1px solid ${active ? '#5a4815' : '#d8d6cc'}`, color: active ? '#fff' : '#5a5852',
    background: active ? '#5a4815' : '#fff' }
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
