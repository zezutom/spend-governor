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
    style={{ fontSize: big ? 15 : 13, borderRadius: 8, padding: big ? '10px 22px' : '6px 13px',
      cursor: 'pointer', fontWeight: primary ? 800 : 600,
      border: `1px solid ${color}`, color: primary ? '#fff' : color,
      background: primary ? color : '#fff' }}>{label}</button>
}

// ---- system-map control node ----------------------------------------------
// Color grammar (no legend): GREEN = resolved/safe (cached), AMBER = costly-but-fine
// (cost-heat), LIGHT RED = the specific node a decision is about. Plus a cost-heat
// ramp on lanes (warm = more spend) and an "agent here" spotlight.
// Live status grammar for a fleet agent.
const _STATUS = {
  governed: { label: 'governed', color: GREEN, bg: '#e6f5ef', icon: '✓' },
  your_call: { label: 'your call', color: RED, bg: '#fdecea', icon: '⚑' },
  problem: { label: 'fixing…', color: AMBER, bg: '#fbf0db', icon: '⚡' },
  off: { label: 'off', color: '#8f8f86', bg: '#efeee8', icon: '○' },
  watching: { label: 'watching', color: DIM, bg: '#f5f4ef', icon: '◔' },
}
const _FIXNAME = { cache_tool: 'cache', limit_tool_calls: 'cap', suppress_tool: 'suppress', route_model: 'route' }

function RootNode({ data }) {
  return (
    <div style={{ position: 'relative', border: `2px solid ${INK}`, background: INK, color: '#fff',
      borderRadius: 16, padding: '14px 22px', minWidth: 230, textAlign: 'center',
      boxShadow: '0 10px 30px rgba(0,0,0,.22)' }}>
      <div style={{ fontSize: 11, letterSpacing: '.12em', textTransform: 'uppercase', color: '#b9f5e2', fontWeight: 800 }}>the accountant</div>
      <div style={{ fontSize: 20, fontWeight: 800, marginTop: 2 }}>Cost Governor</div>
      <div style={{ fontSize: 12.5, color: '#cfece3', marginTop: 3 }}>{data.sub}</div>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

function AgentNode({ data }) {
  const a = data.agent, act = data.act
  const st = _STATUS[a.status] || _STATUS.watching
  const fix = a.fix || {}
  const lit = a.status === 'your_call'
  return (
    <div onClick={() => data.onInspect && data.onInspect(a.id)}
      style={{ position: 'relative', width: 246, border: `2px solid ${lit ? RED : st.color + '55'}`,
        background: '#fff', borderRadius: 15, padding: '13px 15px', cursor: 'pointer',
        boxShadow: lit ? `0 0 0 4px ${RED}22, 0 10px 26px rgba(0,0,0,.14)` : '0 6px 18px rgba(0,0,0,.10)',
        transition: 'box-shadow .3s, border-color .3s' }}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      {/* header: status pill + cost */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 11, fontWeight: 800, color: st.color, background: st.bg,
          border: `1px solid ${st.color}33`, borderRadius: 999, padding: '2px 9px',
          display: 'inline-flex', alignItems: 'center', gap: 4 }}>{st.icon} {st.label}</span>
        <span style={{ marginLeft: 'auto', fontSize: 16, fontWeight: 800, color: INK }}>
          ${a.cost_per_message.toFixed(4)}<span style={{ fontSize: 10.5, fontWeight: 600, color: DIM }}>/msg</span></span>
      </div>
      <div style={{ fontSize: 17, fontWeight: 800, color: INK, marginTop: 8 }}>{a.label}</div>
      <div style={{ fontSize: 12.5, color: DIM, marginTop: 1 }}>{a.purpose}</div>
      {/* waste line */}
      <div style={{ fontSize: 12.5, marginTop: 9, color: a.status === 'governed' ? GREEN : AMBER,
        fontWeight: 600, display: 'flex', alignItems: 'center', gap: 5 }}>
        <span>{a.status === 'governed' ? '✓' : '⚠'}</span>{a.waste}
      </div>
      {/* fix block */}
      <div style={{ marginTop: 10, paddingTop: 9, borderTop: '1px solid #f0eee6' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <span style={{ fontSize: 11, fontWeight: 800, letterSpacing: '.05em', textTransform: 'uppercase',
            color: fix.safe ? GREEN : AMBER }}>{_FIXNAME[fix.type] || 'fix'}</span>
          <span style={{ fontSize: 12.5, color: '#3b3b37' }}>{fix.title}</span>
          <span style={{ marginLeft: 'auto', fontSize: 13.5, fontWeight: 800, color: GREEN }}>
            ${Math.round(fix.monthly || 0).toLocaleString()}<span style={{ fontSize: 10, color: DIM, fontWeight: 600 }}>/mo</span></span>
        </div>
        <div style={{ marginTop: 9, display: 'flex', gap: 7, flexWrap: 'wrap' }}>
          {fix.escalated && <>
            <Btn onClick={() => act('accept', fix.sig)} label="arm it" color={AMBER} primary />
            <Btn onClick={() => act('reject', fix.sig)} label="not now" color={DIM} />
          </>}
          {fix.active && <Btn onClick={() => act('veto', fix.sig)} label="revert" color={DIM} />}
          {fix.vetoed && <Btn onClick={() => act('enable', fix.sig)} label="re-enable" color={AMBER} primary />}
          {!fix.escalated && !fix.active && !fix.vetoed &&
            <span style={{ fontSize: 11.5, color: DIM }}>{a.status === 'problem' ? 'agent acting…' : 'watching the traffic'}</span>}
        </div>
      </div>
    </div>
  )
}
// Compact op/step node for the debugger's call-by-call step graph.
const _SC = { escalate: AMBER, amber: AMBER, struct: '#b7b6ae', green: GREEN, vetoed: '#8f8f86', problem: '#dc2626' }
const _SF = { escalate: '#fbf0db', amber: '#fbf0db', struct: '#fff', green: '#e6f5ef', vetoed: '#efeee8', problem: '#fef2f2' }
function StepNode({ data }) {
  const c = _SC[data.state] || '#b7b6ae', fill = _SF[data.state] || '#fff'
  const lit = data.state === 'escalate'
  return (
    <div style={{ border: `1.5px solid ${c}`, background: fill, borderRadius: 9, padding: '8px 11px', minWidth: 128,
      boxShadow: lit ? `0 0 0 3px ${AMBER}22` : '0 1px 3px rgba(0,0,0,.05)' }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div style={{ fontWeight: 700, fontSize: 13, fontFamily: 'monospace', color: INK }}>{data.label}</div>
      <div style={{ fontSize: 11, color: c, marginTop: 1 }}>{data.sub}</div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  )
}
const nodeTypes = { root: RootNode, agent: AgentNode, ctrl: StepNode }
function nd(id, x, y, data) { return { id, type: 'ctrl', position: { x, y }, data, draggable: false } }
function ed(id, s, t, color) { return { id, source: s, target: t, animated: true, style: { stroke: color, strokeWidth: 2 } } }

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
  const [lab, setLab] = useState(null)             // {uc} when the debugger is open (sandbox)
  const [sessionId, setSessionId] = useState(null) // debug-session metadata popup (#DS-…)
  const [agentDetail, setAgentDetail] = useState(null) // agent metrics popup (id)
  const [hideSummary, setHideSummary] = useState(false)  // dismiss the closing takeaway
  // Ending B is reached deliberately via the inspector's force-route on refunds —
  // no floating controls on the main canvas (that protects the supervise default).

  const stateRef = useRef(null)
  useEffect(() => {
    const es = new EventSource(`${API}/api/stream`)
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data)
      if (ev.state) { setState(ev.state); stateRef.current = ev.state }
      if (ev.narration) setFeed((f) => [{ ...ev.narration, seq: ev.seq, ts: ev.ts }, ...f].slice(0, 120))
    }
    es.onerror = () => {}
    // Open mid-crisis: restart ungoverned so each visit plays the full arc.
    fetch(`${API}/api/reset`, { method: 'POST' }).catch(() => {})
    return () => es.close()
  }, [])

  const act = useCallback((kind, sig) => {
    fetch(`${API}/api/action/${kind}/${encodeURIComponent(sig)}`, { method: 'POST' })
    // arming a route reveals its REAL pre-run eval (when there is a quick eval;
    // account routing has none — its evidence is the replay lab via 'experiment').
    if (kind === 'accept' && sig && sig.startsWith('route_model')) {
      const lv = (stateRef.current?.levers || []).find((l) => l.sig === sig)
      if (lv?.eval_key) setEvalView({ key: lv.eval_key, mode: 'arm' })
    }
  }, [])
  const openProof = useCallback((node) => {
    fetch(`${API}/api/proof/${node || 'requests'}`).then((r) => r.json()).then(setProof).catch(() => {})
  }, [])
  const onFF = useCallback(() => { fetch(`${API}/api/clock/ff?hours=2`, { method: 'POST' }).catch(() => {}) }, [])
  // a chart pin links to its decision: open the session record (same target as the inbox card)
  const onPin = useCallback((p) => { if (p?.session) setSessionId(p.session) }, [])
  // manual control from inside a box — REAL levers, agent stays on watch
  const onForceCache = useCallback((sig) => { act('enable', sig) }, [act])
  const onForceRoute = useCallback((rt) => {
    setInspectTc(null)
    if (rt.eval_key) setEvalView({ key: rt.eval_key, mode: 'trip' })  // quick eval (hold/trip)
    else act('accept', rt.sig)                                        // account: lab is the evidence
  }, [act])

  const openAgent = useCallback((id) => setAgentDetail(id), [])  // metrics popup, not the lab directly
  const { nodes, edges } = useMemo(() => buildGraph(state, act, openAgent), [state, act, openAgent])
  const scene = useMemo(() => sceneFor(state, feed), [state, feed])
  const decision = scene.kind === 'defer' ? scene : null

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: PAPER }}>
      <TopBar state={state} onLab={() => setLab({ uc: state?.agents?.[0]?.id || 'support_copilot' })} onPin={onPin} onFF={onFF} />
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        <MindRail feed={feed} step={state?.step} onOpenSession={setSessionId} />
        {/* stage: the decision docks at the TOP (so it never covers the box it's
            about); the canvas stays fully visible below, focus carried by the
            spotlight + red problem node — never a whole-map dim. */}
        <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          {decision && <div style={{ padding: '12px 18px 0', animation: 'rise .45s ease-out' }}>
            <DeferCard route={decision.route} act={act} state={state} openProof={openProof}
              onExperiment={(uc, evalKey) => {
                // open the at-scale LAB for use cases that have one; otherwise the
                // quick accelerated eval (password holds on the quick check).
                if (LAB_USE_CASES.some((u) => u.key === uc)) setLab({ uc })
                else if (evalKey) setEvalView({ key: evalKey, mode: 'arm' })
                else setLab({ uc })
              }} />
            <style>{`@keyframes rise{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:none}}`}</style>
          </div>}
          <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
            {nodes.length === 0
              ? <div style={{ padding: 24, color: DIM }}>connecting to the live stream…</div>
              : <ReactFlow key={decision ? 'decide' : 'idle'} nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView
                  style={{ width: '100%', height: '100%' }}
                  onNodeClick={(e, node) => node.data.agent && setAgentDetail(node.data.agent.id)}
                  proOptions={{ hideAttribution: true }} nodesDraggable={false}
                  nodesConnectable={false} elementsSelectable={false} panOnDrag={false}
                  zoomOnScroll={false} zoomOnDoubleClick={false}>
                  <Background color="#e7e7e0" gap={22} />
                </ReactFlow>}
            {state?.summary?.ready && !hideSummary &&
              <ClosingCard summary={state.summary} onClose={() => setHideSummary(true)} onOpenSession={setSessionId} />}
          </div>
        </div>
      </div>
      {proof && <ProofPanel proof={proof} onClose={() => setProof(null)} />}
      {inspectTc && <DebuggerPanel tc={inspectTc} onClose={() => setInspectTc(null)}
        onForceCache={onForceCache} onForceRoute={onForceRoute} />}
      {evalView && <EvalPopup view={evalView} onClose={() => setEvalView(null)} />}
      {lab && <ReplayLab initialUc={lab.uc} onClose={() => setLab(null)} />}
      {sessionId && <SessionPopup id={sessionId} onClose={() => setSessionId(null)} />}
      {agentDetail && <AgentPopup agentId={agentDetail} state={state}
        onClose={() => setAgentDetail(null)} act={act}
        onDebug={(id) => { setAgentDetail(null); setLab({ uc: id }) }} />}
    </div>
  )
}

// Agent detail popup — open on a tree click. Useful metrics + a small traffic/cost
// timeline + the option to open the full debugger. (Not the debugger itself.)
function AgentPopup({ agentId, state, onClose, onDebug, act }) {
  const a = (state?.agents || []).find((x) => x.id === agentId)
  if (!a) return null
  const st = _STATUS[a.status] || _STATUS.watching
  const fix = a.fix || {}
  const hist = state?.history || []
  const vshare = a.vshare || 0, cpm = a.cost_per_message || 0
  // per-agent traffic over the (time-compressed) window, from the seeded arrival
  // curve × this agent's volume share; cost = events × its measured $/msg.
  const pts = hist.slice(-48).map((h) => ({ t: h.t, ev: (h.volume || 0) * vshare }))
  const evNow = pts.length ? pts[pts.length - 1].ev : 0
  const evMax = Math.max(1, ...pts.map((p) => p.ev))
  const W = 560, H = 96, PT = 8, PB = 14
  const X = (i) => (i / Math.max(1, pts.length - 1)) * W
  const Y = (v) => PT + (1 - v / evMax) * (H - PT - PB)
  let area = pts.length ? `M 0 ${H - PB}` : ''
  pts.forEach((p, i) => { area += ` L ${X(i)} ${Y(p.ev)}` })
  if (pts.length) area += ` L ${W} ${H - PB} Z`
  const line = pts.map((p, i) => `${i ? 'L' : 'M'} ${X(i)} ${Y(p.ev)}`).join(' ')
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 56 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: PAPER, borderRadius: 16, padding: '22px 26px',
        width: 620, maxHeight: '90vh', overflowY: 'auto', boxShadow: '0 16px 50px rgba(0,0,0,.3)' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
          <div>
            <span style={{ fontSize: 11.5, fontWeight: 800, color: st.color, background: st.bg,
              border: `1px solid ${st.color}33`, borderRadius: 999, padding: '3px 10px' }}>{st.icon} {st.label}</span>
            <div style={{ fontSize: 24, fontWeight: 800, marginTop: 9 }}>{a.label}</div>
            <div style={{ fontSize: 13.5, color: DIM }}>{a.purpose} · {a.model}</div>
          </div>
          <button onClick={onClose} style={{ marginLeft: 'auto', border: 'none', background: 'none',
            fontSize: 26, cursor: 'pointer', color: DIM }}>×</button>
        </div>
        {/* metrics */}
        <div style={{ display: 'flex', gap: 26, marginTop: 16 }}>
          <Stat label="$ / message" main={`$${a.cost_per_message.toFixed(4)}`} sub="measured" color={INK} />
          <Stat label="fleet spend" main={`${Math.round(a.share * 100)}%`} sub="of the bill" color={INK} />
          <Stat label="traffic" main={`${Math.round(a.vshare * 100)}%`} sub="of volume" color={INK} />
          <Stat label={fix.active ? 'saving / mo' : 'fix saves / mo (est.)'}
            main={`$${Math.round(fix.monthly || 0).toLocaleString()}`} sub={`${_FIXNAME[fix.type] || 'fix'}`} color={GREEN} />
        </div>
        {/* traffic + cost timeline */}
        <div style={{ fontSize: 11, color: DIM, letterSpacing: '.06em', marginTop: 18, display: 'flex' }}>
          <span style={{ flex: 1 }}>TRAFFIC · events/min</span>
          <span>{Math.round(evNow).toLocaleString()}/min now · ${(evNow * cpm).toFixed(2)}/min</span>
        </div>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
          style={{ width: '100%', height: H, display: 'block', marginTop: 4,
            border: '1px solid #ece9df', borderRadius: 10, background: '#fcfcfa' }}>
          {area && <path d={area} fill="rgba(181,121,26,.12)" stroke="none" />}
          {line && <path d={line} fill="none" stroke={AMBER} strokeWidth="2" vectorEffect="non-scaling-stroke" />}
        </svg>
        <div style={{ fontSize: 11.5, color: DIM, marginTop: 3 }}>last 12h · time-compressed · cost = events × ${a.cost_per_message.toFixed(4)}/msg</div>
        {/* what's wrong + the fix */}
        <div style={{ marginTop: 16, padding: '12px 14px', borderRadius: 11, background: st.bg, border: `1px solid ${st.color}33` }}>
          <div style={{ fontSize: 13.5, color: a.status === 'governed' ? GREEN : '#85540b', fontWeight: 600 }}>
            {a.status === 'governed' ? '✓' : '⚠'} {a.waste}
          </div>
          <div style={{ fontSize: 13.5, color: '#3b3b37', marginTop: 4 }}>
            <b>{_FIXNAME[fix.type] || 'fix'}</b> — {fix.title} · <span style={{ color: GREEN, fontWeight: 700 }}>~${Math.round(fix.monthly || 0).toLocaleString()}/mo</span>
          </div>
          {fix.escalated && <div style={{ marginTop: 9, display: 'flex', gap: 8 }}>
            <Btn onClick={() => act('accept', fix.sig)} label="arm it" color={AMBER} />
            <Btn onClick={() => act('reject', fix.sig)} label="not now" color={DIM} />
          </div>}
          {fix.active && <div style={{ marginTop: 9 }}><Btn onClick={() => act('veto', fix.sig)} label="revert" color={DIM} /></div>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', marginTop: 18 }}>
          <div style={{ fontSize: 12.5, color: DIM }}>Want to be sure before you commit?</div>
          <div style={{ marginLeft: 'auto' }}>
            <Btn onClick={() => onDebug(a.id)} label="🔬 open debugger →" color={GREEN} primary big />
          </div>
        </div>
      </div>
    </div>
  )
}

// the debug-session record — the agent's memory of an applied decision. A
// lightweight metadata popup (NOT the debugger, NOT a re-run).
function SessionPopup({ id, onClose }) {
  const [s, setS] = useState(null)
  useEffect(() => {
    fetch(`${API}/api/session/${id}`).then((r) => (r.ok ? r.json() : null)).then(setS).catch(() => {})
  }, [id])
  const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`)
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 70 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: '#fff', borderRadius: 14, padding: '20px 22px',
        width: 460, boxShadow: '0 14px 50px rgba(0,0,0,.3)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div><b style={{ fontSize: 16 }}>#{id}</b> <span style={{ color: DIM, fontSize: 12.5 }}>· debug session</span></div>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 22, cursor: 'pointer', color: DIM }}>×</button>
        </div>
        {!s ? <div style={{ color: DIM, padding: 14 }}>loading…</div> : <>
          <div style={{ fontSize: 14.5, fontWeight: 700, marginTop: 10 }}>{s.use_case}</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px 16px', marginTop: 12 }}>
            <Stat2 k="levers applied" v={(s.levers || []).join(', ') || 'none'} />
            <Stat2 k="source" v={`${s.source} · N=${s.n ?? '—'}`} />
            <Stat2 k="saved vs baseline" v={pct(s.saved_pct)} color={GREEN} />
            <Stat2 k="projected (est.)" v={s.projected_monthly != null ? `~$${Number(s.projected_monthly).toLocaleString()}/mo` : '—'} color="#85540b" />
            <Stat2 k="quality held" v={pct(s.held_pct)} color={GREEN} />
            <Stat2 k="degraded" v={pct(s.degraded_pct)} color={AMBER} />
          </div>
          {s.advice_against && <div style={{ fontSize: 12.5, color: '#85540b', background: '#faeeda',
            border: `1px solid ${AMBER}`, borderRadius: 9, padding: '9px 11px', marginTop: 12 }}>
            ⚑ I advised against the economy lever; applied at your direction — <b>watching live</b>, I'll flag if quality slips.
          </div>}
          {!s.advice_against && <div style={{ fontSize: 12.5, color: '#1a4f40', marginTop: 12 }}>● status: watching live.</div>}
          <div style={{ marginTop: 14, borderTop: '1px solid #eceae0', paddingTop: 10 }}>
            <a href={phoenixTestUrl(s.project_gid)} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: GREEN, fontWeight: 600 }}>this session's 'test' traces in Phoenix ↗</a>
          </div>
        </>}
      </div>
    </div>
  )
}
function Stat2({ k, v, color }) {
  return (
    <div>
      <div style={{ fontSize: 10.5, color: DIM, letterSpacing: '.04em', textTransform: 'uppercase' }}>{k}</div>
      <div style={{ fontSize: 14, fontWeight: 600, color: color || INK }}>{v}</div>
    </div>
  )
}

// ---- scene derivation: ONLY a decision takes the screen --------------------
// The agent narrates every ambient beat (observe/diagnose/act/verify/hold) in the
// inbox — non-blocking, its own channel. The screen is only stolen for a DECISION
// the user must make (the deferred risky lever). So the cockpit is interactive
// from the first frame; no startup cutscene of focal cards dimming the map.
function sceneFor(state, feed) {
  if (!state) return { kind: 'idle' }
  // any escalated RISKY fix (suppress or route) the agent has deferred to you
  const route = (state.levers || []).find((l) => !l.safe && l.escalated && !l.active && !l.vetoed)
  if (route) return { kind: 'defer', route }
  return { kind: 'idle' }
}

// ---- the focal layer: one card at a time, centred, over the dimmed map -----
function FocalLayer({ scene, state, act, openProof, onExperiment }) {
  if (!scene || scene.kind === 'idle') return null
  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
      justifyContent: 'center', pointerEvents: 'none', padding: 28 }}>
      <div key={scene.kind} style={{ pointerEvents: 'auto', width: scene.kind === 'defer' ? 600 : 600,
        animation: 'rise .45s ease-out' }}>
        {scene.kind === 'diagnose' && <DiagnoseCard text={scene.text} state={state} openProof={openProof} />}
        {scene.kind === 'act' && <ActCard text={scene.text} openProof={openProof} />}
        {scene.kind === 'verify' && <VerifyCard text={scene.text} state={state} />}
        {scene.kind === 'defer' && <DeferCard route={scene.route} act={act} state={state}
          onExperiment={onExperiment} openProof={openProof} />}
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

// The defer card names the SPECIFIC agent the Accountant stopped on, and frames
// the ask by the fix type + (for routing) its real pre-run verdict.
function DeferCard({ route, act, state, onExperiment, openProof }) {
  const agents = state?.agents || []
  const uc = route.tc || route.agent
  const ag = agents.find((a) => a.id === uc)
  const label = ag?.label || 'this agent'
  const vol = state && ag ? Math.round(state.volume * (ag.share || 0)) : null
  const saving = route.monthly
  const isRoute = route.type === 'route_model'
  const verb = isRoute ? 'routing it to the economy model' : 'suppressing that call'
  const line = isRoute
    ? "the replay shows economy degrading here — I'd keep premium, but it's your call."
    : "the KB likely covers it, but dropping it could still change a quote — your call."
  return (
    <Card accent={RED} kicker={`⚑ Decide · ${label} — your call`} glow>
      <div style={{ fontSize: 23, lineHeight: 1.36, color: INK, marginTop: 8 }}>
        On <b>{label}</b>, {verb} could <b style={{ color: RED }}>change the answers</b> — {line}
      </div>
      {/* evidence line — grounds the ask */}
      <div style={{ fontSize: 13.5, color: '#3b3b37', marginTop: 14, paddingTop: 12, borderTop: '1px solid #f0eee6',
        display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
        <span style={{ color: DIM }}>~{vol != null ? vol.toLocaleString() : '—'} msgs/mo on this agent</span>
        <span style={{ color: GREEN, fontWeight: 700 }}>~${saving != null ? Math.round(saving).toLocaleString() : '—'}/mo if it holds <span style={{ color: DIM, fontSize: 11, fontWeight: 400 }}>(est.)</span></span>
        <Btn onClick={() => openProof(uc)} label="sample traces ↗" color={DIM} />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 16 }}>
        <Btn onClick={() => act('reject', route.sig)} label="not now" color={DIM} />
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 10, alignItems: 'center' }}>
          <Btn onClick={() => act('accept', route.sig)} label="arm it anyway" color={AMBER} />
          <Btn onClick={() => onExperiment(uc, route.eval_key)} label="🔬 debug it →" color={GREEN} primary big />
        </div>
      </div>
      <div style={{ fontSize: 12, color: DIM, marginTop: 9, textAlign: 'right' }}>
        not sure? <b>debug it</b> — step a real conversation and load-test before you commit.
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

// The top bar is the VALUE SPINE: a real time-series of what governance moved.
// $/message is a measured descending staircase (solid), each step pinned to the
// decision that caused it; quality is eval-measured (dashed), flat but for the one
// dip under the trap pin; volume is the seeded traffic behind. The clock is the
// only artifice, disclosed.
function TopBar({ state, onLab, onPin, onFF }) {
  const dpm = useTween(state?.dollars_per_message)
  const start = state?.summary?.start_dpm ?? state?.baseline_dollars_per_message
  const down = start && state && state.dollars_per_message < start - 1e-9
  const pct = down ? Math.round((1 - state.dollars_per_message / start) * 100) : 0
  const projSaved = state ? Math.max(0, (start - dpm) * state.volume) : 0  // animates as fixes land
  const clock = state?.clock
  return (
    <div style={{ borderBottom: '1px solid #eceae0', background: '#fff' }}>
      <div style={{ padding: '12px 24px 6px', display: 'flex', alignItems: 'baseline', gap: 16 }}>
        <div style={{ fontSize: 21, fontWeight: 800, letterSpacing: '-.01em', color: INK }}>
          AI cost governance
          <span style={{ fontSize: 15.5, fontWeight: 500, color: DIM }}> · one Accountant governing a fleet</span>
        </div>
        <div style={{ marginLeft: 'auto' }}><MindLoop step={state?.step} steps={state?.steps} /></div>
      </div>
      <div style={{ padding: '2px 24px 14px', display: 'flex', gap: 26, alignItems: 'stretch' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 200, justifyContent: 'center' }}>
          <BigReadout label="$ / message now" main={`$${(dpm || 0).toFixed(4)}`}
            sub={down ? `▼ ${pct}% vs start` : 'baseline'} color={down ? GREEN : INK}
            subColor={down ? GREEN : DIM} big />
          <BigReadout label="saved / mo (est.)" main={`$${Math.round(projSaved).toLocaleString()}`}
            sub={`$${state ? state.realized_savings.toFixed(2) : '—'} measured live`}
            color={GREEN} subColor={DIM} big />
        </div>
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 4 }}>
            <Legend />
            <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontSize: 12.5, color: DIM }}>
                {clock ? `${clock.disclosure} · ${clock.label}` : 'time-compressed'}
              </span>
              <button onClick={onFF} title="advance the clock ~2h"
                style={{ fontSize: 12.5, fontWeight: 700, cursor: 'pointer', border: '1px solid #d8d6cc',
                  borderRadius: 8, padding: '5px 11px', color: '#5a5852', background: '#fff' }}>⏩ +2h</button>
              <button onClick={onLab}
                style={{ fontSize: 13, fontWeight: 700, cursor: 'pointer', border: `1px solid ${GREEN}`,
                  borderRadius: 8, padding: '6px 14px', color: '#fff', background: GREEN }}>🔬 debugger</button>
            </div>
          </div>
          <Spine state={state} onPin={onPin} />
        </div>
      </div>
    </div>
  )
}

// The closing takeaway — the demo's last 10 seconds, read straight off the series.
function ClosingCard({ summary, onClose, onOpenSession }) {
  if (!summary) return null
  const { start_dpm, now_dpm, pct_down, quality_note, decisions } = summary
  return (
    <div style={{ position: 'absolute', left: 18, right: 18, bottom: 16, zIndex: 6, background: '#fff',
      border: `2px solid ${GREEN}`, borderRadius: 18, padding: '18px 22px',
      boxShadow: '0 16px 50px rgba(0,0,0,.18)', animation: 'rise .5s ease-out' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{ fontSize: 11.5, fontWeight: 800, letterSpacing: '.1em', textTransform: 'uppercase', color: GREEN }}>
          ✦ the run, end to end
        </div>
        <button onClick={onClose} style={{ marginLeft: 'auto', fontSize: 12.5, color: DIM, cursor: 'pointer',
          border: '1px solid #e6e4da', borderRadius: 8, padding: '5px 11px', background: '#fff' }}>dismiss</button>
      </div>
      <div style={{ fontSize: 23, lineHeight: 1.42, color: INK, marginTop: 9, fontWeight: 500 }}>
        Started at <b>${(start_dpm || 0).toFixed(4)}</b>/msg, now <b style={{ color: GREEN }}>${(now_dpm || 0).toFixed(4)}</b>
        {' '}— <b style={{ color: GREEN }}>down {pct_down}%</b>. Quality {quality_note}. Every step proven safe and <b>reversible</b>.
      </div>
      {decisions && decisions.length > 0 && (
        <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ fontSize: 12.5, color: DIM }}>your decisions:</span>
          {decisions.map((d, i) => (
            <DsLink key={i} id={d.session} onOpenSession={onOpenSession} />
          ))}
        </div>
      )}
    </div>
  )
}

function BigReadout({ label, main, sub, color, subColor, big }) {
  return (
    <div>
      <div style={{ fontSize: 11.5, color: DIM, letterSpacing: '.03em', textTransform: 'uppercase', fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: big ? 34 : 26, fontWeight: 800, lineHeight: 1.0, color, letterSpacing: '-.02em' }}>{main}</div>
      <div style={{ fontSize: 13, fontWeight: 700, color: subColor }}>{sub}</div>
    </div>
  )
}

function Legend() {
  const Item = ({ children, dash, sq }) => (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12.5, color: '#54524c', fontWeight: 600 }}>
      {sq ? <span style={{ width: 11, height: 11, background: 'rgba(181,121,26,.32)', borderRadius: 2 }} />
        : <svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke={dash ? DIM : GREEN}
            strokeWidth="2.5" strokeDasharray={dash ? '4 3' : '0'} /></svg>}
      {children}
    </span>
  )
  return (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
      <Item>$/message</Item><Item dash>quality</Item><Item sq>volume</Item>
    </div>
  )
}

// --- the value-spine chart (hand-rolled SVG, no chart dep) -----------------
const _W = 1000, _H = 150, _PADL = 0.03, _PADR = 0.012, _PADT = 12, _PADB = 16
const _xpct = (t) => (_PADL + t * (1 - _PADL - _PADR)) * 100
function _pinColor(k) { return k === 'reverted' ? RED : GREEN }
function _pinBg(k) { return k === 'reverted' ? '#fbeeec' : '#eaf6f0' }
function _pinIcon(k) { return k === 'reverted' ? '⚠' : k === 'you_decided' ? '◆' : '🤖' }
function _pinText(p) {
  if (p.kind === 'reverted') return 'reverted'
  if (p.kind === 'you_decided') return 'you applied'
  const l = (p.label || '').toLowerCase()
  if (l.includes('refund')) return 'cached refunds'
  if (l.includes('account')) return 'cached account'
  return 'agent cached'
}

function Spine({ state, onPin }) {
  const history = state?.history || []
  const pins = state?.pins || []
  if (history.length < 2) {
    return <div style={{ height: _H, display: 'flex', alignItems: 'center', justifyContent: 'center',
      color: DIM, fontSize: 14, border: '1px dashed #e6e4da', borderRadius: 10 }}>
      watching the live traffic — the value line builds as the agent works…</div>
  }
  const start = state?.summary?.start_dpm ?? history[0].dollars_per_message
  const ymax = Math.max(start * 1.08, ...history.map((h) => h.dollars_per_message)) || 1
  const vmax = Math.max(1, ...history.map((h) => h.volume || 0))
  const X = (t) => (_PADL + (t || 0) * (1 - _PADL - _PADR)) * _W
  const plotH = _H - _PADT - _PADB
  const costY = (v) => _PADT + (1 - v / ymax) * plotH
  const qY = (q) => _PADT + (1 - (q == null ? 1 : q)) * (plotH * 0.6)  // quality rides the upper band
  const volY = (v) => _H - _PADB - (v / vmax) * (plotH * 0.42)

  // cost: a descending STAIRCASE (hold, then step down at each lever change)
  let cost = ''
  history.forEach((h, i) => {
    const x = X(h.t), y = costY(h.dollars_per_message)
    if (i === 0) cost = `M ${x} ${y}`
    else cost += ` L ${x} ${costY(history[i - 1].dollars_per_message)} L ${x} ${y}`
  })
  // quality: dashed line, flat but for the trap dip
  const qual = history.map((h, i) => `${i ? 'L' : 'M'} ${X(h.t)} ${qY(h.quality)}`).join(' ')
  // volume: faint area behind
  let vol = `M ${X(history[0].t)} ${_H - _PADB}`
  history.forEach((h) => { vol += ` L ${X(h.t)} ${volY(h.volume || 0)}` })
  vol += ` L ${X(history[history.length - 1].t)} ${_H - _PADB} Z`
  const nowX = X(history[history.length - 1].t)

  return (
    <div>
      <div style={{ position: 'relative', height: _H }}>
        <svg viewBox={`0 0 ${_W} ${_H}`} preserveAspectRatio="none"
          style={{ width: '100%', height: _H, display: 'block', overflow: 'visible' }}>
          <path d={vol} fill="rgba(181,121,26,.13)" stroke="none" />
          <line x1={X(0)} y1={costY(start)} x2={_W} y2={costY(start)} stroke="#d8d6cc"
            strokeWidth="1" strokeDasharray="2 4" vectorEffect="non-scaling-stroke" />
          <path d={qual} fill="none" stroke={DIM} strokeWidth="2" strokeDasharray="5 4"
            vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
          <path d={cost} fill="none" stroke={GREEN} strokeWidth="2.75"
            vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
          <circle cx={nowX} cy={costY(history[history.length - 1].dollars_per_message)} r="3.5" fill={GREEN} />
        </svg>
        {pins.map((p, i) => (
          <div key={i} style={{ position: 'absolute', left: `${_xpct(p.t)}%`, top: 0, bottom: 0, width: 0,
            borderLeft: `1.5px dashed ${_pinColor(p.kind)}55` }} />
        ))}
      </div>
      <div style={{ position: 'relative', height: 26, marginTop: 3 }}>
        {pins.map((p, i) => {
          const c = _pinColor(p.kind)
          return (
            <button key={i} onClick={() => onPin && onPin(p)}
              title={`${p.label_time || ''} — ${p.label || ''}${p.trigger ? ' · ' + p.trigger : ''}`}
              style={{ position: 'absolute', left: `${_xpct(p.t)}%`, transform: 'translateX(-50%)',
                whiteSpace: 'nowrap', fontSize: 11.5, fontWeight: 700, color: c, background: _pinBg(p.kind),
                border: `1px solid ${c}44`, borderRadius: 999, padding: '3px 9px',
                cursor: p.session ? 'pointer' : 'default', display: 'inline-flex', gap: 5, alignItems: 'center' }}>
              <span style={{ fontSize: 10 }}>{_pinIcon(p.kind)}</span>{_pinText(p)}
              {p.session ? <span style={{ opacity: .6 }}>›</span> : null}
            </button>
          )
        })}
      </div>
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
// The inbox is a TYPED activity timeline — not a wall of text. Each event carries
// a kind (icon-chip + colour), a one-line summary, the TRIGGER that set it off,
// a timestamp, and a reopen-session link when a debug session backs it.
function railStyle(kind) {
  switch (kind) {
    case 'acted': return { accent: GREEN, bg: '#eaf6f0', icon: '🤖', tag: 'acted alone' }
    case 'decided': return { accent: GREEN, bg: '#eaf6f0', icon: '◆', tag: 'you decided' }
    case 'applied': return { accent: GREEN, bg: '#eaf6f0', icon: '✓', tag: 'applied' }
    case 'escalate': return { accent: AMBER, bg: '#fbf0db', icon: '⚑', tag: 'deferred to you' }
    case 'reverted': return { accent: RED, bg: '#fbeeec', icon: '⚠', tag: 'reverted' }
    case 'verified': return { accent: GREEN, bg: '#eef7f1', icon: '✦', tag: 'verified' }
    case 'holding': return { accent: DIM, bg: '#f6f6f1', icon: '○', tag: 'watching' }
    case 'user': return { accent: '#3b3b37', bg: '#efece2', icon: '◐', tag: 'you' }
    case 'reaction': return { accent: GREEN, bg: '#f3faf6', icon: '·', tag: '' }
    default: return { accent: GREEN, bg: '#f3faf6', icon: '◇', tag: 'reasoning' }  // thinking / reasoned
  }
}
function relTime(ts) {
  if (!ts) return ''
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (s < 5) return 'now'
  if (s < 60) return `${s}s ago`
  return `${Math.round(s / 60)}m ago`
}
function DsLink({ id, onOpenSession, label }) {
  if (!id) return null
  return <button onClick={() => onOpenSession && onOpenSession(id)}
    style={{ marginTop: 4, fontSize: 12.5, fontWeight: 700, color: GREEN, background: 'none',
      border: 'none', cursor: 'pointer', padding: 0, display: 'block' }}>
    {label ? `${label} ` : ''}#{id} ›</button>
}

function TimelineCard({ c, onOpenSession }) {
  const s = railStyle(c.kind)
  return (
    <div style={{ display: 'flex', gap: 11, padding: '12px 0', borderTop: '1px solid #f1efe7' }}>
      <div style={{ flexShrink: 0, width: 30, height: 30, borderRadius: 9, background: s.bg,
        border: `1px solid ${s.accent}33`, display: 'flex', alignItems: 'center',
        justifyContent: 'center', fontSize: 14, color: s.accent }}>{s.icon}</div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
          {s.tag && <span style={{ fontSize: 10.5, fontWeight: 800, letterSpacing: '.06em',
            textTransform: 'uppercase', color: s.accent }}>{s.tag}</span>}
          <span style={{ marginLeft: 'auto', fontSize: 11.5, color: DIM }}>{relTime(c.ts)}</span>
        </div>
        <div style={{ fontSize: 14.5, color: '#2e2e2b', lineHeight: 1.4, marginTop: 2 }}>
          {c.kind === 'user' ? <b>You: </b> : null}{c.text}
        </div>
        {c.trigger && <div style={{ fontSize: 12.5, color: DIM, marginTop: 3 }}>↳ {c.trigger}</div>}
        <DsLink id={c.session} onOpenSession={onOpenSession} label="reopen session" />
      </div>
    </div>
  )
}

function MindRail({ feed, step, onOpenSession }) {
  const [q, setQ] = useState('')
  const now = feed[0]
  const rest = feed.slice(1)
  const shown = q ? rest.filter((c) => (c.text || '').toLowerCase().includes(q.toLowerCase())) : rest
  const s = now ? railStyle(now.kind) : null
  const applied = feed.filter((c) => c.kind === 'acted' || c.kind === 'decided' || c.kind === 'applied').length
  const reverted = feed.filter((c) => c.kind === 'reverted').length
  return (
    <div style={{ width: 396, borderRight: '1px solid #eceae0', background: '#fff',
      display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ padding: '16px 18px 8px' }}>
        <div style={{ fontWeight: 800, fontSize: 20, letterSpacing: '-.01em' }}>The agent's mind</div>
        <div style={{ fontSize: 12.5, color: DIM, marginTop: 2 }}>
          today · {feed.length} events · {applied} applied · {reverted} reverted ·
          <span style={{ color: GREEN, fontWeight: 700 }}> watching live</span>
        </div>
      </div>
      {/* NOW — the current thought, the hero */}
      <div style={{ padding: '4px 18px 8px' }}>
        {now ? (
          <div style={{ border: `2px solid ${s.accent}`, background: s.bg, borderRadius: 14, padding: '14px 16px' }}>
            <div style={{ fontSize: 10.5, letterSpacing: '.08em', textTransform: 'uppercase', fontWeight: 800,
              color: s.accent, display: 'flex', alignItems: 'center', gap: 7 }}>
              <span style={{ fontSize: 13 }}>{s.icon}</span>
              reasoning live · {(step || 'observe').toLowerCase()}
            </div>
            <div style={{ fontSize: 18.5, lineHeight: 1.4, color: INK, marginTop: 7, fontWeight: 500 }}>
              {now.kind === 'user' ? <b>You: </b> : null}{now.text}
            </div>
            {now.trigger && <div style={{ fontSize: 13, color: '#6b6962', marginTop: 6 }}>↳ {now.trigger}</div>}
            <DsLink id={now.session} onOpenSession={onOpenSession} label="reopen session" />
          </div>
        ) : <div style={{ color: DIM, fontSize: 17, padding: '10px 0' }}>Reading the live traffic…</div>}
      </div>
      {/* the timeline rail */}
      <div style={{ overflowY: 'auto', flex: 1, minHeight: 0, padding: '0 18px 8px' }}>
        {shown.map((c) => <TimelineCard key={c.seq} c={c} onOpenSession={onOpenSession} />)}
        {!shown.length && <div style={{ color: DIM, fontSize: 13, padding: '14px 0' }}>
          {q ? 'nothing matches.' : 'the timeline fills as the agent works…'}</div>}
      </div>
      {/* search — a quiet line at the bottom */}
      <div style={{ padding: '8px 18px 14px', borderTop: '1px solid #f1efe7' }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="🔍  search the agent's history…"
          style={{ width: '100%', boxSizing: 'border-box', padding: '8px 12px', border: '1px solid #e6e4da',
            borderRadius: 9, fontSize: 13, color: '#54524c', background: '#fcfcfa' }} />
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
                  {r.phoenix_url && <SpanLink url={r.phoenix_url} label="trace" />}
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
      <div onClick={(e) => e.stopPropagation()} style={{ background: PAPER, borderRadius: 16, padding: '24px 28px',
        width: 700, maxHeight: '90vh', overflowY: 'auto', boxShadow: '0 14px 50px rgba(0,0,0,.3)' }}>
        {!d ? <div style={{ color: DIM, padding: 16 }}>Dropping into the box…</div> : <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <span style={{ fontSize: 10.5, fontWeight: 800, color: '#fff', background: '#5a4815',
                borderRadius: 9, padding: '3px 10px', letterSpacing: '.06em' }}>INSPECT</span>
              <div style={{ fontSize: 23, fontWeight: 800, marginTop: 10 }}>{d.title}</div>
              {d.purpose && <div style={{ fontSize: 13.5, color: DIM, marginTop: 1 }}>{d.purpose}</div>}
              <div style={{ fontSize: 13.5, color: '#3b3b37', marginTop: 3 }}>
                {Math.round(d.share * 100)}% of fleet spend · <b>${d.cost_per_message.toFixed(4)}</b> / message
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
              <span style={{ width: 184, textAlign: 'right', fontSize: 12, color: DIM,
                display: 'inline-flex', alignItems: 'center', gap: 7, justifyContent: 'flex-end' }}>
                measured {d.llm_url && <SpanLink url={d.llm_url} label="Phoenix" />}
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

          <div style={{ fontSize: 11, color: DIM, letterSpacing: '.06em', marginTop: 18 }}>TAKE MANUAL CONTROL</div>
          <div style={{ display: 'flex', gap: 10, marginTop: 9 }}>
            {/* the agent's ONE fix — safe (force-on) or risky (eval-gated) */}
            {d.cache && (d.cache.active
              ? <div style={{ flex: 1, textAlign: 'center', fontSize: 14, fontWeight: 700, color: GREEN, border: `1.5px solid ${GREEN}`,
                  borderRadius: 9, padding: '11px 14px', background: '#eef7f1' }}>✓ {d.cache.label} — active</div>
              : <button onClick={() => onForceCache(d.cache.sig)} style={mbtn(GREEN)}>{d.cache.label}</button>)}
            {d.route && (d.route.active
              ? <div style={{ flex: 1, textAlign: 'center', fontSize: 14, fontWeight: 700, color: AMBER, border: `1.5px solid ${AMBER}`,
                  borderRadius: 9, padding: '11px 14px', background: '#fbf0db' }}>✓ {d.route.label} — live</div>
              : <button onClick={() => onForceRoute(d.route)} style={mbtn(d.route.eval_key ? RED : AMBER)}>
                  {d.route.label}{d.route.eval_key ? ' — see the eval →' : ' →'}</button>)}
          </div>
          <div style={{ fontSize: 13, color: '#3b3b37', marginTop: 12 }}>
            The agent keeps watching — it re-measures from the traffic and flags you if quality drops.
          </div>
        </>}
      </div>
    </div>
  )
}
function mbtn(color) {
  return { flex: 1, fontSize: 14.5, fontWeight: 700, cursor: 'pointer', color, background: '#fff',
    border: `1.5px solid ${color}`, borderRadius: 9, padding: '11px 14px' }
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
  { key: 'support_copilot', label: 'Support Co-Pilot' },
  { key: 'refund_auditor', label: 'Refund Auditor' },
  { key: 'sales_assistant', label: 'Sales Assistant' },
  { key: 'docs_bot', label: 'Docs Bot' },
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

function ReplayLab({ onClose, initialUc }) {
  const [uc, setUc] = useState(initialUc || 'account_question')
  const [data, setData] = useState(null)
  const [config, setConfig] = useState({ cache: true, economy: false })  // candidate; toggles instant
  const [selConv, setSelConv] = useState(0)
  const [cursorCall, setCursorCall] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [bp, setBp] = useState('none')
  const [source, setSource] = useState('replay')   // replay | synthetic
  const [N, setN] = useState(6)                     // adjustable run size
  const [ran, setRan] = useState(false)            // a load test has been run
  const [running, setRunning] = useState(false)
  const [runRows, setRunRows] = useState([])        // rows from the live run
  const [runSource, setRunSource] = useState('replay')
  const [trickle, setTrickle] = useState(null)
  const [confirm, setConfirm] = useState(false)    // close-to-apply
  const esRef = useRef(null)

  const [loadErr, setLoadErr] = useState(false)
  useEffect(() => {
    setData(null); setLoadErr(false); setRan(false); setRunRows([]); esRef.current?.close()
    fetch(`${API}/api/lab/${uc}`).then((r) => (r.ok ? r.json() : Promise.reject(new Error('no pre-run'))))
      .then(setData).catch(() => setLoadErr(true))
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
    const e = imp || {}
    const q = new URLSearchParams({
      use_case: uc, cache: config.cache, economy: config.economy,
      held_pct: e.heldPct ?? '', degraded_pct: e.degradedPct ?? '', saved_pct: e.savedPct ?? '',
      projected_monthly: proj != null ? Math.round(proj) : '',
      source: ran ? runSource : source, n: rows.length || data?.n || '',
    }).toString()
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

        {!data ? (loadErr
          ? <div style={{ color: DIM, padding: 24, fontSize: 14 }}>
              No pre-run batch for <b>{uc.replace('_', ' ')}</b>. Switch the source to <b>synthetic</b> and Run to generate one live.</div>
          : <div style={{ color: DIM, padding: 24 }}>Loading the pre-run batch…</div>) : (
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
                <CallInspector call={call} row={selRow}
                  corpus={{ tools: data?.corpus_tool_spans, trace: data?.corpus_trace_span }}
                  live={runRows.length > 0} />
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
                    {(source === 'synthetic' ? [6, 12, 24, 48] : [6, 12, 18]).map((v) => <option key={v} value={v}>{v}</option>)}
                  </select>
                  <select value={source} onChange={(e) => { setSource(e.target.value); setN(6) }} disabled={running}
                    style={{ fontSize: 12.5, padding: '4px 7px', borderRadius: 7, border: `1px solid ${source === 'synthetic' ? AMBER : '#d8d6cc'}`, color: source === 'synthetic' ? AMBER : INK }}>
                    <option value="replay">replay real</option>
                    <option value="synthetic">synthetic</option>
                  </select>
                </div>
                <div style={{ fontSize: 11, color: source === 'synthetic' ? AMBER : DIM, marginTop: 6 }}>
                  {source === 'synthetic'
                    ? 'synthetic — fresh generated cases, any volume, beyond your history (exploration, not real-traffic proof)'
                    : 'replay — your real past conversations (evidence on actual traffic)'}
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
                        {r.phoenix_url && <SpanLink url={redirectUrl({}, r, { tools: data?.corpus_tool_spans, trace: data?.corpus_trace_span }, runRows.length > 0)} label="trace ↗" />}
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

        {/* CLOSE-TO-APPLY — the agent narrates the change it's about to make */}
        {confirm && data && (() => {
          const label = LAB_USE_CASES.find((u) => u.key === uc)?.label
          const lab = (label || uc).toLowerCase()
          const both = config.cache && config.economy
          const nRun = rows.length || data.n
          return <div style={{ position: 'absolute', inset: 0, background: 'rgba(255,255,255,.9)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: 14 }}>
            <div style={{ width: 500, background: '#fff', border: `1px solid ${config.economy ? AMBER : GREEN}`, borderRadius: 14, padding: 22, boxShadow: '0 12px 44px rgba(0,0,0,.2)' }}>
              <div style={{ fontSize: 11, letterSpacing: '.08em', fontWeight: 800, color: config.economy ? AMBER : GREEN }}>THE AGENT</div>
              <div style={{ fontSize: 15, color: INK, lineHeight: 1.5, marginTop: 8 }}>
                I'll apply {both ? 'two changes' : 'one change'} to production for {lab}:
                {config.cache && <> caching repeated lookups <span style={{ color: GREEN }}>(safe, output-preserving)</span></>}
                {both && ', and'}
                {config.economy && <> routing this class to the <span style={{ color: AMBER }}>economy model</span></>}.
                {config.economy && imp && ` Economy held ${Math.round(imp.heldPct * 100)}% across your ${nRun} replays — I'd keep premium, but I'll apply at your direction and watch live.`}
                {' '}Confirm and I'll enact, log the session, and flag you the moment quality slips.
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 20 }}>
                <button onClick={onClose} style={{ fontSize: 13, cursor: 'pointer', border: '1px solid #d8d6cc', background: '#fff', borderRadius: 8, padding: '7px 14px', color: '#5a5852' }}>not now — close</button>
                <button onClick={apply} style={{ fontSize: 13, fontWeight: 700, cursor: 'pointer', border: 'none', background: GREEN, color: '#fff', borderRadius: 8, padding: '7px 16px' }}>confirm — enact &amp; log</button>
              </div>
            </div>
          </div>
        })()}
      </div>
    </div>
  )
}
// the shared inspector — renders whatever the cursor points at; the model call
// is the payoff (premium vs economy side by side).
function CallInspector({ call, row, corpus, live }) {
  if (!call) return <div style={{ color: DIM, fontSize: 13 }}>Step or click a box to inspect a call.</div>
  // a live-run conversation links to its exact span; a pre-run row to a durable
  // corpus span of the same tool (its own bulk-exported span was dropped).
  const span = redirectUrl(call, row, corpus, live)
  const spanLabel = (live && call.span_id) ? 'open this span in Phoenix ↗'
    : call.tool ? `open a real ${call.tool} span in Phoenix ↗` : 'open in Phoenix ↗'
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
// A conversation YOU run live in the debugger is small + paced, so its spans land
// in Phoenix — point straight at YOUR exact span (/redirects/spans/<otel-id>). The
// PRE-RUN rows came from a bulk export Phoenix Cloud dropped, so for those we fall
// back to a real DURABLE corpus span of the same agent+tool (a genuine web_search
// you can open), then the project spans view.
function redirectUrl(call, row, corpus, live) {
  const base = (row?.phoenix_url || '').split('/projects/')[0]   // …/s/tomas
  if (!base) return null
  if (live && call?.span_id) return `${base}/redirects/spans/${call.span_id}`  // your exact span
  const tools = corpus?.tools || {}
  if (call?.tool && tools[call.tool]) return `${base}/redirects/spans/${tools[call.tool]}`
  if (corpus?.trace) return `${base}/redirects/spans/${corpus.trace}`
  const m = (row?.phoenix_url || '').match(/^(.*\/projects\/[^/]+)\//)
  return m ? `${m[1]}/spans` : null
}
// Phoenix links are EVIDENCE — obvious but secondary. One consistent treatment
// everywhere they appear: a quiet green pill you never have to hunt for.
function SpanLink({ url, label }) {
  return (
    <a href={url} target="_blank" rel="noreferrer"
      style={{ fontSize: 12.5, color: GREEN, fontWeight: 700, whiteSpace: 'nowrap',
        textDecoration: 'none', background: '#eef7f1', border: `1px solid ${GREEN}33`,
        borderRadius: 999, padding: '3px 10px', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      {label || 'open in Phoenix'} <span style={{ fontSize: 11 }}>↗</span></a>
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
// The FLEET TREE: the Accountant on top, each observed agent a node below it.
function buildGraph(state, act, onInspect) {
  const agents = state?.agents || []
  if (!agents.length) return { nodes: [], edges: [] }
  const nodes = [], edges = []
  const colW = 280, cardW = 246, startX = 30
  const rootX = startX + ((agents.length - 1) * colW) / 2 + (cardW - 230) / 2
  nodes.push({ id: 'root', type: 'root', position: { x: rootX, y: 0 }, draggable: false,
    data: { sub: `governing ${agents.length} agents · live` } })
  agents.forEach((a, i) => {
    const aid = 'agent_' + a.id
    nodes.push({ id: aid, type: 'agent', position: { x: startX + i * colW, y: 188 }, draggable: false,
      data: { agent: a, act, onInspect } })
    const col = a.status === 'governed' ? GREEN : a.status === 'your_call' ? RED : '#c8c7c0'
    edges.push({ id: 'e_' + aid, source: 'root', target: aid, animated: a.status !== 'watching',
      style: { stroke: col, strokeWidth: a.status === 'your_call' ? 2.5 : 2 } })
  })
  return { nodes, edges }
}
