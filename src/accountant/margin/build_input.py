"""Deterministic builder for the Margin Agent's INPUT.

ALL arithmetic lives here, never in the agent. It reads the SAME measured
cost vector the cost dashboard renders (per-task LLM + tool cost, and the
per-policy savings) plus the operator's price/target and the segment
provider's facts, and emits the exact INPUT contract the agent consumes.

Two entry points:
- `build_margin_input(...)` — pure: takes already-normalised cost + policy
  + segment data. Independently testable.
- `from_live_state(...)` — adapter: normalises the dashboard's `live_state`
  + recommendations + tool rates into that shape, then calls the builder.

credit_value_usd is a COST anchor (no margin division): the volume-weighted
average cost per ticket on the raw and governed bases. Per-segment margins
derive independently from each segment's own usage_mix x the per-task cost
vector vs its price_per_ticket.
"""

from accountant.margin.segments import SegmentProvider, SegmentSpec


# Operator's stated gross-margin target. A single global setting for now
# (a per-segment / UI-set target can replace this later).
DEFAULT_TARGET_MARGIN = 0.80

# Not a sellable task type — measured volume with no task_classifier.
_NON_TASK = "unknown"


def _round(x: float, n: int) -> float:
    return round(float(x), n)


def _margin(price: float, cost: float) -> float:
    return (price - cost) / price if price else 0.0


def build_margin_input(
    task_cost: dict[str, dict],
    policies: list[dict],
    segments: list[SegmentSpec],
    *,
    target_margin: float,
    monthly_volume: int,
    baseline_task: str,
    tiering_cost_basis: str = "raw",
) -> dict:
    """Assemble the INPUT object.

    task_cost: {task_type: {n, raw_cost, llm, tool}} — includes the
        non-sellable 'unknown' bucket (used only to weight the global
        credit_value anchor, never tiered or sold).
    policies: [{policy_id, affects, per_task_saving:{tc: usd},
        projected_saving_per_ticket_usd}].
    """
    # Per-task fully-governed cost = raw minus every policy saving on it.
    saving_on: dict[str, float] = {}
    for tc in task_cost:
        saving_on[tc] = sum(
            p["per_task_saving"].get(tc, 0.0)
            for p in policies if tc in p.get("affects", [])
        )

    def governed_cost(tc: str) -> float:
        return max(task_cost[tc]["raw_cost"] - saving_on.get(tc, 0.0), 0.0)

    base_raw = task_cost[baseline_task]["raw_cost"] or 1e-9
    base_gov = governed_cost(baseline_task) or 1e-9

    # --- task_types[] (sellable types only) ---
    task_types = []
    for tc, c in task_cost.items():
        if tc == _NON_TASK:
            continue
        raw = c["raw_cost"]
        gov = governed_cost(tc)
        task_types.append({
            "task_type": tc,
            "raw_cost_usd": _round(raw, 5),
            "governed_cost_usd": _round(gov, 5),
            "cost_ratio_raw": _round(raw / base_raw, 1),
            "cost_ratio_governed": _round(gov / base_gov, 1),
            "measured_token_usd": _round(c["llm"], 5),
            "estimated_tool_usd": _round(c["tool"], 5),
            "tool_share": _round(c["tool"] / raw, 2) if raw else 0.0,
        })
    task_types.sort(key=lambda t: t["cost_ratio_raw"], reverse=True)

    # --- credit_value: volume-weighted avg cost per ticket (global anchor,
    # over ALL tickets incl. unknown), raw and governed bases ---
    total_n = sum(c["n"] for c in task_cost.values()) or 1
    cv_raw = sum(c["raw_cost"] * c["n"] for c in task_cost.values()) / total_n
    cv_gov = sum(governed_cost(tc) * c["n"] for tc, c in task_cost.items()) / total_n

    # --- available_policies[] ---
    available_policies = [{
        "policy_id": p["policy_id"],
        "affects_task_types": [t for t in p.get("affects", []) if t != _NON_TASK],
        "projected_saving_per_ticket_usd": _round(p["projected_saving_per_ticket_usd"], 5),
    } for p in policies]
    pol_by_id = {p["policy_id"]: p for p in policies}

    # --- segments[] ---
    seg_out = []
    for s in segments:
        mix = s.usage_mix
        raw_blended = sum(w * task_cost.get(tc, {}).get("raw_cost", 0.0) for tc, w in mix.items())
        gov_blended = sum(
            w * max(task_cost.get(tc, {}).get("raw_cost", 0.0)
                    - sum(pol_by_id[pid]["per_task_saving"].get(tc, 0.0)
                          for pid in s.active_policy_ids if pid in pol_by_id and tc in pol_by_id[pid].get("affects", [])),
                    0.0)
            for tc, w in mix.items()
        )
        raw_margin = _margin(s.price_per_ticket_usd, raw_blended)
        gov_margin = _margin(s.price_per_ticket_usd, gov_blended)

        options = []
        # One option per available policy not yet on for this segment that
        # touches a task this segment actually uses.
        for p in policies:
            pid = p["policy_id"]
            if pid in s.active_policy_ids:
                continue
            touched = [tc for tc in p.get("affects", []) if tc in mix]
            if not touched:
                continue
            delta = sum(mix[tc] * p["per_task_saving"].get(tc, 0.0) for tc in touched)
            after = _margin(s.price_per_ticket_usd, max(gov_blended - delta, 0.0))
            options.append({
                "lever": "policy",
                "policy_id": pid,
                "projected_margin_after": _round(after, 2),
                "bill_change_pct": 0.0,
                "lever_recovers_pts": round((after - gov_margin) * 100),
            })
        # Reprice to hit target on current (governed-for-segment) cost.
        price_needed = gov_blended / (1.0 - target_margin) if target_margin < 1 else s.price_per_ticket_usd
        options.append({
            "lever": "reprice",
            "projected_margin_after": _round(target_margin, 2),
            "bill_change_pct": _round(price_needed / s.price_per_ticket_usd - 1.0, 2) if s.price_per_ticket_usd else 0.0,
            "lever_recovers_pts": round((target_margin - gov_margin) * 100),
        })

        seg_out.append({
            "segment_id": s.segment_id,
            "synthetic": s.synthetic,
            "price_per_ticket_usd": _round(s.price_per_ticket_usd, 5),
            "usage_mix": {tc: _round(w, 2) for tc, w in mix.items()},
            "raw_margin": _round(raw_margin, 2),
            "governed_margin": _round(gov_margin, 2),
            "governance_recovered_pts": round((gov_margin - raw_margin) * 100),
            "near_renewal": s.near_renewal,
            "options": options,
        })

    return {
        "pricing_model": "credit",
        "target_margin": _round(target_margin, 2),
        "monthly_volume": int(monthly_volume),
        "baseline_task": baseline_task,
        "tiering_cost_basis": tiering_cost_basis,
        "credit_value_usd_raw_cost": _round(cv_raw, 5),
        "credit_value_usd_governed_cost": _round(cv_gov, 5),
        "task_types": task_types,
        "available_policies": available_policies,
        "segments": seg_out,
    }


# --- adapter: dashboard live_state + recs + tool rates -> builder inputs ---

def _policies_from_recs(recs: list[dict], rates: dict, task_cost: dict[str, dict]) -> list[dict]:
    """Normalise recommendations into policy savings, applying the live tool
    rates exactly as the cost report does (tool half of a cache saving scales
    with the operator's rate; the LLM half does not).

    A policy's per-task saving is CLAMPED to what that task can actually give
    up: routing only saves the LLM bill (cap at the task's measured token
    cost); a cache only removes tool cost (cap at the task's raw cost as a
    safety floor). Without this, a group-average routing saving can exceed a
    cheap task's own cost and drive its governed cost — and every governed
    ratio off it — to nonsense."""
    import json

    out = []
    for r in recs:
        try:
            i = json.loads(r.get("data") or "{}")
        except Exception:
            continue
        kind = i.get("kind")
        if kind == "tool_cache" and i.get("primary_tool") and i.get("task_class"):
            tc = i["task_class"]
            comp = i.get("components") or {}
            removed = comp.get("avg_tool_calls_removed", 0) or 0
            tool_part = removed * rates.get(i["primary_tool"], 0.0)
            llm_part = (i.get("savings_per_ticket_usd", 0) or 0) - (comp.get("tool_savings_per_ticket_usd", 0) or 0)
            spt = min(tool_part + llm_part, task_cost.get(tc, {}).get("raw_cost", float("inf")))
            out.append({
                "policy_id": f"cache_tool:{i['primary_tool']}",
                "affects": [tc],
                "per_task_saving": {tc: spt},
                "projected_saving_per_ticket_usd": spt,
            })
        elif kind == "model_routing":
            classes = list(i.get("classes") or [])
            spt = i.get("savings_per_ticket_usd", 0) or 0
            # Routing saves LLM only — cap per task at its measured token cost.
            per_task = {c: min(spt, task_cost.get(c, {}).get("llm", float("inf"))) for c in classes}
            out.append({
                "policy_id": "route_model:simple",
                "affects": classes,
                "per_task_saving": per_task,
                "projected_saving_per_ticket_usd": spt,
            })
    return out


def from_live_state(
    live: dict,
    recs: list[dict],
    rates: dict,
    provider: SegmentProvider,
    *,
    target_margin: float = DEFAULT_TARGET_MARGIN,
    monthly_volume: int,
    baseline_task: str,
    tiering_cost_basis: str = "raw",
) -> dict:
    """Build INPUT from the dashboard's shared cost vector."""
    by = live.get("by_task_class") or {}
    task_cost = {}
    for tc, s in by.items():
        counts = s.get("avg_tool_counts") or {}
        tool = sum((counts.get(t, 0) or 0) * rate for t, rate in rates.items())
        llm = s.get("avg_llm_cost_usd", 0) or 0
        task_cost[tc] = {"n": s.get("n", 0), "llm": llm, "tool": tool, "raw_cost": llm + tool}
    policies = _policies_from_recs(recs, rates, task_cost)
    return build_margin_input(
        task_cost, policies, provider.segments(),
        target_margin=target_margin, monthly_volume=monthly_volume,
        baseline_task=baseline_task, tiering_cost_basis=tiering_cost_basis,
    )
