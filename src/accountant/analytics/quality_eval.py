"""The accelerated quality eval — the demo's load-bearing new build.

The cost side of governance is already proven from traces (caching, routing →
fewer dollars). The open question a cheaper-model decision raises is QUALITY:
does routing to the economy model change the answer? This module answers it for
real — it replays real support tickets through the baseline model and the
economy model and has an LLM-judge score the answers — and surfaces the signals
the agent renders a verdict on (judge-score drift, clarification rate,
refusal/escalation rate, answer-equivalence rate).

Two honest design choices make it fast enough to feel live in the demo:
- **Lighter replay.** It calls the model directly with the observed agent's
  own instruction, not the full ~30s ADK tool loop. This is a real model-quality
  comparison on the real ticket — exactly what the route_model decision turns on.
- **The clock is the only artifice.** The scoring is real, on really-replayed
  traffic; only the wall-clock is compressed, and that is disclosed.

The VERDICT (hold vs revert) is the agent's judgment over these real signals —
never a scripted "quality: good." Phoenix surfaces the signal; the agent renders
the verdict.
"""

import concurrent.futures as cf
import threading
import time

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

BASELINE_MODEL = "gemini-2.5-flash"
ECONOMY_MODEL = "gemini-2.5-flash-lite"
JUDGE_MODEL = "gemini-2.5-flash"

# Thread-local clients: the genai client isn't safe to share across the worker
# threads that replay tickets concurrently (a shared client gets its httpx
# transport closed under us). One client per thread sidesteps that.
_local = threading.local()


def _genai() -> genai.Client:
    c = getattr(_local, "client", None)
    if c is None:
        c = _local.client = genai.Client()
    return c


def _instruction() -> str:
    from observed.config import load_instruction
    return load_instruction()


def _tools() -> list:
    """The real observed-agent tools, handed to genai's automatic function
    calling so the replay runs the agent's actual tool loop (classify → look up
    → resolve) — a faithful replay, just without the ADK runtime's overhead."""
    from observed import tools as T
    return [T.task_classifier, T.kb_lookup, T.web_search, T.customer_lookup,
            T.refund_api, T.ticket_update, T.escalate_human]


# --- replay: the agent's real answer to a ticket, on a chosen model ---------
def _replay(ticket: str, model: str) -> str:
    resp = _genai().models.generate_content(
        model=model,
        contents=ticket,
        config=types.GenerateContentConfig(
            system_instruction=_instruction(),
            temperature=0.0,
            tools=_tools(),
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return (resp.text or "").strip() or "(no answer)"


# --- judge: score the pair on the dimensions the decision turns on ----------
class _Verdict(BaseModel):
    equivalent: bool = Field(description="Do both answers give the same actionable resolution to the ticket?")
    baseline_quality: int = Field(description="Baseline answer quality, 1 (poor) to 5 (excellent).")
    economy_quality: int = Field(description="Economy answer quality, 1 (poor) to 5 (excellent).")
    economy_asked_clarification: bool = Field(description="Does the economy answer ask the user a clarifying question instead of resolving?")
    economy_refused_or_escalated: bool = Field(description="Does the economy answer refuse, defer, or escalate instead of resolving?")


_JUDGE_SYSTEM = (
    "You are a QA judge for a customer-support agent. You are given one support "
    "ticket and two candidate answers — BASELINE (a stronger model) and ECONOMY "
    "(a cheaper model). Judge whether the ECONOMY answer serves the customer as "
    "well as the BASELINE.\n"
    "Score resolution-APPROPRIATENESS, not heroics: for a vague request, clear "
    "self-service guidance (e.g. how to reset a password) is a good answer and "
    "scores high (4-5). For a request with all the details needed to act, "
    "actually completing the action is the good answer; asking for information "
    "the agent already has, or that it could look up, is a quality DROP.\n"
    "Set economy_asked_clarification true only when the economy answer asks the "
    "customer for more information instead of resolving, AND the baseline did "
    "not need to. Set equivalent true when both answers give the customer the "
    "same effective resolution, even if worded differently. Return the verdict."
)


def _judge(ticket: str, baseline: str, economy: str) -> _Verdict:
    payload = (f"TICKET:\n{ticket}\n\nBASELINE ANSWER:\n{baseline}\n\n"
               f"ECONOMY ANSWER:\n{economy}")
    resp = _genai().models.generate_content(
        model=JUDGE_MODEL,
        contents=payload,
        config=types.GenerateContentConfig(
            system_instruction=_JUDGE_SYSTEM,
            response_mime_type="application/json",
            response_schema=_Verdict,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return resp.parsed if isinstance(resp.parsed, _Verdict) else _Verdict.model_validate_json(resp.text)


def _traced_replay(ticket: str, model: str, gid: str | None):
    """Replay inside a span so the call lands as a real, inspectable Phoenix
    trace (the google-genai instrumentor fills in model/tokens/cost). Returns
    the answer and a Phoenix Cloud deep-link to that trace."""
    from opentelemetry import trace as _ot
    from accountant.pipeline import phoenix_cost
    tracer = _ot.get_tracer("accountant.quality_eval")
    with tracer.start_as_current_span(f"eval.replay.{model}") as span:
        ans = _replay(ticket, model)
        tid = format(span.get_span_context().trace_id, "032x")
    url = phoenix_cost.span_deeplink(gid, tid, None) if gid else None
    return ans, url


def _eval_ticket(ticket: str, baseline_model: str, economy_model: str,
                 gid: str | None = None) -> dict:
    if gid is not None:
        base, _ = _traced_replay(ticket, baseline_model, gid)
        econ, econ_url = _traced_replay(ticket, economy_model, gid)
    else:
        base, econ, econ_url = _replay(ticket, baseline_model), _replay(ticket, economy_model), None
    v = _judge(ticket, base, econ)
    return {
        "ticket": ticket,
        "equivalent": v.equivalent,
        "baseline_quality": v.baseline_quality,
        "economy_quality": v.economy_quality,
        "clarified": v.economy_asked_clarification,
        "refused_escalated": v.economy_refused_or_escalated,
        "phoenix_url": econ_url,  # the economy trace, inspectable in Phoenix
    }


# --- the eval: aggregate the signals + the agent's verdict ------------------
def run_quality_eval(tickets: list[str], *, baseline_model: str = BASELINE_MODEL,
                     economy_model: str = ECONOMY_MODEL, max_workers: int = 4,
                     trace: bool = False) -> dict:
    """Replay every ticket through both models, judge each, aggregate the signals,
    and render the agent's verdict (hold vs revert). Returns real numbers + the
    wall-clock so the caller can decide live-vs-prerun. With trace=True, each
    replay lands as a real Phoenix trace and rows carry a Phoenix deep-link."""
    gid = None
    if trace:
        from observed.telemetry import init_telemetry
        from accountant.pipeline import phoenix_cost
        init_telemetry()
        gid = phoenix_cost.project_gid()
    t0 = time.monotonic()
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_eval_ticket, t, baseline_model, economy_model, gid) for t in tickets]
        for f in cf.as_completed(futs):
            rows.append(f.result())
    n = len(rows) or 1
    equivalent_rate = sum(r["equivalent"] for r in rows) / n
    mean_base = sum(r["baseline_quality"] for r in rows) / n
    mean_econ = sum(r["economy_quality"] for r in rows) / n
    clar_rate = sum(r["clarified"] for r in rows) / n
    refusal_rate = sum(r["refused_escalated"] for r in rows) / n
    drift = round(mean_base - mean_econ, 2)

    # The agent's verdict over the real signals. It trips only on MATERIAL
    # degradation — a clear judge-score drop, a wave of refusals/escalations, or
    # many new clarifying questions alongside any drift. Mild small-sample noise
    # (one divergent answer, drift ~0) holds. Tuned so a clean economy-on-simple
    # run holds and economy-on-complex (refunds) trips hard.
    trip = (drift >= 1.0) or (refusal_rate >= 0.5) or (clar_rate >= 0.5 and drift > 0.0) \
        or (equivalent_rate <= 0.34)
    return {
        "baseline_model": baseline_model,
        "economy_model": economy_model,
        "n": len(rows),
        "equivalent_rate": round(equivalent_rate, 3),
        "mean_quality_baseline": round(mean_base, 2),
        "mean_quality_economy": round(mean_econ, 2),
        "quality_drift": drift,
        "clarification_rate": round(clar_rate, 3),
        "refusal_escalation_rate": round(refusal_rate, 3),
        "verdict": "revert" if trip else "hold",
        "elapsed_sec": round(time.monotonic() - t0, 1),
        "project_gid": gid,
        "rows": rows,
    }


# --- persistence: pre-run the real eval, reveal it on the disclosed clock ---
# A literally-live full eval can't finish in ~10s (each replay is 5-15s). The
# honest demo design the spec frames ("10s ≈ ~N min of live traffic") is to run
# the REAL eval once, persist it here, and have the popup reveal the real,
# already-scored rows progressively on the disclosed accelerated clock.
import json
import pathlib

_CACHE_DIR = pathlib.Path(__file__).resolve().parents[3] / "data" / "evals"


def save_eval(key: str, result: dict) -> str:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps(result, indent=2))
    return str(path)


def load_eval(key: str) -> dict | None:
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def sample_tickets(classes: tuple[str, ...], per_class: int = 2) -> list[str]:
    """Real tickets from the observed pool, for a given set of task classes."""
    from observed.generate_dataset import MESSAGE_POOLS, CUSTOMER_POOL
    import itertools
    cust = itertools.cycle(CUSTOMER_POOL)
    out: list[str] = []
    for cls in classes:
        pool = MESSAGE_POOLS[cls]
        for i in range(per_class):
            out.append(pool[i % len(pool)].replace("{customer_id}", next(cust)))
    return out
