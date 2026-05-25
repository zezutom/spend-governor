"""Tier 2 templated recommendations.

One recommendation per detected *issue* (already deduped + costed by
savings.build_issues), keyed by the issue signature. Cheap, instant,
deterministic — covers the common cases the moment an issue is
detected. Gemini reasoning (Tier 3) supersedes these in place with
tailored guidance and the ready-to-apply instruction fix.

The savings figures live in the recommendation's `data` JSON; the
dashboard reads them to render the headline (% per ticket + $/mo).
"""

import json

from accountant.db import (
    connect,
    supersede_recommendations,
    upsert_recommendation,
)


def _human_class(tc: str) -> str:
    return tc.replace("_", " ")


def _template_for_issue(issue: dict) -> dict:
    pct = int(round(issue.get("pct_reduction", 0) * 100))

    if issue.get("kind") == "model_routing":
        title = f"Run simple tickets on a cheaper model — {pct}% less per ticket"
        description = (
            f"{issue['n_traces']} simple tickets (password resets, account "
            f"questions) run on the standard model. Routing them to "
            f"{issue.get('cheap_model')} saves ~${issue['savings_per_ticket_usd']:.4f} "
            f"per ticket — about ${issue['monthly_savings_usd']:.2f}/month at "
            f"current volume."
        )
        return {"title": title, "description": description}

    tc = _human_class(issue["task_class"])
    tool = issue.get("primary_tool")
    if issue.get("actionable") and tool:
        title = f"{tc} costs {pct}% more than it should, per ticket"
        description = (
            f"Every {tc} ticket runs ~{issue['avg_repeats']:.0f} redundant "
            f"{tool} calls. Caching them at the gateway saves about "
            f"${issue['savings_per_ticket_usd']:.4f} per ticket — roughly "
            f"${issue['monthly_savings_usd']:.2f}/month at current volume."
        )
    else:
        title = f"{tc} cost is elevated"
        description = (
            f"{tc} averages ${issue['current_avg_usd']:.5f} per ticket "
            f"across {issue['n_traces']} tickets. Investigating the driver."
        )
    return {"title": title, "description": description}


def generate_templated_recommendations(state: dict) -> set[str]:
    """Write one recommendation row per issue. Returns the set of active
    signatures so the caller can supersede anything no longer detected."""
    active: set[str] = set()
    with connect() as c:
        c.execute("BEGIN")
        for issue in state.get("issues", []):
            tpl = _template_for_issue(issue)
            sig = issue["signature"]
            active.add(sig)
            upsert_recommendation({
                "signature": sig,
                "source": "templated",
                "task_class": issue.get("task_class"),
                "anomaly_type": "issue",
                "title": tpl["title"],
                "description": tpl["description"],
                "data": json.dumps(issue),
            }, conn=c)
        supersede_recommendations(active, conn=c)
        c.execute("COMMIT")
    return active
