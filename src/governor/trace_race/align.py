"""Structural alignment of a baseline vs governed trace (the trace race).

The governed run is a DIFFERENT execution, so its spans carry different ids
than the baseline's — matching by id is impossible. They are matched by
STRUCTURAL POSITION: same operation, same occurrence, in call order. From
that, every row is one of three kinds:

- unchanged  — present in both, same operation/model.
- cached     — a tool call the governed run served from cache (cache_hit,
               zero paid cost) while the baseline paid for it. The most
               legible diff, and what the caching race ships first.
- swapped    — same operation, cheaper model in the governed run (deferred).

Alignment is computed ONCE here, before any playback; the animation renders
an already-aligned result. Per the build brief, the row-matching is the part
that lives or dies, so this stays plain and verifiable.
"""

from governor.pipeline.db import connect


# Span kinds that carry a cost or a visible operation — the rows that race.
# The CHAIN/AGENT roots are structural wrappers; we keep them out of the
# raced rows but use the AGENT root for the trace's input/output.
_RACED_KINDS = ("LLM", "TOOL")


def load_spans(trace_id: str, conn=None) -> list[dict]:
    """Ordered raced spans for one trace, oldest first."""
    def _run(c) -> list[dict]:
        rows = c.execute(
            "SELECT span_id, parent_id, span_kind, name, tool_name, "
            "COALESCE(cache_hit,0) cache_hit, model_name, "
            "COALESCE(llm_cost_usd,0) llm_cost, COALESCE(tool_cost_usd,0) tool_cost, "
            "COALESCE(savings_usd,0) savings, phoenix_node_id, start_time "
            "FROM spans WHERE trace_id = ? ORDER BY start_time",
            (trace_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    if conn is None:
        with connect() as c:
            return _run(c)
    return _run(conn)


def _op_key(s: dict) -> tuple:
    """Identity of a span by operation, not id — what structural matching
    pairs on. A tool is keyed by tool name; an LLM by its kind (the model can
    differ between lanes, that is the 'swapped' case)."""
    if s["span_kind"] == "TOOL":
        return ("TOOL", s.get("tool_name") or s.get("name") or "")
    return ("LLM",)


def _raced(spans: list[dict]) -> list[dict]:
    return [s for s in spans if s["span_kind"] in _RACED_KINDS]


def align(baseline_spans: list[dict], governed_spans: list[dict]) -> dict:
    """Pair baseline and governed raced spans by structural position and
    classify each row. Returns rows + reconciled totals.

    Matching walks both ordered sequences in lockstep on the op-key. For the
    caching race the two sequences are identical in shape (the governed run
    still emits the web_search span, flagged cache_hit) so this is a 1:1 zip;
    the parallel walk also tolerates a genuine skip (a span absent on the
    governed side) by holding the governed cursor."""
    b = _raced(baseline_spans)
    g = _raced(governed_spans)

    rows: list[dict] = []
    i = j = 0
    base_cum = gov_cum = 0.0
    while i < len(b):
        bs = b[i]
        gs = g[j] if j < len(g) else None
        b_cost = bs["llm_cost"] + bs["tool_cost"]

        if gs is not None and _op_key(bs) == _op_key(gs):
            g_cost = gs["llm_cost"] + gs["tool_cost"]
            if gs["span_kind"] == "TOOL" and gs["cache_hit"] and not bs["cache_hit"]:
                status = "cached"
            elif gs["span_kind"] == "LLM" and (gs.get("model_name") != bs.get("model_name")):
                status = "swapped"
            else:
                status = "unchanged"
            base_cum += b_cost
            gov_cum += g_cost
            rows.append({
                "op": bs.get("tool_name") or (bs.get("model_name") or "llm"),
                "kind": bs["span_kind"],
                "status": status,
                "baseline": {"cost": round(b_cost, 6), "node": bs.get("phoenix_node_id")},
                "governed": {"cost": round(g_cost, 6),
                             "saved": round(gs.get("savings") or 0, 6),
                             "cache_hit": bool(gs["cache_hit"]),
                             "model": gs.get("model_name"),
                             "node": gs.get("phoenix_node_id")},
                "base_cum": round(base_cum, 6),
                "gov_cum": round(gov_cum, 6),
            })
            i += 1
            j += 1
        else:
            # baseline span with no governed counterpart at this position —
            # a true skip (governed omitted the call entirely).
            base_cum += b_cost
            rows.append({
                "op": bs.get("tool_name") or (bs.get("model_name") or "llm"),
                "kind": bs["span_kind"],
                "status": "cached",
                "baseline": {"cost": round(b_cost, 6), "node": bs.get("phoenix_node_id")},
                "governed": {"cost": 0.0, "saved": round(b_cost, 6),
                             "cache_hit": True, "model": None, "node": None},
                "base_cum": round(base_cum, 6),
                "gov_cum": round(gov_cum, 6),
            })
            i += 1

    base_total = round(sum(s["llm_cost"] + s["tool_cost"] for s in b), 6)
    gov_total = round(sum(s["llm_cost"] + s["tool_cost"] for s in g), 6)
    return {
        "rows": rows,
        "baseline_total_usd": base_total,
        "governed_total_usd": gov_total,
        "saved_usd": round(base_total - gov_total, 6),
        "skipped_calls": sum(1 for r in rows if r["status"] == "cached"),
        "swapped_calls": sum(1 for r in rows if r["status"] == "swapped"),
    }
