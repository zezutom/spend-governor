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
  const openProof = useCallback((node) => {
    fetch(`${API}/api/proof/${node || 'requests'}`).then((r) => r.json()).then(setProof).catch(() => {})
  }, [])

  const burn = useTween(state?.burn_rate)
  const down = state && state.burn_rate < state.gross_burn - 1e-9

  const { nodes, edges } = useMemo(() => buildGraph(state, act), [state, act])

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
              onNodeClick={(e, node) => openProof(node.data.proofNode || node.id)}
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
        <div style={{ fontSize: 12, color: DIM }}>measured saved (live)</div>
        <div style={{ fontSize: 22, fontWeight: 700, lineHeight: 1.1, color: GREEN }}>
          ${state ? state.realized_savings.toFixed(4) : '—'}
        </div>
      </div>
      <div style={{ textAlign: 'right' }}>
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

function entryStyle(kind) {
  if (kind === 'user') return { accent: '#3b3b37', bg: '#efece2', icon: '' }
  if (kind === 'applied') return { accent: GREEN, bg: '#eaf6f0', icon: '✓ ' }
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
    <div style={{ width: 460, borderRight: '1px solid #eceae0', padding: '16px 18px',
      display: 'flex', flexDirection: 'column', background: '#fff', minHeight: 0 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
        <div style={{ fontWeight: 800, fontSize: 18 }}>Agent inbox</div>
        <div style={{ color: GREEN, fontSize: 13 }}>● reasoning over live traffic</div>
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
