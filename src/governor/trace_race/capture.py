"""Capture a seeded trace pair for the race: one ticket, run twice.

The race must show the SAME conversation run two ways — so we take one
ticket, run it through the observed agent with policies OFF (baseline) and
again with policies ON (governed), and capture both real traces. This is the
brief's "replay real captured traces" / "Run one live" path; nothing is
hand-built.

Run it with the ingest server up and ingest fan-out on, so the spans land in
the cache where align/fixture read them:

    GOVERNOR_INGEST_URL=http://localhost:8765 \\
        uv run python -m governor.trace_race.capture "I want a refund for order 12345"

It restores the policy override on exit. ~2 live agent runs — Vertex-quota
sensitive, so run it when quota is healthy.
"""

import asyncio
import sys
import time

from governor.pipeline.db import connect


APP_NAME = "agent-accountant"


def _trace_ids() -> set[str]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT DISTINCT trace_id FROM spans").fetchall()}


def _wait_for_new_trace(before: set[str], *, timeout: float = 90.0, settle: float = 2.0) -> str | None:
    """Poll the cache until one new trace_id appears (vs the `before` snapshot)
    and its span count stops growing — i.e. the run's spans have fully landed
    through the ingest fan-out. Returns the trace_id, or None on timeout."""
    deadline = time.monotonic() + timeout
    candidate, last_count, stable_since = None, -1, 0.0
    while time.monotonic() < deadline:
        with connect() as c:
            new = _trace_ids() - before
            if new:
                candidate = max(  # newest by latest span
                    new,
                    key=lambda t: c.execute(
                        "SELECT MAX(start_time) FROM spans WHERE trace_id=?", (t,)
                    ).fetchone()[0] or "",
                )
                n = c.execute("SELECT COUNT(*) FROM spans WHERE trace_id=?", (candidate,)).fetchone()[0]
                if n == last_count and n > 0:
                    if time.monotonic() - stable_since >= settle:
                        return candidate
                else:
                    last_count, stable_since = n, time.monotonic()
        time.sleep(0.5)
    return candidate


def capture_pair(ticket: str, customer_id: str = "race-demo") -> dict:
    """Run `ticket` twice (baseline policies-off, governed policies-on),
    capture both trace_ids and both final answer texts. Requires ingest on
    (GOVERNOR_INGEST_URL set + server up) so the spans reach the cache."""
    from observed.telemetry import init_telemetry
    init_telemetry()
    from observed.agent import build_agent
    from governor.wrapper import wrapper as gov
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = build_agent()

    async def _run() -> str:
        runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
        sess = await runner.session_service.create_session(app_name=APP_NAME, user_id=customer_id)
        content = types.Content(role="user", parts=[types.Part(text=ticket)])
        out: list[str] = []
        async for ev in runner.run_async(user_id=customer_id, session_id=sess.id, new_message=content):
            if ev.content and ev.content.parts:
                for p in ev.content.parts:
                    if p.text:
                        out.append(p.text)
        return " ".join(out).strip()

    try:
        before = _trace_ids()
        gov.set_policy_override([])              # baseline: ungoverned
        base_answer = asyncio.run(_run())
        base_tid = _wait_for_new_trace(before)

        before = _trace_ids()
        gov.set_policy_override(None)            # governed: store's active policies
        gov_answer = asyncio.run(_run())
        gov_tid = _wait_for_new_trace(before)
    finally:
        gov.set_policy_override(None)

    return {
        "ticket": ticket,
        "baseline_trace_id": base_tid,
        "baseline_answer": base_answer,
        "governed_trace_id": gov_tid,
        "governed_answer": gov_answer,
    }


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    if len(sys.argv) < 2:
        from observed.generate_dataset import MESSAGE_POOLS
        ticket = MESSAGE_POOLS["refund_handling"][0].replace("{customer_id}", "CUST-00042")
        print(f"(no ticket given — using a refund sample)\n  {ticket}\n")
    else:
        ticket = sys.argv[1]
    cap = capture_pair(ticket)
    print("baseline:", cap["baseline_trace_id"], "| governed:", cap["governed_trace_id"])
    print("baseline answer:", (cap["baseline_answer"] or "")[:120])
    print("governed answer:", (cap["governed_answer"] or "")[:120])
    from governor.trace_race.fixture import build_and_save
    path = build_and_save(cap)
    print("fixture written:", path)


if __name__ == "__main__":
    main()
