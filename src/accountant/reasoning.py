"""Tier 3 Gemini reasoning.

Turns templated recommendations into individually-authored ones — but
only when the anomaly picture actually changes. Gemini is slow and
costs money, so it never runs on a timer or per-trace. It fires only
on a state transition:

  - a new anomaly signature appears, or
  - an existing anomaly's magnitude crosses a meaningful threshold.

(An anomaly that disappears is just superseded by the templated layer;
no Gemini call needed.)

The reasoned recommendation supersedes the templated card via an upsert
at the same `signature` with `source='gemini'`. The dashboard already
renders a 🤖 reasoned badge for those, so no UI change is required.

Reasoning is scheduled as a non-blocking asyncio task; the genai call
runs in a thread so it never stalls the ingest worker's event loop.
"""

import asyncio
import json
import logging
import os

from google import genai
from google.genai import types

from accountant.db import get_meta, set_meta, upsert_recommendation
from accountant.detection import anomaly_signature


log = logging.getLogger(__name__)

REASONING_MODEL = os.environ.get("ACCOUNTANT_REASONING_MODEL", "gemini-2.5-pro")

# Re-reason an existing anomaly only if its magnitude grew or shrank by
# at least this factor since the last time Gemini looked at it — avoids
# burning calls on minor jitter.
MAGNITUDE_REFIRE_RATIO = 1.5

# Signatures currently being reasoned about — prevents duplicate
# concurrent Gemini calls for the same anomaly. Module-level: the
# worker and the backfill share one process.
_in_flight: set[str] = set()
_client: genai.Client | None = None


# The observed agent's tunable configuration, handed to Gemini so its
# recommendations target real levers rather than abstract advice.
OBSERVED_AGENT_CONTEXT = """\
The observed agent is "Helpdesk Co-Pilot", a customer-support agent for a
SaaS product. Its entire behavior is governed by ONE instruction string
(a system prompt) that the operator can edit. Its tools: task_classifier,
kb_lookup (internal knowledge base), web_search (external/open web),
customer_lookup, refund_api, ticket_update, escalate_human.

Levers the operator can actually pull:
- Edit the instruction text — relax or remove a rule that forces
  redundant tool calls; reorder or condition workflow steps.
- The refund workflow currently MANDATES exactly three web_search calls
  per refund ticket (covering FTC regs, SaaS norms, competitor policies)
  before doing anything else. web_search hits the open web and is the
  most expensive tool; kb_lookup of /policies/refunds already covers the
  refund policy. This mandatory-3×-web_search rule is the single biggest
  avoidable cost driver.
"""


def _genai_client() -> genai.Client:
    global _client
    if _client is None:
        # Picks up Vertex config (GOOGLE_GENAI_USE_VERTEXAI, project,
        # location) from the environment, same as the observed agent.
        _client = genai.Client()
    return _client


def _magnitude(a: dict) -> float:
    if a["type"] == "class_cost_uplift":
        return float(a.get("uplift_x") or 0)
    if a["type"] == "repeated_tool":
        return float(a.get("hit_rate") or 0)
    return 0.0


def _load_reasoned() -> dict:
    raw = get_meta("reasoned_signatures")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _mark_reasoned(sig: str, magnitude: float) -> None:
    reasoned = _load_reasoned()
    reasoned[sig] = magnitude
    set_meta("reasoned_signatures", json.dumps(reasoned))


def _anomaly_changed(sig: str, magnitude: float, reasoned: dict) -> bool:
    """True if this anomaly is new, or its magnitude crossed the refire
    threshold relative to the last time Gemini reasoned about it."""
    if sig not in reasoned:
        return True
    prev = reasoned[sig]
    if prev <= 0:
        return magnitude > 0
    ratio = magnitude / prev
    return ratio >= MAGNITUDE_REFIRE_RATIO or ratio <= (1 / MAGNITUDE_REFIRE_RATIO)


def _build_prompt(a: dict) -> str:
    return f"""You are a cost-optimization analyst for AI agents. An \
automated detector flagged a cost anomaly in the observed agent's \
behavior. Write one concise, actionable recommendation for the operator.

{OBSERVED_AGENT_CONTEXT}

Detected anomaly (raw detector output):
{json.dumps(a, indent=2)}

Write:
- "title": a single line (<= 90 chars) naming the problem and its cost impact.
- "description": 2-3 sentences. State the likely ROOT CAUSE in terms of the
  agent's instruction/workflow, then the SPECIFIC change the operator should
  make to a named lever. Be concrete and practical — name the rule or tool,
  not generic advice. Quote the relevant numbers from the anomaly."""


def _reason_sync(a: dict) -> dict | None:
    """Blocking Gemini call. Returns {"title", "description"} or None."""
    try:
        client = _genai_client()
        resp = client.models.generate_content(
            model=REASONING_MODEL,
            contents=_build_prompt(a),
            config=types.GenerateContentConfig(
                temperature=0.3,
                response_mime_type="application/json",
                response_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["title", "description"],
                },
            ),
        )
        text = (resp.text or "").strip()
        if not text:
            return None
        obj = json.loads(text)
        title = (obj.get("title") or "").strip()
        description = (obj.get("description") or "").strip()
        if not title or not description:
            return None
        return {"title": title[:200], "description": description}
    except Exception:
        log.exception("Gemini reasoning call failed for %s", anomaly_signature(a))
        return None


async def _reason_and_store(a: dict) -> None:
    sig = anomaly_signature(a)
    try:
        result = await asyncio.to_thread(_reason_sync, a)
        if not result:
            return
        upsert_recommendation({
            "signature": sig,
            "source": "gemini",
            "task_class": a.get("task_class"),
            "anomaly_type": a.get("type"),
            "title": result["title"],
            "description": result["description"],
            "data": json.dumps(a),
        })
        _mark_reasoned(sig, _magnitude(a))
        log.info("Gemini reasoned about %s", sig)
    finally:
        _in_flight.discard(sig)


def schedule_if_changed(anomalies: list[dict]) -> None:
    """Schedule Gemini reasoning for any anomaly that is new or has
    materially changed magnitude since the last reasoning pass.

    Non-blocking: spawns asyncio tasks. Skips entirely while a bulk
    backfill is in progress (templated cards carry the load during
    import; reasoning runs once at completion and during live ops).
    """
    raw = get_meta("live_state")
    if raw:
        try:
            if json.loads(raw).get("ingest", {}).get("status") == "in_progress":
                return
        except Exception:
            pass

    reasoned = _load_reasoned()
    for a in anomalies or []:
        sig = anomaly_signature(a)
        if sig in _in_flight:
            continue
        if _anomaly_changed(sig, _magnitude(a), reasoned):
            _in_flight.add(sig)
            try:
                asyncio.create_task(_reason_and_store(a))
            except RuntimeError:
                # No running loop (e.g. called from a sync context) —
                # drop the in-flight marker so a later call retries.
                _in_flight.discard(sig)
                log.warning("no running loop; skipped reasoning for %s", sig)
