"""Tier 2 templated recommendations.

Maps detected anomaly patterns to boilerplate recommendation text.
Cheap, instant, deterministic, covers ~80% of cases that the
detection layer recognizes. Gemini reasoning (Tier 3, separate
module) supersedes these when it has something more specific to say.
"""

import json

from accountant.db import (
    connect,
    supersede_recommendations,
    upsert_recommendation,
)
from accountant.detection import anomaly_signature


def _template_for(a: dict) -> dict | None:
    """Return {title, description} for a known anomaly pattern, or None."""
    if a["type"] == "class_cost_uplift":
        return {
            "title": (
                f"{a['task_class']} costs {a['uplift_x']}× the "
                f"{a['baseline_class']} baseline"
            ),
            "description": (
                f"Average cost per {a['task_class']} trace is "
                f"${a['avg_cost_usd']:.5f} across {a['n_traces']} traces, vs. "
                f"${a['baseline_cost_usd']:.5f} for {a['baseline_class']}. "
                f"Investigate why this task class is so much more expensive."
            ),
        }

    if a["type"] == "repeated_tool":
        rate_pct = int(round(a["hit_rate"] * 100))
        return {
            "title": (
                f"{a['tool']} called {a['repeat_threshold']}+ times per "
                f"{a['task_class']} ticket ({rate_pct}% of cases)"
            ),
            "description": _repeated_tool_advice(a),
        }
    return None


def _repeated_tool_advice(a: dict) -> str:
    """Specific guidance for known tool/class repeat patterns."""
    tc = a["task_class"]
    tool = a["tool"]
    base = (
        f"{a['traces_with_repeat']} of {a['of_total_in_class']} {tc} "
        f"traces invoke {tool} {a['repeat_threshold']} or more times. "
    )
    if tool == "web_search" and tc == "refund_handling":
        return base + (
            "Likely fix: remove the mandatory web_search-3× rule from the "
            "agent instruction (refund policy should resolve via kb_lookup "
            "of /policies/refunds + customer_lookup, not via repeated open-"
            "web search). Alternatively, cache web_search results so "
            "redundant calls become free."
        )
    if tool == "kb_lookup" and tc == "account_question":
        return base + (
            "Likely fix: tighten the agent instruction so a single, "
            "well-formed kb_lookup query is preferred over scattershot "
            "multi-article retrieval. Consider letting the agent batch "
            "candidate paths in one call."
        )
    return base + (
        f"Investigate whether the {tool} calls are redundant; consider "
        "caching, batching, or restructuring the agent instruction to "
        "avoid the repetition."
    )


def generate_templated_recommendations(state: dict) -> set[str]:
    """Write a recommendation row for each detected anomaly.

    Returns the set of active signatures so the caller can supersede
    anything no longer detected.
    """
    active: set[str] = set()
    with connect() as c:
        c.execute("BEGIN")
        for a in state.get("anomalies", []):
            tpl = _template_for(a)
            if tpl is None:
                continue
            sig = anomaly_signature(a)
            active.add(sig)
            upsert_recommendation({
                "signature": sig,
                "source": "templated",
                "task_class": a.get("task_class"),
                "anomaly_type": a.get("type"),
                "title": tpl["title"],
                "description": tpl["description"],
                "data": json.dumps(a),
            }, conn=c)
        supersede_recommendations(active, conn=c)
        c.execute("COMMIT")
    return active
