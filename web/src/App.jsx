import React, { useEffect, useState, useRef, useCallback, useMemo } from 'react'
import ReactFlow, { Background, Handle, Position } from 'reactflow'
import 'reactflow/dist/style.css'

const API = 'http://localhost:8800'
const GREEN = '#0f6e56', AMBER = '#b5791a', DIM = '#9ca3af', INK = '#141413', PAPER = '#fbfbf9'

// ---- a control-point node: state + the inline affordances to act on it ----
function CtrlNode({ data }) {
  const c = data.state === 'green' ? GREEN : data.state === 'amber' ? AMBER
    : data.state === 'escalate' ? AMBER : '#b7b6ae'
  const fill = data.state === 'green' ? '#e6f5ef' : data.state === 'amber' ? '#fbf0db'
    : data.state === 'escalate' ? '#fbf0db' : '#fff'
  return (
    <div onClick={data.onInspect}
      style={{ border: `1.5px solid ${c}`, background: fill, borderRadius: 12, padding: '10px 14px',
        minWidth: 150, cursor: 'pointer', boxShadow: '0 1px 4px rgba(0,0,0,.05)' }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div style={{ fontWeight: 700, fontSize: 14, color: INK }}>{data.label}</div>
      <div style={{ fontSize: 11.5, color: c }}>{data.sub}</div>
      {data.escalate && (
        <div style={{ marginTop: 6, fontSize: 11, color: AMBER }}>
          ⚑ I want to route this — your call
          <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
            <Act onClick={(e) => { e.stopPropagation(); data.act('accept', data.escalate) }} label="accept" primary />
            <Act onClick={(e) => { e.stopPropagation(); data.act('reject', data.escalate) }} label="reject" />
          </div>
        </div>
      )}
      {data.vetoable && (
        <button onClick={(e) => { e.stopPropagation(); data.act('veto', data.vetoable) }}
          style={{ marginTop: 6, fontSize: 11, border: `1px solid ${GREEN}`, color: GREEN,
            background: '#fff', borderRadius: 6, padding: '2px 8px', cursor: 'pointer' }}>
          veto
        </button>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  )
}
function Act({ onClick, label, primary }) {
  return <button onClick={onClick} style={{ fontSize: 11, borderRadius: 6, padding: '2px 8px',
    cursor: 'pointer', border: `1px solid ${AMBER}`, color: primary ? '#fff' : AMBER,
    background: primary ? AMBER : '#fff' }}>{label}</button>
}
const nodeTypes = { ctrl: CtrlNode }

// ---- burn-rate counter that eases to its new value (semantic motion) ------
function useTween(target) {
  const [v, setV] = useState(target ?? 0)
  const ref = useRef(target ?? 0)
  useEffect(() => {
    if (target == null) return
    const from = ref.current, t0 = performance.now(), dur = 1100
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
  }, [])
  const openProof = useCallback(() => {
    fetch(`${API}/api/proof`).then((r) => r.json()).then(setProof).catch(() => {})
  }, [])

  const burn = useTween(state?.burn_rate)
  const down = state && state.burn_rate < state.gross_burn - 1e-9

  const { nodes, edges } = useMemo(() => buildGraph(state, act, openProof), [state, act, openProof])

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      <Header burn={burn} down={down} state={state} latest={feed[0]} />
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        <Inbox feed={feed} q={q} setQ={setQ} state={state} act={act} />
        <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
          {nodes.length === 0 && (
            <div style={{ padding: 24, color: DIM }}>connecting to the live stream…</div>
          )}
          {nodes.length > 0 && (
            <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView
              style={{ width: '100%', height: '100%' }}
              proOptions={{ hideAttribution: true }} nodesDraggable={false}
              nodesConnectable={false} elementsSelectable={false} panOnDrag={false}
              zoomOnScroll={false} zoomOnDoubleClick={false}>
              <Background color="#e7e7e0" gap={22} />
            </ReactFlow>
          )}
        </div>
      </div>
      {proof && <ProofPanel proof={proof} onClose={() => setProof(null)} />}
    </div>
  )
}

function Header({ burn, down, state, latest }) {
  return (
    <div style={{ padding: '14px 22px', borderBottom: '1px solid #eceae0', display: 'flex',
      alignItems: 'center', gap: 28, background: PAPER }}>
      <div>
        <div style={{ fontSize: 12, color: DIM }}>AI Cost Governance · control plane</div>
        <div style={{ fontSize: 15, fontWeight: 600 }}>An agent governing another agent — autonomously</div>
      </div>
      <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
        <div style={{ fontSize: 12, color: DIM }}>burn rate</div>
        <div style={{ fontSize: 38, fontWeight: 800, lineHeight: 1, color: down ? GREEN : INK }}>
          ${burn < 0.1 ? burn.toFixed(4) : burn.toFixed(2)}<span style={{ fontSize: 18 }}>/min {down ? '▼' : ''}</span>
        </div>
      </div>
      <div style={{ minWidth: 280, maxWidth: 360 }}>
        <div style={{ fontSize: 12, color: DIM }}>latest</div>
        <div style={{ fontSize: 14, color: INK }}>{latest ? latest.text : 'reading the live traffic…'}</div>
      </div>
    </div>
  )
}

function Inbox({ feed, q, setQ, state, act }) {
  const filtered = feed.filter((c) => !q || c.text.toLowerCase().includes(q.toLowerCase()))
  return (
    <div style={{ width: 380, borderRight: '1px solid #eceae0', padding: '14px 16px',
      display: 'flex', flexDirection: 'column', background: '#fff' }}>
      <div style={{ fontWeight: 700 }}>Agent inbox</div>
      <div style={{ color: GREEN, fontSize: 13, marginBottom: 8 }}>● reasoning over live traffic</div>
      {state?.pushback && (
        <div style={{ border: `1px solid ${AMBER}`, background: '#fbf0db', borderRadius: 8,
          padding: '10px 12px', marginBottom: 10 }}>
          <div style={{ fontSize: 13.5, color: '#5a4815' }}>
            Turning off {state.pushback.title.toLowerCase()} leaves <b>~${Number(state.pushback.monthly).toLocaleString()}/mo</b> of pure waste.
          </div>
          <button onClick={() => act('enable', state.pushback.sig)}
            style={{ marginTop: 6, fontSize: 12, border: `1px solid ${AMBER}`, color: '#fff',
              background: AMBER, borderRadius: 6, padding: '3px 10px', cursor: 'pointer' }}>
            re-enable
          </button>
        </div>
      )}
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search what the agent did…"
        style={{ padding: '7px 10px', border: '1px solid #e0ded3', borderRadius: 8, marginBottom: 8, fontSize: 14 }} />
      <div style={{ overflowY: 'auto', flex: 1 }}>
        {filtered.length === 0 && <div style={{ color: DIM, fontSize: 14 }}>· reading the live traffic…</div>}
        {filtered.map((c) => {
          const applied = c.kind === 'applied'
          const accent = applied ? GREEN : c.kind === 'escalate' ? AMBER : '#d8d6cc'
          return (
            <div key={c.seq} style={{ borderLeft: `3px solid ${accent}`,
              background: applied ? '#f3faf6' : '#fafaf7', borderRadius: 4, padding: '8px 12px', margin: '7px 0' }}>
              <div style={{ fontSize: 15, color: '#23231f', lineHeight: 1.35 }}>
                {applied ? '✓ ' : c.kind === 'escalate' ? '⚑ ' : ''}{c.text}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ProofPanel({ proof, onClose }) {
  const b = proof.baseline, g = proof.governed
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.35)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: '#fff', borderRadius: 12, padding: 22,
        width: 560, maxHeight: '82vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,.25)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <b>Trace insight — system behaviour only</b>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 22, cursor: 'pointer', color: DIM }}>×</button>
        </div>
        <div style={{ fontSize: 14, margin: '8px 0' }}>
          Same ticket, two ways: baseline <b>${b.total_usd.toFixed(4)}</b> → governed <b>${g.total_usd.toFixed(4)}</b>,
          {' '}{proof.skipped_calls} paid calls skipped, saved <b>${proof.saved_usd.toFixed(4)}</b>.
        </div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead><tr style={{ color: DIM, textAlign: 'left' }}><th>call</th><th style={{ textAlign: 'right' }}>baseline</th><th>governed</th></tr></thead>
          <tbody>
            {proof.rows.map((r, i) => {
              const cached = r.status === 'cached'
              return (<tr key={i} style={{ background: cached ? '#fbf0db' : 'transparent' }}>
                <td style={{ fontFamily: 'monospace', padding: '2px 4px' }}>{r.op}</td>
                <td style={{ textAlign: 'right', padding: '2px 4px', color: cached ? AMBER : '#555' }}>${r.baseline.cost.toFixed(4)}</td>
                <td style={{ padding: '2px 4px', color: cached ? GREEN : '#555' }}>{cached ? 'cached · $0' : `$${r.governed.cost.toFixed(4)}`}</td>
              </tr>)
            })}
          </tbody>
        </table>
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          {b.phoenix_url && <a href={b.phoenix_url} target="_blank" rel="noreferrer">Baseline in Phoenix ↗</a>}
          {g.phoenix_url && <a href={g.phoenix_url} target="_blank" rel="noreferrer">Governed in Phoenix ↗</a>}
        </div>
        <div style={{ fontSize: 12, color: DIM, marginTop: 8 }}>System behaviour only — span names, counts, cost. No prompt text or PII.</div>
      </div>
    </div>
  )
}

// ---- derive the React Flow graph from the live state ----------------------
function buildGraph(state, act, openProof) {
  if (!state) return { nodes: [], edges: [] }
  const lv = state.levers
  const toolLevers = lv.filter((l) => l.type === 'cache_tool')
  const routeLever = lv.find((l) => l.type === 'route_model')
  const toolsGov = toolLevers.some((l) => l.active)
  const modelGov = routeLever?.active
  const modelEsc = routeLever?.escalated && !routeLever?.active
  const activeToolVeto = toolLevers.find((l) => l.active)?.sig

  const nodes = [
    n('requests', 0, 130, { label: 'Requests', sub: 'live traffic', state: 'neutral', onInspect: openProof }),
    n('router', 200, 130, { label: 'Router', sub: 'classify', state: 'neutral', onInspect: openProof }),
    n('gateway', 410, 60, { label: 'Tool gateway', sub: '', state: 'neutral', onInspect: openProof }),
    n('tools', 620, 60, {
      label: toolsGov ? 'Cache' : 'External tools', sub: toolsGov ? 'cached · $0' : 'paid per call',
      state: toolsGov ? 'green' : 'amber', act, onInspect: openProof,
      vetoable: toolsGov ? activeToolVeto : null,
    }),
    n('model', 410, 230, {
      label: modelGov ? 'Economy model' : 'Premium model', sub: modelGov ? 'flash-lite' : 'full-price',
      state: modelGov ? 'green' : modelEsc ? 'escalate' : 'amber', act, onInspect: openProof,
      vetoable: modelGov ? routeLever.sig : null, escalate: modelEsc ? routeLever.sig : null,
    }),
  ]
  const e = (id, s, t, on) => ({ id, source: s, target: t, animated: true,
    style: { stroke: on === false ? AMBER : on === true ? GREEN : '#c8c7c0', strokeWidth: 2 } })
  const edges = [
    e('e1', 'requests', 'router'), e('e2', 'router', 'gateway'),
    e('e3', 'gateway', 'tools', toolsGov), e('e4', 'router', 'model', modelGov),
  ]
  return { nodes, edges }
}
function n(id, x, y, data) { return { id, type: 'ctrl', position: { x, y }, data, draggable: false } }
