"""Refactor #3 — prove savings via Phoenix Experiments.

Run a sample-ticket dataset twice through the observed agent — **baseline**
(policies off) and **governed** (policies on) — and let Phoenix compute each
experiment's aggregate cost natively. The delta is the realized savings, on
a URL-addressable Phoenix compare page. Every number is Phoenix's own
(`Experiment.costSummary`), so the per-policy savings claim is verifiable in
Phoenix in one click — the parity-clean aggregate proof
([[feedback-phoenix-parity]]).

Offline / on-demand: each agent run is ~30s, so this is triggered on policy
activation or a button, not per dashboard load. The dashboard links to the
resulting experiment pages.
"""

import asyncio
import itertools
import os
import time

import httpx
from phoenix.client import Client

from accountant.pipeline import phoenix_cost
from accountant.wrapper import wrapper as _wrapper


APP_NAME = "agent-accountant"


def _client() -> Client:
    """Phoenix client with a generous read timeout. The SDK's default client
    uses a 30s read timeout (httpx.Timeout(read=30)), which run_experiment's
    result upload + dataset creation can exceed under load → ReadTimeout.
    Passing our own http_client raises the read timeout for every call (and
    we must set auth ourselves, since `headers` is ignored when http_client
    is provided)."""
    base = os.environ["PHOENIX_COLLECTOR_ENDPOINT"].rstrip("/")
    key = os.environ.get("PHOENIX_API_KEY") or os.environ["PHOENIX_API_KEY_OBSERVED_WRITE"]
    http = httpx.Client(
        base_url=base,
        headers={"authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0),
    )
    return Client(http_client=http)
# Classes whose tickets exercise the active policies (web_search cache +
# model routing), so baseline-vs-governed shows a real delta.
_CLASSES = ("refund_handling", "password_reset", "account_question")


def _build_tickets(per_class: int) -> list[str]:
    from observed.generate_dataset import MESSAGE_POOLS, CUSTOMER_POOL
    cust = itertools.cycle(CUSTOMER_POOL)
    tickets: list[str] = []
    for cls in _CLASSES:
        pool = MESSAGE_POOLS[cls]
        for i in range(per_class):
            tickets.append(pool[i % len(pool)].replace("{customer_id}", next(cust)))
    return tickets


def _agent_task_factory():
    """Build the observed agent once and return a run_experiment task that
    runs it on each ticket. init_telemetry must precede build_agent so the
    agent's spans export to Phoenix (and roll up into the experiment cost)."""
    from observed.telemetry import init_telemetry
    init_telemetry()
    from observed.agent import build_agent
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    agent = build_agent()

    async def _run(text: str) -> str:
        runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
        sess = await runner.session_service.create_session(app_name=APP_NAME, user_id="exp")
        content = types.Content(role="user", parts=[types.Part(text=text)])
        out: list[str] = []
        async for ev in runner.run_async(user_id="exp", session_id=sess.id, new_message=content):
            if ev.content and ev.content.parts:
                for p in ev.content.parts:
                    if p.text:
                        out.append(p.text)
        return " ".join(out)[:500] or "(no text)"

    def task(example) -> str:
        return asyncio.run(_run((example.input or {}).get("ticket", "")))

    return task


def _resolve_experiment_id(client: Client, dataset_id: str, name: str, ran) -> str | None:
    """run_experiment returns a dict; pull the experiment id from it, else
    fall back to the newest experiment of this name under the dataset."""
    if isinstance(ran, dict):
        for k in ("experiment_id", "id"):
            if isinstance(ran.get(k), str):
                return ran[k]
        exp = ran.get("experiment")
        if isinstance(exp, dict) and isinstance(exp.get("id"), str):
            return exp["id"]
    try:
        exps = client.experiments.list(dataset_id=dataset_id)
        def _name(e): return e.get("name") if isinstance(e, dict) else getattr(e, "name", None)
        def _id(e): return e.get("id") if isinstance(e, dict) else getattr(e, "id", None)
        matches = [e for e in exps if _name(e) == name]
        if matches:
            return _id(matches[-1])
    except Exception:
        pass
    return None


def _cost_with_retry(experiment_id: str, tries: int = 6, delay: float = 4.0) -> dict | None:
    """Phoenix rolls up experiment cost shortly after the runs land; poll."""
    for _ in range(tries):
        c = phoenix_cost.experiment_cost(experiment_id)
        if c and c.get("total_cost_usd") is not None:
            return c
        time.sleep(delay)
    return phoenix_cost.experiment_cost(experiment_id)


def run_savings_experiments(label: str, per_class: int = 3, timeout: int = 180) -> dict:
    """Run baseline (policies off) vs governed (policies on) experiments and
    return Phoenix-computed costs + the savings delta + the compare-page URL.

    Requires the policies to demonstrate to be ACTIVE in the store (the
    governed run reads them). Restores the policy override on exit.
    """
    os.environ.setdefault("PHOENIX_API_KEY", os.environ["PHOENIX_API_KEY_OBSERVED_WRITE"])
    client = _client()
    task = _agent_task_factory()
    tickets = _build_tickets(per_class)
    ds = client.datasets.create_dataset(
        name=f"savings-{label}",
        inputs=[{"ticket": t} for t in tickets],
        outputs=[{"ok": True} for _ in tickets],
        dataset_description="Refactor #3: baseline vs governed savings proof.",
        timeout=60,
    )
    ds_id = getattr(ds, "id", None) or (ds.get("id") if isinstance(ds, dict) else None)

    try:
        _wrapper.set_policy_override([])  # baseline: agent runs ungoverned
        base_ran = client.experiments.run_experiment(
            dataset=ds, task=task, experiment_name=f"baseline-{label}",
            timeout=timeout, print_summary=False)
        base_id = _resolve_experiment_id(client, ds_id, f"baseline-{label}", base_ran)

        _wrapper.set_policy_override(None)  # governed: use the store's policies
        gov_ran = client.experiments.run_experiment(
            dataset=ds, task=task, experiment_name=f"governed-{label}",
            timeout=timeout, print_summary=False)
        gov_id = _resolve_experiment_id(client, ds_id, f"governed-{label}", gov_ran)
    finally:
        _wrapper.set_policy_override(None)

    base_cost = _cost_with_retry(base_id) if base_id else None
    gov_cost = _cost_with_retry(gov_id) if gov_id else None
    b = (base_cost or {}).get("total_cost_usd")
    g = (gov_cost or {}).get("total_cost_usd")
    savings = round(b - g, 6) if (b is not None and g is not None) else None
    return {
        "dataset_id": ds_id,
        "tickets": len(tickets),
        "baseline": {"experiment_id": base_id, **(base_cost or {})},
        "governed": {"experiment_id": gov_id, **(gov_cost or {})},
        "savings_usd": savings,
        "compare_url": phoenix_cost.compare_url(ds_id, base_id, gov_id),
    }
