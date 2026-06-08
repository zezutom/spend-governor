"""Curated governance intents — the load-bearing demo path.

Each intent is a DETERMINISTIC handler grounded entirely in the service layer:
every number it states is read or computed by the data layer from Phoenix, none
is produced by a model. This is the reliable spine the demo runs on; freeform
chat (the flourish) is a separate, LLM-driven surface that calls the same
service tools under the same honesty contract.

The loop each intent serves: intent → grounded reasoning → (optional) enacted
real policy → map focus + intervention card → proof reference. Handlers RETURN
structured results for the cockpit to render; they enact only when enact=True,
and only ever through service.activate_policy (which hard-refuses non-enactable
levers).
"""

from governor import service


def _ctx():
    live = service.live_state()
    recs = service.recommendations()
    rates = service.default_tool_rates()
    rows, totals = service.cost_breakdown(live, recs, rates)
    mt = service.default_monthly_volume(live, recs)
    return live, recs, rates, rows, totals, mt


def _name(tc: str) -> str:
    return tc.replace("_", " ")


def diagnose() -> dict:
    """'Why did our AI costs jump?' — name the culprit class from measured cost,
    with the pattern driving it. No enactment."""
    live, recs, rates, rows, totals, mt = _ctx()
    reasons = service.class_reasons(recs)
    cands = [r for r in rows if not r["is_base"] and r["tc"] != "unknown"]
    if not cands:
        return {"intent": "diagnose", "say": "No class is materially above baseline.",
                "map_focus": None, "numbers": {}, "action": None}
    culprit = max(cands, key=lambda r: r["cost"] * r["n"])
    tc = culprit["tc"]
    reason = reasons.get(tc) or "runs hotter than baseline"
    say = (f"Your most expensive class is **{_name(tc)}** at "
           f"${culprit['cost']:.4f}/ticket — {culprit['mult']:.1f}× the "
           f"{_name(service.BASELINE_CLASS)} baseline, and {culprit['share'] * 100:.0f}% "
           f"of spend. It {reason}")
    return {
        "intent": "diagnose",
        "title": "Why costs are high",
        "say": say,
        "map_focus": tc,
        "numbers": {"cost_per_ticket": culprit["cost"], "mult": culprit["mult"],
                    "share": culprit["share"], "llm": culprit["llm"], "tool": culprit["tool"]},
        "action": None,
        "proof_hint": "Ask 'prove it' to see the real before/after spans.",
    }


def _enactable_levers_for(tc: str | None) -> list[dict]:
    levers = [l for l in service.levers() if l["enactable"]]
    if tc:
        levers = [l for l in levers if tc in l["classes"]]
    return levers


def _project(levers, rates, mt, total_n) -> float:
    return sum(service.policy_monthly_saving(l["issue"], rates, mt, total_n) for l in levers)


def cut_spend(target_pct: float = 0.30, *, enact: bool = False) -> dict:
    """'Cut spend 30%' — pick the fewest enactable levers that reach the target,
    enact them on request. Reduction is computed by the data layer, labeled a
    projection."""
    live, recs, rates, rows, totals, mt = _ctx()
    monthly_spend = totals["cost_per_ticket"] * mt
    ranked = sorted(_enactable_levers_for(None),
                    key=lambda l: -service.policy_monthly_saving(l["issue"], rates, mt, totals["total_n"]))
    chosen, cum = [], 0.0
    for l in ranked:
        cum += service.policy_monthly_saving(l["issue"], rates, mt, totals["total_n"])
        chosen.append(l)
        if monthly_spend and cum / monthly_spend >= target_pct:
            break
    reduction = (cum / monthly_spend) if monthly_spend else 0.0
    enacted = []
    if enact:
        for l in chosen:
            service.activate_policy(l["signature"], l["policy_type"], l["params"])
            enacted.append(l["signature"])
    reached = reduction >= target_pct
    verb = "Activated" if enact else "Would activate"
    tail = "Governing live, reversible in one click." if enact else "Confirm to enact."
    say = (f"{verb} {len(chosen)} lever{'s' if len(chosen) != 1 else ''} — projected "
           f"**{reduction * 100:.0f}%** off AI spend "
           f"(${cum:,.0f}/mo of ${monthly_spend:,.0f}/mo at {mt:,} tickets/mo). "
           + ("" if reached else f"That is the most reachable; the {target_pct * 100:.0f}% "
              f"target needs levers that don't exist yet. ") + tail)
    return {
        "intent": "cut_spend", "title": f"Cut spend {target_pct * 100:.0f}%",
        "say": say, "enact": enact, "reached_target": reached,
        "levers": [l["signature"] for l in chosen], "enacted": enacted,
        "map_focus": [c for l in chosen for c in l["classes"]],
        "projection": {"monthly_spend_usd": round(monthly_spend, 2),
                       "monthly_saving_usd": round(cum, 2),
                       "reduction_pct": round(reduction, 4), "volume": mt,
                       "is_projection": True},
        "cards": [{"signature": l["signature"], "title": l["title"],
                   "per_ticket_usd": service.policy_per_ticket_saving(l["issue"], rates),
                   "monthly_usd": service.policy_monthly_saving(l["issue"], rates, mt, totals["total_n"]),
                   "active": l["active"] or l["signature"] in enacted} for l in chosen],
    }


def prevent(task_class: str | None = None, *, enact: bool = False) -> dict:
    """'Prevent this from happening again' — enact the real lever(s) for the
    culprit class (defaults to the diagnosed one)."""
    live, recs, rates, rows, totals, mt = _ctx()
    tc = task_class or (diagnose().get("map_focus"))
    levers = _enactable_levers_for(tc)
    if not levers:
        return {"intent": "prevent", "say": f"No enactable lever governs {_name(tc or 'that class')} yet.",
                "map_focus": tc, "levers": [], "enacted": [], "cards": []}
    enacted = []
    if enact:
        for l in levers:
            service.activate_policy(l["signature"], l["policy_type"], l["params"])
            enacted.append(l["signature"])
    per_ticket = sum(service.policy_per_ticket_saving(l["issue"], rates) for l in levers)
    monthly = _project(levers, rates, mt, totals["total_n"])
    head = levers[0]
    verb = "Created policy" if enact else "Proposed policy"
    say = (f"{verb}: {head['cause']} "
           f"Saves **${per_ticket:.4f}/ticket** (~${monthly:,.0f}/mo projected at {mt:,} tickets). "
           + ("Governing live, reversible in one click." if enact else "Confirm to enact."))
    return {
        "intent": "prevent", "title": "Prevent recurrence",
        "say": say, "enact": enact, "map_focus": tc,
        "levers": [l["signature"] for l in levers], "enacted": enacted,
        "cards": [{"signature": l["signature"], "title": l["title"],
                   "per_ticket_usd": service.policy_per_ticket_saving(l["issue"], rates),
                   "monthly_usd": service.policy_monthly_saving(l["issue"], rates, mt, totals["total_n"]),
                   "active": l["active"] or l["signature"] in enacted} for l in levers],
        "projection": {"per_ticket_usd": round(per_ticket, 6),
                       "monthly_usd": round(monthly, 2), "volume": mt, "is_projection": True},
    }


def prove() -> dict:
    """'Show me it's real' — the captured baseline-vs-governed pair + the live
    Phoenix Cloud deep-links. System behaviour only; no prompt/PII."""
    fx = service.captured_trace_pair()
    if not fx:
        return {"intent": "prove", "say": "No captured trace pair yet — run a seeded capture.",
                "proof": None}
    b, g = fx["baseline"], fx["governed"]
    answer = ("same final answer, verified" if fx.get("same_answer")
              else "outputs differ — preservation not claimed" if fx.get("seeded")
              else "representative pair (dev) — same-answer withheld")
    say = (f"Same ticket, two ways: baseline **${b['total_usd']:.4f}**, governed "
           f"**${g['total_usd']:.4f}**, {fx['skipped_calls']} paid call"
           f"{'s' if fx['skipped_calls'] != 1 else ''} skipped, saved "
           f"**${fx['saved_usd']:.4f}** — {answer}. Open the real spans in Phoenix.")
    return {"intent": "prove", "title": "Proof", "say": say, "proof": fx,
            "deeplinks": {"baseline": b.get("phoenix_url"), "governed": g.get("phoenix_url")}}


def forecast(volume: int | None = None) -> dict:
    """Current realized savings + a labeled month-end projection at a volume."""
    live, recs, rates, rows, totals, mt = _ctx()
    vol = volume or mt
    spend = totals["cost_per_ticket"] * vol
    recoverable = totals["recoverable_per_ticket"] * vol
    realized = service.realized_savings().get("total_savings_usd", 0) or 0
    say = (f"At {vol:,} tickets/mo: projected spend **${spend:,.0f}/mo**, of which "
           f"**${recoverable:,.0f}/mo** is avoidable with the real levers. "
           f"Measured savings so far: ${realized:.4f}. (Monthly figures are projections "
           f"at the volume you set.)")
    return {"intent": "forecast", "title": "Forecast", "say": say,
            "projection": {"monthly_spend_usd": round(spend, 2),
                           "monthly_recoverable_usd": round(recoverable, 2),
                           "realized_savings_usd": round(realized, 6),
                           "volume": vol, "is_projection": True}}


# The curated set the cockpit offers as buttons — the reliable spine.
CURATED = (
    {"id": "diagnose", "label": "Why did costs spike?", "handler": diagnose},
    {"id": "cut_spend", "label": "Cut spend 30%", "handler": lambda: cut_spend(0.30)},
    {"id": "prevent", "label": "Prevent this again", "handler": lambda: prevent()},
    {"id": "prove", "label": "Show me it's real", "handler": prove},
    {"id": "forecast", "label": "Forecast spend", "handler": forecast},
)
