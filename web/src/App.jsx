import React, { useEffect, useState, useRef, useCallback, useMemo } from 'react'
import ReactFlow, { Background, Handle, Position } from 'reactflow'
import 'reactflow/dist/style.css'

const API = 'http://localhost:8800'
const GREEN = '#0f6e56', AMBER = '#b5791a', DIM = '#9ca3af', INK = '#141413', PAPER = '#fbfbf9'

// ---- a control-point node: state + the inline affordances to act on it ----
function Btn({ onClick, label, color, primary }) {
  return <button className="nodrag nopan" onClick={(e) => { e.stopPropagation(); onClick() }}
    style={{ fontSize: 11.5, borderRadius: 6, padding: '3px 10px', cursor: 'pointer',
      border: `1px solid ${color}`, color: primary ? '#fff' : color,
      background: primary ? color : '#fff' }}>{label}</button>
}

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
            <Btn onClick={() => data.act('accept', data.sig)} label="accept" color={AMBER} primary />
            <Btn onClick={() => data.act('reject', data.sig)} label="reject" color={AMBER} />
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

export default function App() {
  const [state, setState] = useState(null)
  const [feed, setFeed] = useState([])
  const [q, setQ] = useState('')
  const [proof, setProof] = useState(null)
  const [evalView, setEvalView] = useState(null)  // the consequence popup on ARM

  useEffect(() => {
    const es = new EventSource(`${API}/api/stream`)
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data)
      if (ev.state) setState(ev.state)
      if (ev.narration) setFeed((f) => [{ ...ev.narration, seq: ev.seq }, ...f].slice(0, 120))
    }
    es.onerror = () => {}  // EventSource auto-reconnects
    // Restart the demo from ungoverned on load, so each visit watches the full
    // hands-off arc (agent reasons → auto-applies safe levers → escalates risky).
    fetch(`${API}/api/reset`, { method: 'POST' }).catch(() => {})
    return () => es.close()
  }, [])

  const act = useCallback((kind, sig) => {
    fetch(`${API}/api/action/${kind}/${encodeURIComponent(sig)}`, { method: 'POST' })
    // ARM: deciding the deferred model-routing lever takes it live immediately
    // AND opens the consequence popup that instruments your choice — the
    // accelerated quality eval (HOLD case for the simple-routing decision).
    if (kind === 'accept' && sig && sig.startsWith('route_model')) setEvalView({ key: 'hold' })
  }, [])
  const openProof = useCallback((node) => {
    fetch(`${API}/api/proof/${node || 'requests'}`).then((r) => r.json()).then(setProof).catch(() => {})
  }, [])

  const { nodes, edges } = useMemo(() => buildGraph(state, act), [state, act])

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: PAPER }}>
      <Header state={state} latest={feed[0]} />
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        {/* ZONE 1 — the agent's mind: its cognitive loop + the stream it drives */}
        <Inbox feed={feed} q={q} setQ={setQ} state={state} act={act} />
        {/* ZONE 2 — the governed agent: focal metrics + the live system map */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <Focal state={state} />
          <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
            {nodes.length === 0 && (
              <div style={{ padding: 24, color: DIM }}>connecting to the live stream…</div>
            )}
            {nodes.length > 0 && (
              <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView
                style={{ width: '100%', height: '100%' }}
                onNodeClick={(e, node) => openProof(node.data.proofNode || node.id)}
                proOptions={{ hideAttribution: true }} nodesDraggable={false}
                nodesConnectable={false} elementsSelectable={false} panOnDrag={false}
                zoomOnScroll={false} zoomOnDoubleClick={false}>
                <Background color="#e7e7e0" gap={22} />
              </ReactFlow>
            )}
          </div>
        </div>
        {/* ZONE 3 — Phoenix: the senses that feed the agent + the courtroom it proves in */}
        <PhoenixPanel state={state} />
      </div>
      {proof && <ProofPanel proof={proof} onClose={() => setProof(null)} />}
      {evalView && <EvalPopup view={evalView} onClose={() => setEvalView(null)} />}
    </div>
  )
}

// ---- the cognitive loop, current step lit (the visible mind) ---------------
const _STEP_LABEL = { OBSERVE: 'observe', DIAGNOSE: 'diagnose', DECIDE: 'decide', ACT: 'act', VERIFY: 'verify' }
function MindLoop({ step, steps }) {
  const seq = steps || ['OBSERVE', 'DIAGNOSE', 'DECIDE', 'ACT', 'VERIFY']
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      {seq.map((s, i) => {
        const on = s === step
        return (
          <React.Fragment key={s}>
            <div style={{ fontSize: 11, fontWeight: on ? 800 : 500, letterSpacing: '.04em',
              padding: '3px 9px', borderRadius: 999, textTransform: 'uppercase',
              color: on ? '#fff' : DIM, background: on ? GREEN : '#f0efe9',
              border: `1px solid ${on ? GREEN : '#e6e4da'}`, transition: 'all .25s' }}>
              {_STEP_LABEL[s] || s.toLowerCase()}
            </div>
            {i < seq.length - 1 && <span style={{ color: '#cfcdc2', fontSize: 12 }}>→</span>}
          </React.Fragment>
        )
      })}
    </div>
  )
}

function Header({ state, latest }) {
  return (
    <div style={{ padding: '12px 22px', borderBottom: '1px solid #eceae0', display: 'flex',
      alignItems: 'center', gap: 24, background: '#fff' }}>
      <div>
        <div style={{ fontSize: 11.5, color: DIM, letterSpacing: '.04em' }}>AI COST GOVERNANCE · CONTROL PLANE</div>
        <div style={{ fontSize: 15.5, fontWeight: 700 }}>An agent governing another agent — live, on Phoenix</div>
      </div>
      <div style={{ marginLeft: 'auto' }}>
        <div style={{ fontSize: 11, color: DIM, marginBottom: 4, textAlign: 'center' }}>the agent's loop</div>
        <MindLoop step={state?.step} steps={state?.steps} />
      </div>
      <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
        <div style={{ fontSize: 12, color: DIM }}>measured saved · live</div>
        <div style={{ fontSize: 24, fontWeight: 700, lineHeight: 1.05, color: GREEN }}>
          ${state ? state.realized_savings.toFixed(4) : '—'}
        </div>
      </div>
    </div>
  )
}

// ---- the focal pair: messages/sec + $/message (co-equal), burn demoted -----
function Focal({ state }) {
  const dpm = useTween(state?.dollars_per_message)
  const base = state?.baseline_dollars_per_message
  const down = state && state.dollars_per_message < base - 1e-9
  const pct = down ? Math.round((1 - state.dollars_per_message / base) * 100) : 0
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 40, padding: '12px 24px 10px',
      borderBottom: '1px solid #eceae0', background: PAPER }}>
      <div>
        <div style={{ fontSize: 11.5, color: DIM, letterSpacing: '.04em' }}>THROUGHPUT</div>
        <div style={{ fontSize: 30, fontWeight: 800, lineHeight: 1, color: INK }}>
          {state ? state.throughput_per_sec.toFixed(2) : '—'}
          <span style={{ fontSize: 15, fontWeight: 500, color: DIM }}> msgs/sec</span>
        </div>
      </div>
      <div style={{ width: 1, alignSelf: 'stretch', background: '#eceae0' }} />
      <div>
        <div style={{ fontSize: 11.5, color: DIM, letterSpacing: '.04em' }}>$ / MESSAGE — what governance moves</div>
        <div style={{ fontSize: 30, fontWeight: 800, lineHeight: 1, color: down ? GREEN : INK }}>
          ${dpm.toFixed(4)}
          {down && <span style={{ fontSize: 15, fontWeight: 600, color: GREEN }}> ▼{pct}%</span>}
        </div>
        {down && <div style={{ fontSize: 11.5, color: DIM, marginTop: 2 }}>
          baseline <span style={{ textDecoration: 'line-through' }}>${base.toFixed(4)}</span>
        </div>}
      </div>
      <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
        <div style={{ fontSize: 11, color: DIM }}>burn rate</div>
        <div style={{ fontSize: 15, fontWeight: 600, color: down ? GREEN : INK }}>
          ${state ? (state.burn_per_min < 0.1 ? state.burn_per_min.toFixed(4) : state.burn_per_min.toFixed(2)) : '—'}/min
        </div>
        <div style={{ fontSize: 11, color: DIM }}>{state ? state.active_count : 0} policies live</div>
      </div>
    </div>
  )
}

function entryStyle(kind) {
  if (kind === 'user') return { accent: '#3b3b37', bg: '#efece2', icon: '' }
  if (kind === 'applied') return { accent: GREEN, bg: '#eaf6f0', icon: '✓ ' }
  if (kind === 'verified') return { accent: GREEN, bg: '#eef7f1', icon: '✦ ' }
  if (kind === 'escalate') return { accent: AMBER, bg: '#fbf0db', icon: '⚑ ' }
  if (kind === 'holding') return { accent: DIM, bg: '#f7f7f3', icon: '' }
  return { accent: GREEN, bg: '#f3faf6', icon: '' } // thinking / reaction / reasoned
}

// The focal "happening now" card — large, highlighted, always visible.
function NowCard({ c }) {
  const s = entryStyle(c.kind)
  const isUser = c.kind === 'user'
  return (
    <div style={{ border: `2px solid ${s.accent}`, background: s.bg, borderRadius: 14,
      padding: '16px 18px', marginBottom: 12 }}>
      <div style={{ fontSize: 11, letterSpacing: '.09em', textTransform: 'uppercase',
        color: s.accent, fontWeight: 800 }}>{isUser ? 'your move' : 'happening now'}</div>
      <div style={{ fontSize: 21, lineHeight: 1.32, color: INK, marginTop: 5 }}>{s.icon}{c.text}</div>
    </div>
  )
}

function Inbox({ feed, q, setQ, state, act }) {
  const latest = feed[0]
  const rest = feed.slice(1).filter((c) => !q || c.text.toLowerCase().includes(q.toLowerCase()))
  return (
    <div style={{ width: 440, borderRight: '1px solid #eceae0', padding: '16px 18px',
      display: 'flex', flexDirection: 'column', background: '#fff', minHeight: 0 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
        <div style={{ fontWeight: 800, fontSize: 18 }}>The agent's mind</div>
        <div style={{ color: GREEN, fontSize: 13 }}>● reasoning live</div>
      </div>
      {state?.pushback && (
        <div style={{ border: `1px solid ${AMBER}`, background: '#fbf0db', borderRadius: 10,
          padding: '12px 14px', marginBottom: 12 }}>
          <div style={{ fontSize: 15, color: '#5a4815' }}>
            Turning off {state.pushback.title.toLowerCase()} leaves <b>~${Number(state.pushback.monthly).toLocaleString()}/mo</b> of pure waste.
          </div>
          <button onClick={() => act('enable', state.pushback.sig)}
            style={{ marginTop: 8, fontSize: 14, border: `1px solid ${AMBER}`, color: '#fff',
              background: AMBER, borderRadius: 7, padding: '5px 14px', cursor: 'pointer' }}>
            re-enable
          </button>
        </div>
      )}
      {latest ? <NowCard c={latest} />
        : <div style={{ color: DIM, fontSize: 17, padding: '14px 0' }}>Reading the live traffic…</div>}
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search history…"
        style={{ padding: '8px 11px', border: '1px solid #e0ded3', borderRadius: 8, marginBottom: 8, fontSize: 14 }} />
      <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
        {rest.map((c) => {
          const s = entryStyle(c.kind)
          return (
            <div key={c.seq} style={{ borderLeft: `3px solid ${s.accent}`, background: s.bg,
              borderRadius: 4, padding: '9px 12px', margin: '8px 0' }}>
              <div style={{ fontSize: 15.5, color: '#23231f', lineHeight: 1.36 }}>
                {c.kind === 'user' ? <b>You: </b> : s.icon}{c.text}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ---- ZONE 3 — Phoenix: the senses that feed the agent + the courtroom -------
function PhoenixPanel({ state }) {
  const v = state?.verify
  const projUrl = 'https://app.phoenix.arize.com/s/tomas'
  return (
    <div style={{ width: 360, borderLeft: '1px solid #eceae0', background: '#fff',
      display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ padding: '16px 18px 10px' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
          <div style={{ fontWeight: 800, fontSize: 18 }}>Phoenix</div>
          <div style={{ color: '#b5791a', fontSize: 12 }}>senses &amp; courtroom</div>
        </div>
        {/* senses: the live traces the agent reads */}
        <div style={{ marginTop: 10, fontSize: 13.5, color: '#3b3b37', display: 'flex',
          alignItems: 'center', gap: 7 }}>
          <span style={{ width: 8, height: 8, borderRadius: 9, background: GREEN,
            display: 'inline-block' }} />
          Live OTEL traces feeding <b>&nbsp;observe</b>
        </div>
        <div style={{ fontSize: 12.5, color: DIM, marginTop: 3 }}>
          The agent senses every governed message here — then it proves the delta here too.
        </div>
      </div>

      {/* courtroom: the VERIFY result, re-measured from the same traffic */}
      <div style={{ padding: '6px 18px 18px', overflowY: 'auto', flex: 1, minHeight: 0 }}>
        <div style={{ fontSize: 11, letterSpacing: '.08em', color: DIM, textTransform: 'uppercase',
          fontWeight: 800, margin: '8px 0 8px' }}>Verified in Phoenix</div>
        {!v && (
          <div style={{ color: DIM, fontSize: 14, padding: '8px 0' }}>
            The agent hasn't enacted yet — the first VERIFY appears the moment it does.
          </div>
        )}
        {v && (
          <div style={{ border: `1.5px solid ${v.same_answer ? GREEN : '#e6e4da'}`,
            background: v.same_answer ? '#eef7f1' : PAPER, borderRadius: 12, padding: '14px 15px' }}>
            <div style={{ fontSize: 14.5, fontWeight: 700, color: INK }}>{v.title}</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 10 }}>
              <span style={{ fontSize: 13, color: DIM, textDecoration: 'line-through' }}>
                ${v.baseline_dollars_per_message.toFixed(4)}
              </span>
              <span style={{ color: DIM }}>→</span>
              <span style={{ fontSize: 24, fontWeight: 800, color: GREEN }}>
                ${v.dollars_per_message.toFixed(4)}
              </span>
              <span style={{ fontSize: 12.5, color: DIM }}>/ message</span>
            </div>
            <div style={{ fontSize: 13, color: '#3b3b37', marginTop: 6 }}>
              ≈ <b>${Number(v.monthly_saving).toLocaleString(undefined, { maximumFractionDigits: 0 })}/mo</b> saved at current volume.
            </div>
            {/* quality verdict — asserted only when the captured pair proves it */}
            <div style={{ marginTop: 10, fontSize: 13, fontWeight: 600,
              color: v.same_answer ? GREEN : AMBER }}>
              {v.same_answer ? '✦ Answer identical — quality held.'
                : '◷ Watching the next traces to confirm quality holds.'}
            </div>
            {v.pair && (
              <div style={{ marginTop: 12, borderTop: '1px solid #eceae0', paddingTop: 10 }}>
                <div style={{ fontSize: 12, color: DIM, marginBottom: 6 }}>
                  Same ticket, two ways · {v.pair.skipped_calls} paid calls skipped
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <a href={v.pair.baseline.phoenix_url} target="_blank" rel="noreferrer"
                    style={ptag(false)}>baseline ${v.pair.baseline.total_usd.toFixed(4)} ↗</a>
                  <a href={v.pair.governed.phoenix_url} target="_blank" rel="noreferrer"
                    style={ptag(true)}>governed ${v.pair.governed.total_usd.toFixed(4)} ↗</a>
                </div>
              </div>
            )}
            <a href={projUrl} target="_blank" rel="noreferrer"
              style={{ display: 'inline-block', marginTop: 12, fontSize: 13, color: GREEN, fontWeight: 600 }}>
              open in Phoenix Cloud ↗
            </a>
          </div>
        )}
        <div style={{ fontSize: 11.5, color: DIM, marginTop: 14 }}>
          System behaviour only — span names, counts, cost. No prompt text or PII.
        </div>
      </div>
    </div>
  )
}
function ptag(gov) {
  return { fontSize: 12, padding: '4px 9px', borderRadius: 7, textDecoration: 'none',
    border: `1px solid ${gov ? GREEN : '#d8d6cc'}`, color: gov ? GREEN : '#5a5852',
    background: gov ? '#eef7f1' : '#fff' }
}

// ---- the consequence popup: the accelerated quality eval (HOLD case) -------
// Your decision (route simple tickets to the cheaper model) goes live, and this
// instruments it: the REAL pre-run eval is revealed row-by-row on a disclosed
// compressed clock. The scoring is real; only the wall-clock is compressed. The
// verdict is the agent's judgment over the signals and carries NO Phoenix link;
// each replayed row links to its real trace (evidence).
function qColor(b, e) { return e >= b ? GREEN : AMBER }

function EvalPopup({ view, onClose }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [revealed, setRevealed] = useState(0)

  useEffect(() => {
    fetch(`${API}/api/eval/${view.key}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setData).catch(setErr)
  }, [view.key])

  useEffect(() => {
    if (!data) return
    const n = data.rows.length
    setRevealed(0)
    const per = Math.max(900, Math.round(10000 / n))  // ~10s total reveal
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

  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 60 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: '#fff', borderRadius: 14, padding: 24,
        width: 660, maxHeight: '88vh', overflowY: 'auto', boxShadow: '0 14px 50px rgba(0,0,0,.3)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div style={{ fontSize: 11, letterSpacing: '.09em', color: AMBER, fontWeight: 800 }}>YOUR DECISION · ARMED &amp; LIVE</div>
            <div style={{ fontSize: 18, fontWeight: 800, marginTop: 2 }}>Route simple tickets → economy model</div>
            <div style={{ fontSize: 12.5, color: DIM, marginTop: 3 }}>
              Accelerated eval · real replays through both models, scored by an LLM-judge · clock compressed to ~10s
            </div>
          </div>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 24, cursor: 'pointer', color: DIM }}>×</button>
        </div>

        {err && <div style={{ color: AMBER, padding: '16px 0' }}>No cached eval yet — pre-run it off-stage.</div>}
        {!data && !err && <div style={{ color: DIM, padding: '16px 0' }}>Loading the eval…</div>}

        {data && (
          <>
            {/* progress on the disclosed clock */}
            <div style={{ height: 4, background: '#eee', borderRadius: 3, margin: '16px 0 14px', overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${(revealed / data.rows.length) * 100}%`,
                background: hold ? GREEN : AMBER, transition: 'width .4s' }} />
            </div>

            {/* the signals, forming as rows reveal */}
            <div style={{ display: 'flex', gap: 10, marginBottom: 14 }}>
              <Tile label="answer quality" main={`${mBase.toFixed(1)} → ${mEcon.toFixed(1)}`}
                color={qColor(mBase, mEcon)} sub="baseline → economy" />
              <Tile label="same resolution" main={`${equiv}/${shown.length || 0}`}
                color={equiv === shown.length ? GREEN : AMBER} sub="judged equivalent" />
              <Tile label="new clarifications" main={`${clar}`}
                color={clar ? AMBER : GREEN} sub="economy asked back" />
              <Tile label="refused / escalated" main={`${refused}`}
                color={refused ? AMBER : GREEN} sub="instead of resolving" />
            </div>

            {/* the replayed rows — each links to its real Phoenix trace (evidence) */}
            <div style={{ border: '1px solid #eceae0', borderRadius: 10, overflow: 'hidden' }}>
              {shown.map((r, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px',
                  borderTop: i ? '1px solid #f1efe8' : 'none', fontSize: 13 }}>
                  <span style={{ flex: 1, color: '#23231f' }}>{r.ticket.replace(/Account [A-Z]+-\d+\.?/g, '').slice(0, 58)}</span>
                  <span style={{ fontWeight: 700, color: qColor(r.baseline_quality, r.economy_quality) }}>
                    {r.baseline_quality} → {r.economy_quality}
                  </span>
                  {r.equivalent
                    ? <span style={{ fontSize: 11, color: GREEN }}>● equivalent</span>
                    : <span style={{ fontSize: 11, color: AMBER }}>● {r.clarified ? 'clarified' : r.refused_escalated ? 'escalated' : 'differs'}</span>}
                  {r.phoenix_url && <a href={r.phoenix_url} target="_blank" rel="noreferrer"
                    style={{ fontSize: 12, color: GREEN, textDecoration: 'none' }}>trace ↗</a>}
                </div>
              ))}
            </div>

            {/* the agent's verdict — judgment, no Phoenix link */}
            {done && (
              <div style={{ marginTop: 16, border: `1.5px solid ${hold ? GREEN : AMBER}`,
                background: hold ? '#eef7f1' : '#fbf0db', borderRadius: 12, padding: '14px 16px' }}>
                <div style={{ fontSize: 11, letterSpacing: '.08em', fontWeight: 800,
                  color: hold ? GREEN : AMBER }}>THE AGENT'S VERDICT</div>
                <div style={{ fontSize: 16, color: INK, marginTop: 4 }}>
                  {hold
                    ? '✦ Quality held — the economy model resolves these as well as the baseline. Routing stays live, and I keep watching.'
                    : '⚑ Quality dropped — the economy model is degrading these answers. I recommend reverting.'}
                </div>
                <div style={{ fontSize: 11.5, color: DIM, marginTop: 7 }}>
                  My judgment over the signals above — Phoenix surfaces the evidence, I render the verdict.
                </div>
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
    <div style={{ flex: 1, border: '1px solid #eceae0', borderRadius: 10, padding: '9px 11px' }}>
      <div style={{ fontSize: 10.5, color: DIM, letterSpacing: '.04em', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 19, fontWeight: 800, color, lineHeight: 1.2 }}>{main}</div>
      <div style={{ fontSize: 10.5, color: DIM }}>{sub}</div>
    </div>
  )
}

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

// ---- derive the React Flow graph: a LANE per workload (conversation type),
//      each showing the operations it runs and the lever governing them -------
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
