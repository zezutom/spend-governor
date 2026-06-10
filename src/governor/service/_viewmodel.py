"""The view-model — per-class cost rows + totals, the volume→projection, the
tool-cost decomposition, the issue→policy lever mapping, and the live-state /
recommendations loaders. Pure compute (no UI); the public interface in
__init__.py is what the control-plane API serves.
"""

import json

from governor.pipeline.db import connect, get_meta


# Cheapest task class — the per-ticket baseline every class is compared against.
BASELINE_CLASS = "password_reset"

# Module-cached Phoenix project node id (see _project_gid).
_PROJECT_GID: str | None = None

def _cache_span_count() -> int:
    try:
        with connect() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM spans").fetchone()
            return int(row["n"] or 0)
    except Exception:
        return 0


def _load_live_state() -> dict:
    """Single source of truth — read one blob, render every UI element
    from it. No divergent cadences between counters and aggregates."""
    raw = get_meta("live_state")
    if not raw:
        return {
            "ingest": {"status": "idle"},
            "summary": {"total_traces": 0, "total_spans": 0, "total_cost_usd": 0.0},
            "by_task_class": {},
            "anomalies": [],
        }
    try:
        return json.loads(raw)
    except Exception:
        return {
            "ingest": {"status": "idle"},
            "summary": {"total_traces": 0, "total_spans": 0, "total_cost_usd": 0.0},
            "by_task_class": {},
            "anomalies": [],
        }


def _load_recommendations() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT signature, source, task_class, anomaly_type, title, "
            "description, data, updated_at "
            "FROM recommendations WHERE superseded = 0"
        ).fetchall()
    recs = [dict(r) for r in rows]

    # Always rank by impact (projected monthly savings), descending.
    # Applied/updated state must NOT change the order — the biggest
    # leak stays on top whether or not it's been actioned.
    def _impact(r: dict) -> float:
        try:
            return float(json.loads(r.get("data") or "{}").get("monthly_savings_usd") or 0)
        except Exception:
            return 0.0

    recs.sort(key=_impact, reverse=True)
    return recs


def _issue_of(rec: dict) -> dict:
    try:
        return json.loads(rec.get("data") or "{}")
    except Exception:
        return {}


def _policy_for_issue(issue: dict):
    """Map a detected issue to the wrapper policy that fixes it at
    runtime. Returns (signature, policy_type, params) or None."""
    kind = issue.get("kind")
    if kind == "tool_cache" and issue.get("primary_tool"):
        tool = issue["primary_tool"]
        unit = (issue.get("components") or {}).get("tool_unit_price_usd", 0.0)
        return (f"cache_tool:{tool}", "cache_tool",
                {"tool": tool, "cost_per_call_usd": unit})
    if kind == "model_routing":
        # Spread the per-ticket saving across ~4 LLM calls/ticket for a
        # per-call estimate the wrapper records on each downgrade.
        per_call = round((issue.get("savings_per_ticket_usd", 0) or 0) / 4.0, 6)
        return ("route_model:simple", "route_model",
                {"cheap_model": issue.get("cheap_model", "gemini-2.5-flash-lite"),
                 "est_savings_per_call_usd": per_call})
    return None


def _affected_classes(issue: dict) -> list[str]:
    if issue.get("kind") == "model_routing":
        return list(issue.get("classes") or [])
    tc = issue.get("task_class")
    return [tc] if tc else []


def _story_cause(issue: dict) -> str:
    """Plain-English cause + fix — the line that triggers the AHA."""
    comp = issue.get("components") or {}
    if issue.get("kind") == "model_routing":
        cheap = issue.get("cheap_model", "a cheaper model")
        return (f"Simple tickets (password resets, FAQs) ran on the standard model. "
                f"Routed to **{cheap}** at the gateway — same answer, far cheaper input.")
    tool = issue.get("primary_tool") or "the tool"
    removed = round(comp.get("avg_tool_calls_removed", 0) or 0)
    return (f"Tickets re-ran the **same `{tool}`** ~{removed}× — pure waste. "
            f"Now served from a semantic cache; the paid call never fires.")


def _project_gid() -> str | None:
    """Phoenix project node id for span deeplinks. Module-cached so a
    transient None is retried next render — st.cache_data would pin the
    None forever and silently kill the verify links."""
    global _PROJECT_GID
    if not _PROJECT_GID:
        from governor.pipeline.phoenix_cost import project_gid
        _PROJECT_GID = project_gid()
    return _PROJECT_GID


def _default_tool_rates() -> dict:
    """Operator-set per-call rates, editable in the math drawer. Drops the
    structural zero-cost classifier — it's not a billable tool."""
    from governor.pricing.tools import TOOL_PRICES
    return {k: float(v) for k, v in TOOL_PRICES.items() if k != "task_classifier"}


def _tool_cost(counts: dict, rates: dict) -> float:
    """Tool dollars for one ticket = Σ(calls_for_tool × rate_for_tool) over the
    class's ACTUAL tool mix (refinement #3). At the default rates this equals
    the stored avg_tool_cost_usd exactly — tools absent from `rates`
    (task_classifier, merged spans) price at 0, as Phoenix counts them."""
    return sum((counts.get(t, 0) or 0) * r for t, r in rates.items())


def _observed_hours(recs: list[dict]) -> float:
    for r in recs:
        wd = (_issue_of(r).get("components") or {}).get("window_days")
        if wd:
            return wd * 24
    return 12.0


def _default_monthly_tickets(live: dict, recs: list[dict]) -> int:
    by = live.get("by_task_class") or {}
    total_n = int((live.get("summary") or {}).get("total_traces") or 0) or sum(s["n"] for s in by.values())
    wd = 0.5
    for r in recs:
        wd = (_issue_of(r).get("components") or {}).get("window_days") or wd
        break
    return int(round(total_n / max(wd, 1e-9) * 30)) if total_n else 0


def _issue_rows(live: dict, recs: list[dict], rates: dict):
    """Per-class derived view + page totals, recomputed from the live per-tool
    rate table. LLM cost is Phoenix-measured (fixed); the tool segment and the
    tool half of each saving move with the rates. At default rates this
    reproduces the stored numbers exactly. Returns (rows, totals)."""
    by = live.get("by_task_class") or {}
    total_n = int((live.get("summary") or {}).get("total_traces") or 0) or sum(s["n"] for s in by.values())
    cache_rec, routing = {}, None
    for r in recs:
        i = _issue_of(r)
        if i.get("kind") == "tool_cache" and i.get("task_class"):
            cache_rec[i["task_class"]] = i
        elif i.get("kind") == "model_routing":
            routing = i
    base_s = by.get(BASELINE_CLASS) or {}
    base_cost = ((base_s.get("avg_llm_cost_usd", 0) or 0)
                 + _tool_cost(base_s.get("avg_tool_counts") or {}, rates)) or 1e-9
    rows, rec_tot, cost_tot = [], 0.0, 0.0
    for tc, s in by.items():
        n = s["n"]
        llm = s["avg_llm_cost_usd"]
        tool = _tool_cost(s.get("avg_tool_counts") or {}, rates)
        cost = llm + tool
        saving = 0.0
        ci = cache_rec.get(tc)
        if ci:
            comp = ci.get("components") or {}
            removed = comp.get("avg_tool_calls_removed", 0) or 0
            ts = removed * rates.get(ci.get("primary_tool"), 0.0)  # tool half scales with rate
            ls = (ci.get("savings_per_ticket_usd", 0) or 0) - (comp.get("tool_savings_per_ticket_usd", 0) or 0)
            saving += ts + ls
        if routing and tc in (routing.get("classes") or []):
            saving += routing.get("savings_per_ticket_usd", 0) or 0
        rows.append({"tc": tc, "n": n, "cost": cost, "llm": llm, "tool": tool,
                     "saving": saving, "mult": cost / base_cost,
                     "is_base": tc == BASELINE_CLASS})
        rec_tot += saving * n; cost_tot += cost * n
    for r in rows:
        r["share"] = (r["cost"] * r["n"]) / (cost_tot or 1e-9)
    rows.sort(key=lambda r: r["cost"] * r["n"], reverse=True)
    cpt = cost_tot / total_n if total_n else 0
    rpt = rec_tot / total_n if total_n else 0
    return rows, {"total_n": total_n, "cost_per_ticket": cpt,
                  "recoverable_per_ticket": rpt,
                  "pct_avoidable": (rpt / cpt) if cpt else 0}


def _class_reasons(recs: list[dict]) -> dict:
    out: dict = {}
    for r in recs:
        i = _issue_of(r)
        if i.get("kind") == "tool_cache" and i.get("task_class"):
            comp = i.get("components") or {}
            n = round(comp.get("avg_tool_calls_removed", 0) or 0)
            out.setdefault(i["task_class"], []).append(
                f"repeats the same `{i.get('primary_tool', 'tool')}` ~{n}×")
        if i.get("kind") == "model_routing":
            for c in (i.get("classes") or []):
                out.setdefault(c, []).append("runs on the full-price model")
    return {tc: " · ".join(parts) for tc, parts in out.items()}


def _policy_mo(issue: dict, rates: dict, mt: int, total_n: int) -> float:
    n = issue.get("n_traces", 0) or 0
    if issue.get("kind") == "tool_cache":
        comp = issue.get("components") or {}
        spt = ((comp.get("avg_tool_calls_removed", 0) or 0) * rates.get(issue.get("primary_tool"), 0.0)
               + ((issue.get("savings_per_ticket_usd", 0) or 0)
                  - (comp.get("tool_savings_per_ticket_usd", 0) or 0)))
    else:
        spt = issue.get("savings_per_ticket_usd", 0) or 0
    return spt * mt * (n / total_n if total_n else 0)


def _policy_per_ticket(issue: dict, rates: dict) -> float:
    if issue.get("kind") == "tool_cache":
        comp = issue.get("components") or {}
        return ((comp.get("avg_tool_calls_removed", 0) or 0) * rates.get(issue.get("primary_tool"), 0.0)
                + ((issue.get("savings_per_ticket_usd", 0) or 0)
                   - (comp.get("tool_savings_per_ticket_usd", 0) or 0)))
    return issue.get("savings_per_ticket_usd", 0) or 0


def _fix_text(issue: dict):
    if issue.get("kind") == "model_routing":
        pct = int(round((issue.get("pct_reduction", 0) or 0) * 100))
        return ("Route simple tickets to a cheaper model",
                f"Resets and FAQs go to `{issue.get('cheap_model', 'a cheaper model')}` "
                f"— same answer, {pct}% less.")
    tool = issue.get("primary_tool", "the tool")
    cls = (issue.get("task_class", "") or "").replace("_", " ")
    return (f"Cache the repeated {cls} lookups",
            f"Serve the duplicate `{tool}` from cache — the paid call never fires.")
