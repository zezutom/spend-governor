"""Trace-measured before/after verification.

The governor's intervention log says what it *thinks* it saved. This
module proves it from the customer's own traces: for the task types a
policy affects, it compares the actual average cost-per-ticket before
the policy was activated vs. after — where "after" traces already carry
the governor's effect (flash-lite model, $0 cached tool calls).

When the measured drop matches the governor's reported savings, the
number is trustworthy, not claimed.
"""

from datetime import datetime, timezone

from accountant.db import connect


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def measured_before_after(task_classes: list[str], since_iso: str | None) -> dict:
    """Average cost-per-ticket for the given task classes, split at the
    activation time. Returns before/after averages, counts, and the
    measured per-ticket and monthly savings."""
    since = _parse(since_iso)
    with connect() as c:
        rows = c.execute(
            """
            SELECT trace_id,
                   MIN(start_time) AS ts,
                   MAX(CASE WHEN tool_name='task_classifier'
                            THEN classifier_task_class END) AS tc,
                   COALESCE(SUM(llm_cost_usd + tool_cost_usd), 0) AS cost,
                   SUM(CASE WHEN span_kind='TOOL' THEN 1 ELSE 0 END) AS tool_spans
            FROM spans
            GROUP BY trace_id
            """
        ).fetchall()

    classes = set(task_classes)
    before, after = [], []
    for r in rows:
        if (r["tc"] or "unknown") not in classes:
            continue
        if (r["tool_spans"] or 0) < 2:  # complete tickets only
            continue
        ts = _parse(r["ts"])
        cost = float(r["cost"] or 0)
        if since is not None and ts is not None and ts >= since:
            after.append(cost)
        else:
            before.append(cost)

    before_avg = sum(before) / len(before) if before else 0.0
    after_avg = sum(after) / len(after) if after else 0.0
    per_ticket = max(before_avg - after_avg, 0.0)
    pct = (per_ticket / before_avg) if before_avg else 0.0

    return {
        "before_avg_usd": round(before_avg, 6),
        "after_avg_usd": round(after_avg, 6),
        "before_n": len(before),
        "after_n": len(after),
        "savings_per_ticket_usd": round(per_ticket, 6),
        "pct_reduction": round(pct, 3),
        "measured_savings_usd": round(per_ticket * len(after), 4),
        "has_after_data": len(after) > 0,
    }
