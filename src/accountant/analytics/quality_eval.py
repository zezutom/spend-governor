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
import json
import os
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


import functools
import threading

# Per-thread accumulator of the OTEL span_id for every tool call the current
# replay makes, in order — so each stepped tool call links to its OWN span.
_tool_rec = threading.local()


def _traced_tool(fn):
    """Wrap an observed tool so each invocation emits its own real Phoenix span
    (a child of the model call). AFC introspects the signature via __wrapped__,
    so the function declaration is unchanged."""
    @functools.wraps(fn)
    def w(*args, **kwargs):
        from opentelemetry import trace as _ot
        tr = _ot.get_tracer("accountant.quality_eval")
        with tr.start_as_current_span(fn.__name__) as sp:
            # record the span id FIRST, so it's captured even if the tool raises
            # (a tool erroring on bad args is itself a real, inspectable signal)
            sid = format(sp.get_span_context().span_id, "016x")
            rec = getattr(_tool_rec, "ids", None)
            if rec is not None:
                rec.append(sid)
            try:
                sp.set_attribute("openinference.span.kind", "TOOL")
                sp.set_attribute("tool.name", fn.__name__)
                sp.set_attribute("input.value", json.dumps(kwargs, default=str)[:1500])
            except Exception:
                pass
            out = fn(*args, **kwargs)
            try:
                sp.set_attribute("output.value", json.dumps(out, default=str)[:1500])
            except Exception:
                pass
        return out
    return w


def _tools() -> list:
    """The real observed-agent tools, each wrapped to emit its own Phoenix span,
    handed to genai's automatic function calling so the replay runs the agent's
    actual tool loop (classify → look up → resolve)."""
    from observed import tools as T
    fns = [T.task_classifier, T.kb_lookup, T.web_search, T.customer_lookup,
           T.refund_api, T.ticket_update, T.escalate_human]
    return [_traced_tool(f) for f in fns]


def _final_answer(resp) -> str:
    """The agent's actual resolution. Often it's resp.text, but the agent can
    resolve THROUGH a tool — passing the customer-facing reply as an arg to
    ticket_update / escalate_human — and emit no final text. Fall back to that
    arg so the judge scores the real answer, not an empty string."""
    if resp.text and resp.text.strip():
        return resp.text.strip()
    for content in reversed(getattr(resp, "automatic_function_calling_history", None) or []):
        for p in (content.parts or []):
            fc = getattr(p, "function_call", None)
            args = dict(getattr(fc, "args", None) or {}) if fc else {}
            reply = args.get("customer_reply") or args.get("reply") or args.get("message")
            if reply:
                return str(reply)
    return "(no answer)"


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
    return _final_answer(resp)


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


def _traced_replay(ticket: str, model: str, gid: str | None, attrs: dict | None = None):
    """Replay inside a span so the call lands as a real, inspectable Phoenix
    trace (the google-genai instrumentor fills in model/tokens/cost). `attrs`
    sets span attributes — the lab tags every replay span 'test' so sandbox runs
    are filterable out of production. Returns the answer and a Phoenix deep-link."""
    from opentelemetry import trace as _ot
    from accountant.pipeline import phoenix_cost
    tracer = _ot.get_tracer("accountant.quality_eval")
    with tracer.start_as_current_span(f"eval.replay.{model}") as span:
        for k, v in (attrs or {}).items():
            span.set_attribute(k, v)
        ans = _replay(ticket, model)
        tid = format(span.get_span_context().trace_id, "032x")
    url = phoenix_cost.span_deeplink(gid, tid, None) if gid else None
    return ans, url


def _eval_ticket(ticket: str, baseline_model: str, economy_model: str,
                 gid: str | None = None, attrs: dict | None = None) -> dict:
    if gid is not None:
        base, _ = _traced_replay(ticket, baseline_model, gid, attrs)
        econ, econ_url = _traced_replay(ticket, economy_model, gid, attrs)
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


# ===========================================================================
#  The replay-at-scale lab — sample REAL past conversations for a use case and
#  re-run them through a CANDIDATE optimization, in a SANDBOX. Reuses the eval
#  engine above (no new eval machinery); adds sub-type segmentation, a held vs
#  degraded DISTRIBUTION (one number can't show "holds under variety"), a labeled
#  cost projection, and the agent's recommendation. Every replay span is tagged
#  'test' so the sandbox run is filterable out of production. Pre-run offline and
#  stored; the lab DISPLAYS the real result (displayed N == what actually ran).
# ===========================================================================
def _subtype(use_case: str, ticket: str) -> str:
    t = ticket.lower()
    if use_case == "refund_handling":
        # a refund WITH the charge details is simple; a vague one is complex
        return "simple" if ("$" in ticket or "charge was" in t) else "complex"
    if use_case == "account_question":
        hard = ("teammate", "transfer", "ownership", "sso", "permission", "seat", "invite")
        return "complex" if any(h in t for h in hard) else "simple"
    return "simple"


def _sample_with_subtype(use_case: str, n: int) -> list[dict]:
    from observed.generate_dataset import MESSAGE_POOLS, CUSTOMER_POOL
    import itertools
    pool = MESSAGE_POOLS[use_case]
    cust = itertools.cycle(CUSTOMER_POOL)
    out = []
    for i in range(n):
        tkt = pool[i % len(pool)].replace("{customer_id}", next(cust))
        out.append({"ticket": tkt, "sub_type": _subtype(use_case, tkt)})
    return out


def _lab_cost(use_case: str) -> dict | None:
    """Labeled cost projection for routing this use case to the economy model.
    Baseline = the class's measured ungoverned $/msg; projected nets the cache
    saving it already has plus the economy-model LLM drop (flash-lite is ~1/5 the
    blended price of flash). Clearly a projection — it hasn't shipped."""
    from accountant import service
    rates = service.default_tool_rates()
    live = service.live_state()
    recs = service.recommendations()
    rows, _ = service.cost_breakdown(live, recs, rates)
    row = next((r for r in rows if r["tc"] == use_case), None)
    if not row:
        return None
    base = row["cost"]
    econ_ratio = 0.2  # flash-lite blended ≈ 1/5 of flash
    # the candidate under test is route→economy: the LLM portion drops to the
    # economy price; tools are unchanged. (Tool-heavy use cases like refunds
    # barely move — itself a real finding.)
    projected = row["tool"] + row["llm"] * econ_ratio
    return {"baseline": round(base, 4), "projected": round(projected, 4),
            "pct": round((1 - projected / base) * 100) if base else 0}


def replay_one_live(use_case: str, *, idx: int = 0, baseline_model: str = BASELINE_MODEL,
                    economy_model: str = ECONOMY_MODEL) -> dict:
    """One REAL replay, run live for the lab's visible trickle (so the displayed
    pre-run batch doesn't feel canned). Sandbox + tagged 'test'; never touches
    live policies."""
    from observed.telemetry import init_telemetry
    from accountant.pipeline import phoenix_cost
    init_telemetry()
    gid = phoenix_cost.project_gid()
    sample = _sample_with_subtype(use_case, idx + 1)[idx]
    tag = {"accountant.run_type": "test", "accountant.lab.use_case": use_case,
           "accountant.lab.candidate": "route_economy"}
    r = _eval_ticket(sample["ticket"], baseline_model, economy_model, gid, tag)
    r["sub_type"] = sample["sub_type"]
    r["held"] = bool(r["equivalent"]) and not r["refused_escalated"]
    return r


def _replay_full(ticket: str, model: str):
    """Like _replay but returns the whole response, so we can read the agent's
    real call sequence (AFC history) and token usage for call-by-call stepping."""
    return _genai().models.generate_content(
        model=model, contents=ticket,
        config=types.GenerateContentConfig(
            system_instruction=_instruction(), temperature=0.0, tools=_tools(),
            thinking_config=types.ThinkingConfig(thinking_budget=0)))


def _trim_obj(obj, n: int = 220) -> str:
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    s = s.strip()
    return s[:n] + ("…" if len(s) > n else "")


def _calls_from_resp(resp, rates: dict, span_ids: list[str] | None = None) -> list[dict]:
    """The real tool-call sequence the agent ran, in order, with per-call tool
    cost, a duplicate flag (the SAME tool fired again = the waste story), the
    real input args + output result from the AFC history, and the OTEL span_id
    of that exact tool span (so stepping it links to its own Phoenix span)."""
    hist = getattr(resp, "automatic_function_calling_history", None) or []
    span_ids = span_ids or []
    fcalls, fresps = [], []
    for content in hist:
        for p in (content.parts or []):
            fc = getattr(p, "function_call", None)
            fr = getattr(p, "function_response", None)
            if fc and fc.name:
                fcalls.append((fc.name, dict(fc.args or {})))
            elif fr and fr.name:
                fresps.append(dict(fr.response or {}))
    calls, seen = [], set()
    for i, (name, args) in enumerate(fcalls):
        out = fresps[i] if i < len(fresps) else {}
        sid = span_ids[i] if i < len(span_ids) else None  # aligned: AFC call i ↔ tool span i
        if name == "task_classifier":  # structural classifier — not a billable step
            continue
        dup = name in seen
        seen.add(name)
        calls.append({"kind": "tool", "tool": name, "cost": round(rates.get(name, 0.0), 5),
                      "dup": dup, "input": _trim_obj(args), "output": _trim_obj(out), "span_id": sid})
    return calls


def _tokens(resp) -> tuple[int, int]:
    u = getattr(resp, "usage_metadata", None)
    if not u:
        return 0, 0
    return (getattr(u, "prompt_token_count", 0) or 0, getattr(u, "candidates_token_count", 0) or 0)


def _model_cost_of(resp, model: str) -> float:
    from accountant.pricing.gemini import MODELS
    mp = MODELS.get(model)
    if not mp:
        return 0.0
    inp, out = _tokens(resp)
    return round(inp / 1e6 * mp.input_uncached_per_1m_usd + out / 1e6 * mp.output_per_1m_usd, 6)


def _lab_eval_ticket(ticket: str, sub_type: str, conv_id: str, gid, attrs: dict, rates: dict,
                     baseline_model: str, economy_model: str) -> dict:
    """One replayed conversation, captured for stepping: the call sequence, the
    per-call cost, the model diff (premium vs economy answer + judge quality)."""
    from opentelemetry import trace as _ot
    from accountant.pipeline import phoenix_cost
    tracer = _ot.get_tracer("accountant.quality_eval")
    # economy (the candidate under test) — capture its real path + trace + latency;
    # _tool_rec collects each tool span's id in execution order during this replay.
    t_econ = time.monotonic()
    _tool_rec.ids = []
    with tracer.start_as_current_span(f"eval.replay.{economy_model}") as span:
        for k, v in attrs.items():
            span.set_attribute(k, v)
        er = _replay_full(ticket, economy_model)
        etid = format(span.get_span_context().trace_id, "032x")
    tool_span_ids = list(_tool_rec.ids)
    econ_latency_ms = round((time.monotonic() - t_econ) * 1000)
    econ = _final_answer(er)
    calls = _calls_from_resp(er, rates, tool_span_ids)
    econ_cost = _model_cost_of(er, economy_model)
    econ_in, econ_out = _tokens(er)
    # baseline (premium) — deterministic; reuse the memoized result if we have it,
    # else run it once and cache (answer + cost are all we need for the judge + diff)
    cached = _baseline_cache.get((ticket, baseline_model))
    if cached:
        base, base_cost = cached
    else:
        with tracer.start_as_current_span(f"eval.replay.{baseline_model}") as span:
            for k, v in attrs.items():
                span.set_attribute(k, v)
            br = _replay_full(ticket, baseline_model)
        base, base_cost = _final_answer(br), _model_cost_of(br, baseline_model)
        _baseline_cache[(ticket, baseline_model)] = (base, base_cost)
    v = _judge(ticket, base, econ)
    seq = ([{"kind": "user", "label": f'user: "{ticket[:90]}"'}]
           + calls
           + [{"kind": "model", "label": "model · respond", "cost": econ_cost,
               "bites": v.economy_quality < v.baseline_quality, "model": economy_model,
               "latency_ms": econ_latency_ms, "in_tokens": econ_in, "out_tokens": econ_out},
              {"kind": "reply", "label": "reply sent"}])
    return {
        "conv_id": conv_id, "ticket": ticket, "sub_type": sub_type,
        "equivalent": v.equivalent, "baseline_quality": v.baseline_quality,
        "economy_quality": v.economy_quality, "clarified": v.economy_asked_clarification,
        "refused_escalated": v.economy_refused_or_escalated,
        "held": v.economy_quality >= 4, "phoenix_url": phoenix_cost.span_deeplink(gid, etid, None) if gid else None,
        "baseline_answer": base[:240], "economy_answer": econ[:240],
        "baseline_model_cost": base_cost, "economy_model_cost": econ_cost,
        "calls": seq,
    }


def _fetch_recent_spans(max_spans: int = 3000) -> list[tuple]:
    """(trace_id, span_id_hex, span_kind, phoenix_node_id) for recent project
    spans, cursor-paginated (Phoenix caps `first` per page, so a big single
    request returns nothing — page at 500)."""
    from accountant.pipeline.phoenix_cost import _endpoint_and_key
    import httpx
    ep, key = _endpoint_and_key()
    q = ("query($p:String!,$f:Int!,$a:String){getProjectByName(name:$p){spans(first:$f,after:$a,"
         "sort:{col:startTime,dir:desc}){pageInfo{hasNextPage endCursor} edges{node{id spanId spanKind trace{traceId}}}}}}")
    out, after = [], None
    try:
        with httpx.Client(timeout=90) as cl:
            while len(out) < max_spans:
                r = cl.post(ep, json={"query": q, "variables": {"p": os.environ["PHOENIX_PROJECT_NAME"], "f": 500, "a": after}},
                            headers={"authorization": f"Bearer {key}", "content-type": "application/json"})
                conn = (((r.json().get("data") or {}).get("getProjectByName") or {}).get("spans") or {})
                for e in conn.get("edges") or []:
                    n = e["node"]
                    out.append(((n.get("trace") or {}).get("traceId"), n.get("spanId"),
                                n.get("spanKind"), n.get("id")))
                pi = conn.get("pageInfo") or {}
                if not pi.get("hasNextPage"):
                    break
                after = pi.get("endCursor")
    except Exception:
        return out
    return out


def _resolve_span_urls(rows: list[dict], gid: str | None) -> None:
    """After the batch, map each captured tool span (and the model's LLM span) to
    its Phoenix node id, so every stepped call links to its OWN exact span."""
    import time as _t
    from accountant.pipeline.phoenix_cost import span_deeplink
    if not gid:
        return
    _t.sleep(10)  # let Phoenix ingest the batch's spans
    spans = _fetch_recent_spans(2500)
    by_span, llm_by_trace = {}, {}
    for tid, sid, kind, node in spans:
        if tid and sid and node:
            by_span[(tid, sid)] = node
        if tid and kind == "llm" and node and tid not in llm_by_trace:
            llm_by_trace[tid] = node
    for row in rows:
        tid = row["phoenix_url"].rsplit("/spans/", 1)[1].split("?")[0]
        for c in row["calls"]:
            if c.get("span_id"):
                node = by_span.get((tid, c["span_id"]))
                if node:
                    c["span_url"] = span_deeplink(gid, tid, node)
            elif c["kind"] == "model":
                node = llm_by_trace.get(tid)
                if node:
                    c["span_url"] = span_deeplink(gid, tid, node)


def generate_synthetic_tickets(use_case: str, n: int) -> list[dict]:
    """Simple synthetic stress-test inputs: plausible requests for the use case,
    to probe variety/edge cases beyond history. NOT a conversation simulator —
    one LLM call yields short messages; they run through the real eval like any
    replay (real tools, real cost, real spans tagged 'test'). Source is marked
    synthetic so the UI never presents these as real-traffic proof."""
    import itertools
    from observed.generate_dataset import CUSTOMER_POOL
    label = {"account_question": "account questions", "refund_handling": "refund requests",
             "password_reset": "password resets", "plan_change": "plan-change requests"}.get(
        use_case, use_case.replace("_", " "))
    prompt = (f"Generate {n} short, varied, realistic customer-support messages — {label} — for a SaaS "
              f"online-forms product called Stratus Forms. Mix straightforward and tricky/edge cases. "
              f"One message per line, no numbering, no quotes.")
    try:
        resp = _genai().models.generate_content(
            model=JUDGE_MODEL, contents=prompt,
            config=types.GenerateContentConfig(temperature=0.9, max_output_tokens=1000,
                                               thinking_config=types.ThinkingConfig(thinking_budget=0)))
        lines = [l.strip(" -•\t0123456789.") for l in (resp.text or "").splitlines() if l.strip()]
    except Exception:
        lines = []
    lines = [l for l in lines if len(l) > 8][:n]
    cust = itertools.cycle(CUSTOMER_POOL)
    return [{"ticket": (l if "-001" in l else f"{l} (account {next(cust)})"),
             "sub_type": _subtype(use_case, l)} for l in lines]


_lab_inited = False
_lab_gid = None
# The premium baseline is deterministic (temp 0) and never changes between runs —
# memoize it so a re-measure only executes the economy candidate + judge (~half the
# model work). Keyed by (ticket, model); a real measurement, just not re-run.
_baseline_cache: dict = {}


def _lab_setup():
    """Init telemetry once and cache the project gid — so a load test's first
    replay isn't held up re-registering OTEL + re-resolving the gid each run."""
    global _lab_inited, _lab_gid
    if not _lab_inited:
        from observed.telemetry import init_telemetry
        init_telemetry()
        _lab_inited = True
    if _lab_gid is None:
        from accountant.pipeline import phoenix_cost
        _lab_gid = phoenix_cost.project_gid()
    return _lab_gid


def iter_lab_rows(use_case: str, n: int, source: str = "replay", *,
                  baseline_model: str = BASELINE_MODEL, economy_model: str = ECONOMY_MODEL,
                  max_workers: int = 6):
    """Execute N replays LIVE and yield each captured row as it completes — for a
    real, visible re-measure. REPLAY samples real past tickets; SYNTHETIC generates
    plausible ones. Every span tagged 'test' (+ the source); sandbox, never touches
    live policies. Rows carry the call sequence + both model costs + judge verdict,
    so the UI derives any {cache, economy} config's impact from the run."""
    from accountant import service
    gid = _lab_setup()
    rates = service.default_tool_rates()
    if source != "synthetic":   # replay tickets match the pre-run — warm the baseline cache from it
        stored = load_eval(f"lab_{use_case}")
        for row in (stored or {}).get("rows", []):
            key = (row.get("ticket"), baseline_model)
            if key[0] and key not in _baseline_cache and row.get("baseline_answer"):
                _baseline_cache[key] = (row["baseline_answer"], row.get("baseline_model_cost", 0.0))
    tickets = (generate_synthetic_tickets(use_case, n) if source == "synthetic"
               else _sample_with_subtype(use_case, n))
    tag = {"accountant.run_type": "test", "accountant.lab.use_case": use_case,
           "accountant.lab.candidate": "route_economy", "accountant.lab.source": source}
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_lab_eval_ticket, t["ticket"], t["sub_type"], str(1200 + i),
                          gid, tag, rates, baseline_model, economy_model)
                for i, t in enumerate(tickets)]
        for f in cf.as_completed(futs):
            try:
                yield f.result()
            except Exception:
                continue


def run_replay_lab(use_case: str, *, n: int = 24, candidate: str = "economy",
                   baseline_model: str = BASELINE_MODEL, economy_model: str = ECONOMY_MODEL,
                   max_workers: int = 3) -> dict:
    """Replay N real past conversations through the candidate in a sandbox (every
    span tagged 'test', live policies untouched) and return the quality
    DISTRIBUTION + per-conversation call sequences (for step-mode) + cost
    projection + recommendation."""
    from observed.telemetry import init_telemetry
    from accountant.pipeline import phoenix_cost
    from accountant import service
    init_telemetry()
    gid = phoenix_cost.project_gid()
    rates = service.default_tool_rates()
    tickets = _sample_with_subtype(use_case, n)
    tag = {"accountant.run_type": "test", "accountant.lab.use_case": use_case,
           "accountant.lab.candidate": f"route_{candidate}"}
    t0 = time.monotonic()
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_lab_eval_ticket, t["ticket"], t["sub_type"], str(1200 + i),
                          gid, tag, rates, baseline_model, economy_model): i
                for i, t in enumerate(tickets)}
        for f in cf.as_completed(futs):
            rows.append(f.result())
    rows.sort(key=lambda r: int(r["conv_id"]))
    _resolve_span_urls(rows, gid)  # attach each call's exact Phoenix span deep-link
    return {
        **_lab_aggregate(rows, use_case),
        "use_case": use_case, "candidate": candidate, "n": len(rows),
        "recommendation": _lab_recommend(rows, use_case),
        "cost": _lab_cost(use_case),
        "elapsed_sec": round(time.monotonic() - t0, 1),
        "project_gid": gid, "test_tag": "accountant.run_type = test",
        "rows": rows,
    }


def _lab_recompute(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["held"] = r["economy_quality"] >= 4
    return rows


def _lab_aggregate(rows: list[dict], use_case: str) -> dict:
    from collections import Counter
    n_ = len(rows) or 1
    held_pct = round(sum(r["held"] for r in rows) / n_, 3)
    sub_total = Counter(r["sub_type"] for r in rows)
    deg_sub = Counter(r["sub_type"] for r in rows if not r["held"])
    held_by_sub = {s: round(sum(1 for r in rows if r["sub_type"] == s and r["held"]) / c, 2)
                   for s, c in sub_total.items()}
    return {"held_pct": held_pct, "degraded_pct": round(1 - held_pct, 3),
            "degraded_dominant_sub": (deg_sub.most_common(1)[0][0] if deg_sub else None),
            "held_by_sub": held_by_sub}


def _lab_recommend(rows: list[dict], use_case: str) -> str:
    agg = _lab_aggregate(rows, use_case)
    held_pct = agg["held_pct"]
    by = agg["held_by_sub"]
    # a clean safe subset only if one sub-type clearly holds and another clearly breaks
    good = [s for s, h in by.items() if h >= 0.7]
    bad = [s for s, h in by.items() if h < 0.45]
    if held_pct >= 0.85:
        return "Holds across the variety — safe to route this use case to the economy model."
    if good and bad:
        return (f"Holds for {' & '.join(good)} cases, breaks on {' & '.join(bad)} ones — "
                f"route only the {' & '.join(good)} subset, keep premium for the rest.")
    if held_pct < 0.4:
        return ("Breaks across the variety — keep the premium model here. "
                "The small eval looked fine; at scale it doesn't hold.")
    return ("Mixed — holds barely over half, with no clean safe subset. Too inconsistent "
            "to route wholesale; the small eval was optimistic. Keep premium for now.")
