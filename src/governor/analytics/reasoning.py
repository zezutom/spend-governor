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

from governor.pipeline.db import get_meta, set_meta, upsert_recommendation


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


# Context for Gemini: the Accountant is a RUNTIME WRAPPER. It does not
# edit prompts or touch source — it enforces economic policy inline at a
# gateway the observed agent's tool/LLM traffic flows through. The
# operator activates a policy; the gateway enforces it in real time.
WRAPPER_CONTEXT = """\
You advise Agent Accountant, a runtime cost wrapper for AI agents. The
wrapper sits inline (an API gateway the agent's tool and model calls route through)
and enforces economic policies in real time — WITHOUT editing prompts or
accessing source. Available policy types:
- Semantic-cache a tool: serve a cached, semantically-equivalent result
  for a repeated external call (e.g. web_search) instead of re-executing
  it. Quality is preserved by an embedding-equivalence check.
- Route simple requests to a cheaper model tier.
- Cap redundant tool invocations.

The observed agent is a SaaS customer-support copilot. The detected
issue (below) already quantifies the projected savings. Your job is the
operator-facing rationale, not a code or prompt change.
"""


def _genai_client() -> genai.Client:
    global _client
    if _client is None:
        # Picks up Vertex config (GOOGLE_GENAI_USE_VERTEXAI, project,
        # location) from the environment, same as the observed agent.
        _client = genai.Client()
    return _client


def _magnitude(issue: dict) -> float:
    """Change-detection magnitude for an issue — its projected per-ticket
    savings. Re-reason when this moves materially."""
    return float(issue.get("savings_per_ticket_usd") or 0.0)


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


def _issue_changed(sig: str, magnitude: float, reasoned: dict) -> bool:
    """True if this issue is new, or its magnitude crossed the refire
    threshold relative to the last time Gemini reasoned about it."""
    if sig not in reasoned:
        return True
    prev = reasoned[sig]
    if prev <= 0:
        return magnitude > 0
    ratio = magnitude / prev
    return ratio >= MAGNITUDE_REFIRE_RATIO or ratio <= (1 / MAGNITUDE_REFIRE_RATIO)


def _build_prompt(issue: dict) -> str:
    return f"""You advise Agent Accountant, a runtime cost wrapper for AI agents. A \
detector flagged a costly execution pattern and quantified the savings \
of a runtime policy. Write the operator-facing rationale.

{WRAPPER_CONTEXT}

Detected issue (deduped, with projected savings):
{json.dumps(issue, indent=2)}

Return:
- "title": one line (<= 90 chars). Lead with the cost impact (percent
  saved per ticket). Decisive, not hedged.
- "description": 1-2 SHORT sentences. Name the wasteful pattern and the
  runtime policy that fixes it (semantic-cache the repeated tool, or
  route to the cheaper model). State that it needs no prompt or source
  change. No preamble — a busy CFO reads this."""


def _reason_sync(issue: dict) -> dict | None:
    """Blocking Gemini call. Returns {"title", "description"} or None."""
    try:
        client = _genai_client()
        resp = client.models.generate_content(
            model=REASONING_MODEL,
            contents=_build_prompt(issue),
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
        log.exception("Gemini reasoning call failed for %s", issue.get("signature"))
        return None


async def _reason_and_store(issue: dict) -> None:
    sig = issue["signature"]
    try:
        result = await asyncio.to_thread(_reason_sync, issue)
        if not result:
            return
        upsert_recommendation({
            "signature": sig,
            "source": "gemini",
            "task_class": issue.get("task_class"),
            "anomaly_type": issue.get("kind", "issue"),
            "title": result["title"],
            "description": result["description"],
            # Carry the full issue (incl. savings) so the dashboard
            # renders the same headline numbers on a reasoned card.
            "data": json.dumps(issue),
        })
        _mark_reasoned(sig, _magnitude(issue))
        log.info("Gemini reasoned about %s", sig)
    finally:
        _in_flight.discard(sig)


def schedule_if_changed(issues: list[dict]) -> None:
    """Schedule Gemini reasoning for any issue that is new or whose
    projected savings changed materially since the last pass.

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
    for issue in issues or []:
        # Only reason about actionable issues — ones where there's a fix
        # to propose and savings to quote.
        if not issue.get("actionable"):
            continue
        sig = issue["signature"]
        if sig in _in_flight:
            continue
        if _issue_changed(sig, _magnitude(issue), reasoned):
            _in_flight.add(sig)
            try:
                asyncio.create_task(_reason_and_store(issue))
            except RuntimeError:
                _in_flight.discard(sig)
                log.warning("no running loop; skipped reasoning for %s", sig)
