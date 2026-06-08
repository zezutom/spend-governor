"""Service layer — the one interface the control-plane UI consumes.

The cockpit reaches the backend ONLY through this module: state, economics,
governance levers, savings/before-after, and the Phoenix proof drill-down.
Every function either re-exports an existing backend entry point or aliases a
view-model function lifted VERBATIM from the v5 dashboard (`_viewmodel.py`), so
no number changes value and nothing is reimplemented. Rendering lives entirely
in the UI; this layer returns data only.

Grouped by the brief's surfaces:
- system state  — what's live right now
- economics     — per-class cost, projection, tool-rate config
- governance    — the enactable levers + their measured effect + live toggles
- savings       — realized savings, interventions, measured before/after
- proof         — trace race, per-trace tables, Phoenix deeplinks
"""

from governor.service import _viewmodel as _vm

# --- backend pass-throughs (reused, not reimplemented) ---------------------
from governor.analytics.verification import measured_before_after as before_after
from governor.pipeline.db import (
    savings_summary as realized_savings,
    class_cost_stats,
    class_trace_costs,
    policy_savings_series,
    policy_saving_spans,
    representative_saving_span,
)
from governor.pipeline.phoenix_cost import span_deeplink
from governor.pricing.tools import TOOL_PRICES
# "pair", not "fixture": it is a REAL captured baseline+governed trace pair.
# The word "fixture" invites swapping in fabricated data later; the honesty
# contract forbids that, and the name holds the line.
from governor.trace_race.fixture import load_fixture as captured_trace_pair
from governor.wrapper import store as _store

# --- system state ----------------------------------------------------------
live_state = _vm._load_live_state                 # the single live_state blob
cache_span_count = _vm._cache_span_count          # ingested span count
recommendations = _vm._load_recommendations       # active recommendation rows
BASELINE_CLASS = _vm.BASELINE_CLASS

# --- economics (per-class cost, projection, tool rates) --------------------
cost_breakdown = _vm._issue_rows                  # (live, recs, rates) -> (rows, totals)
default_tool_rates = _vm._default_tool_rates      # operator-set per-call rates
tool_cost = _vm._tool_cost                        # Σ(calls × rate) for one ticket
default_monthly_volume = _vm._default_monthly_tickets  # observed-rate projection base
observed_hours = _vm._observed_hours              # window the sample spans
class_reasons = _vm._class_reasons                # diagnosis text per task class

# --- governance (levers the optimizer enacts + their measured effect) ------
issue_of = _vm._issue_of                          # decode a recommendation's issue
policy_for_issue = _vm._policy_for_issue          # issue -> (sig, type, params) | None
affected_classes = _vm._affected_classes          # task classes a policy governs
policy_cause = _vm._story_cause                   # plain-English cause + fix
policy_per_ticket_saving = _vm._policy_per_ticket  # measured $/ticket a lever saves
policy_monthly_saving = _vm._policy_mo            # $/mo at a given volume
lever_text = _vm._fix_text                        # (title, one-line) for a lever

# --- enactable vs roadmap (the honesty contract, enforced at the seam) ------
# The ONLY policy types that physically exist and can be enforced today. The
# optimizer may *recommend* anything; it may only *enact* these.
ENACTABLE_POLICY_TYPES = frozenset(
    {"cache_tool", "route_model", "limit_tool_calls", "suppress_tool"})

# Capabilities the agent may recommend but must label not-yet-enforced. Surfaced
# explicitly here so the seam — not UI discipline — owns the real/roadmap split.
ROADMAP_CAPABILITIES = (
    {"id": "budget_strategy", "title": "Budget-strategy automation",
     "blurb": "Spend caps and budget-driven routing beyond the current levers.",
     "enactable": False},
    {"id": "autonomy", "title": "Higher autonomy (autopilot)",
     "blurb": "Auto-optimize beyond low-risk caching/routing without approval.",
     "enactable": False},
    {"id": "org_wide", "title": "Cross-agent / org-wide budgeting",
     "blurb": "Allocation and executive reporting across many agents.",
     "enactable": False},
)


def is_enactable(policy_type: str | None) -> bool:
    """True only for levers that really exist and can be enforced today."""
    return policy_type in ENACTABLE_POLICY_TYPES


# Answer-affecting policy types: routing a ticket to a different model can change
# the output, so the agent ESCALATES these for a human accept/reject instead of
# auto-applying. Caching serves a semantically-equivalent result, so it's safe to
# auto-apply. This is a real property of the lever, not a cosmetic flag.
ANSWER_AFFECTING_POLICY_TYPES = frozenset({"route_model", "suppress_tool"})


def is_safe(policy_type: str | None) -> bool:
    """Safe = the agent may auto-apply it (output unaffected). Risky =
    answer-affecting → the agent escalates it for a human decision."""
    return is_enactable(policy_type) and policy_type not in ANSWER_AFFECTING_POLICY_TYPES


def roadmap_capabilities() -> list[dict]:
    """Labeled not-yet-enforced capabilities — recommend-only, never live."""
    return [dict(c) for c in ROADMAP_CAPABILITIES]


# live toggle state — the real enactable levers
active_policies = _store.active_policies
is_active = _store.is_active
deactivate_policy = _store.deactivate_policy
policy_activated_at = _store.policy_activated_at


def activate_policy(signature: str, policy_type: str, params: dict):
    """Enact a lever. HARD GUARD: refuses anything not enactable, so the agent
    physically cannot enforce a roadmap capability it only recommended."""
    if not is_enactable(policy_type):
        raise ValueError(
            f"refusing to enact non-enactable policy type {policy_type!r} — "
            f"roadmap capabilities can be recommended, not enforced "
            f"(enactable: {sorted(ENACTABLE_POLICY_TYPES)})")
    return _store.activate_policy(signature, policy_type, params)

# --- proof (Phoenix-backed verification) -----------------------------------
project_gid = _vm._project_gid                    # for building span deeplinks


def policies_active_count() -> int:
    """How many levers are governing live right now."""
    return len(active_policies())


def levers() -> list[dict]:
    """The enactable levers, each with its measured effect and live state —
    derived from recommendations through the same view-model the dashboard used.
    Returns one dict per actionable recommendation."""
    out = []
    for rec in recommendations():
        issue = issue_of(rec)
        policy = policy_for_issue(issue)
        if not policy or (issue.get("savings_per_ticket_usd", 0) or 0) <= 0:
            continue
        sig = policy[0]
        title, blurb = lever_text(issue)
        out.append({
            "signature": sig,
            "policy_type": policy[1],
            "params": policy[2],
            "title": title,
            "blurb": blurb,
            "cause": policy_cause(issue),
            "classes": affected_classes(issue),
            "active": is_active(sig),
            "enactable": is_enactable(policy[1]),  # honesty contract, at the seam
            "safe": is_safe(policy[1]),            # safe → auto-apply; risky → escalate
            "issue": issue,
            "rec": rec,
        })
    return out


__all__ = [
    "live_state", "cache_span_count", "recommendations", "BASELINE_CLASS",
    "cost_breakdown", "default_tool_rates", "tool_cost", "default_monthly_volume",
    "observed_hours", "class_reasons",
    "issue_of", "policy_for_issue", "affected_classes", "policy_cause",
    "policy_per_ticket_saving", "policy_monthly_saving", "lever_text", "levers",
    "active_policies", "is_active", "activate_policy", "deactivate_policy",
    "policy_activated_at", "policies_active_count",
    "ENACTABLE_POLICY_TYPES", "is_enactable", "is_safe", "roadmap_capabilities",
    "realized_savings", "before_after", "policy_savings_series", "policy_saving_spans",
    "representative_saving_span",
    "captured_trace_pair", "class_cost_stats", "class_trace_costs", "project_gid",
    "span_deeplink", "TOOL_PRICES",
]
