"""The autonomous Accountant agent — it reasons over real cost and decides.

No pre-canned text: a Gemini call reads the live workflow state (per-class cost,
the waste patterns, the levers available with their measured savings, what is
already governing, and any operator corrections) and returns its own
observations + an ordered plan of actions. The agent acts on its own; the
operator does not approve its thinking — they CORRECT its actions (veto a
lever), and the veto flows back here so the agent re-reasons and re-plans.

Honesty contract (enforced, not hoped for):
- The agent emits NO numbers. It reasons about patterns and priority; the UI
  renders the authoritative measured figures from the service layer beside its
  words, so a dollar amount can never be fabricated in prose.
- It may only plan levers from `available_levers` (real, enactable, not vetoed);
  any other choice is dropped here before it can act. It never invents a lever.
- It holds at the quality floor instead of inventing another cut.
"""

import json
import os
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel

from accountant import service


DEFAULT_MODEL = os.environ.get("ACCOUNTANT_AGENT_MODEL", "gemini-2.5-flash")
THINKING_BUDGET = int(os.environ.get("ACCOUNTANT_AGENT_THINKING_BUDGET", "512"))

_INSTRUCTION = """\
You are the autonomous Accountant: an agent that governs ANOTHER agent's runtime
cost. You watch its live traffic and the cost it is burning, and you decide — on
your own — what to do about it. The operator does not approve your reasoning;
they may CORRECT you by vetoing a lever, and you must respect that.

You are given, as JSON: the task classes with their cost pattern (which class
burns, and why — e.g. repeats a tool, runs on a premium model), the levers
available to you right now (each a real runtime control with a measured saving
and the class it targets), what is already governing, and the operator's vetoes.

Decide the order to enact the available levers to cut the most waste first, then
hold. Rules:
- ONLY plan levers listed in `available_now`. Never invent a lever or an action.
- Respect `vetoed`: never plan a vetoed lever. If the operator vetoed your best
  move, adapt — plan the next-best instead and say so.
- Order by impact: the biggest measured saving first (the figures are given to
  you for ranking).
- Emit NO numbers — no dollars, no percentages. Explain the PATTERN and the
  PRIORITY in words; the interface shows the measured figures itself.
- When nothing safe remains (all good levers governing or vetoed), stop and say
  you are holding at the quality floor — do not invent another cut.

Output your observations (what you notice in the data), the ordered plan (the
levers you will enact, each with a one-line reason), and your holding note.
Plain, declarative, engineering register. No marketing words.\
"""


class PlanStep(BaseModel):
    lever: str          # a signature from available_now
    reason: str         # one-line judgment, NO numbers


class AgentDecision(BaseModel):
    observations: list[str]
    plan: list[PlanStep]
    holding: str


_client: genai.Client | None = None


def _genai() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def observe() -> dict:
    """The real workflow state the agent reasons over — all from the service
    layer. Savings are included so the agent can RANK; it must not echo them."""
    live = service.live_state()
    recs = service.recommendations()
    rates = service.default_tool_rates()
    rows, totals = service.cost_breakdown(live, recs, rates)
    mt = service.default_monthly_volume(live, recs)
    reasons = service.class_reasons(recs)
    classes = [{
        "task_class": r["tc"], "cost_per_ticket": round(r["cost"], 6),
        "x_baseline": round(r["mult"], 1), "share_of_spend": round(r["share"], 3),
        "pattern": reasons.get(r["tc"], ""),
    } for r in rows if not r["is_base"] and r["tc"] != "unknown"]
    levers = [{
        "signature": l["signature"], "title": l["title"], "targets": l["classes"],
        "monthly_saving_usd": round(service.policy_monthly_saving(l["issue"], rates, mt, totals["total_n"]), 2),
        "active": l["active"],
    } for l in service.levers() if l["enactable"]]
    return {"classes": classes, "levers": levers,
            "baseline_class": service.BASELINE_CLASS}


def decide(vetoed: Optional[list[str]] = None, *, model: str = DEFAULT_MODEL) -> AgentDecision:
    """One autonomous reasoning cycle. Returns the agent's observations + an
    ordered plan of real levers to enact (vetoes respected), validated so the
    agent can only ever act on levers that actually exist and aren't vetoed."""
    vetoed = list(vetoed or [])
    state = observe()
    available_now = [l["signature"] for l in state["levers"]
                     if not l["active"] and l["signature"] not in vetoed]
    payload = {**state, "vetoed": vetoed, "available_now": available_now,
               "already_governing": [l["signature"] for l in state["levers"] if l["active"]]}

    resp = _genai().models.generate_content(
        model=model,
        contents="WORKFLOW STATE:\n" + json.dumps(payload, indent=2),
        config=types.GenerateContentConfig(
            system_instruction=_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=AgentDecision,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
        ),
    )
    dec = resp.parsed if isinstance(resp.parsed, AgentDecision) else \
        AgentDecision.model_validate_json(resp.text)
    # Honesty guard: drop any planned lever that isn't currently available.
    allowed = set(available_now)
    dec.plan = [s for s in dec.plan if s.lever in allowed]
    return dec
