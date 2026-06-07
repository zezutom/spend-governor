"""Generate a synthetic dataset for the observed FLEET.

Drives EACH agent in the fleet (Support Co-Pilot, Refund Auditor, Sales
Assistant, Docs Bot) through its own messages, so every agent's signature
waste pattern lands in the traces for the Accountant to detect. Each agent
stamps its identity as the grouping key, so downstream aggregation groups by
agent. customer_id is passed as the ADK user_id for per-customer rollups.

Usage:
    uv run python -m observed.generate_dataset [PER_AGENT] [CONCURRENCY]

Defaults: PER_AGENT=50, CONCURRENCY=4. PER_AGENT is the number of traces per
agent (4 agents → 4×PER_AGENT total). Keep concurrency low to stay under the
Vertex quota. Start with PER_AGENT=4 to validate wiring.
"""

import asyncio
import random
import sys

from dotenv import load_dotenv

load_dotenv()

from observed.telemetry import init_telemetry

init_telemetry()

from google.adk.runners import InMemoryRunner
from google.genai import types

from observed.fleet import AGENT_ORDER, FLEET, build_fleet_agent


APP_NAME = "agent-accountant"


CUSTOMER_POOL = [
    "ACME-001",
    "GLOBEX-001",
    "INITECH-001",
    "STARK-001",
    "WONKA-001",
    "WAYNE-001",
]


# One LlmAgent per fleet agent, built once (stateless across runs). Runner is
# per-call because InMemoryRunner's session service holds per-invocation state.
_AGENTS = {aid: build_fleet_agent(aid) for aid in AGENT_ORDER}


def build_message(agent_id: str, customer_id: str) -> str:
    template = random.choice(FLEET[agent_id]["messages"])
    return template.replace("{customer_id}", customer_id)


async def run_one(agent_id: str, message: str, customer_id: str) -> bool:
    runner = InMemoryRunner(agent=_AGENTS[agent_id], app_name=APP_NAME)
    try:
        session = await runner.session_service.create_session(
            app_name=APP_NAME,
            user_id=customer_id,
        )
        content = types.Content(role="user", parts=[types.Part(text=message)])
        async for _ in runner.run_async(
            user_id=customer_id,
            session_id=session.id,
            new_message=content,
        ):
            pass
        return True
    except Exception as e:
        print(f"[error] {agent_id}/{customer_id}: {type(e).__name__}: {e}", file=sys.stderr)
        return False


async def main(per_agent: int, concurrency: int) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    # one job per (agent, i), INTERLEAVED round-robin so every agent accrues
    # traces together (no agent waits behind another's slow runs).
    jobs = [(aid, i) for i in range(per_agent) for aid in AGENT_ORDER]
    total = len(jobs)
    completed = 0

    async def bounded(agent_id: str) -> bool:
        nonlocal completed
        async with semaphore:
            customer = random.choice(CUSTOMER_POOL)
            message = build_message(agent_id, customer)
            ok = await run_one(agent_id, message, customer)
            completed += 1
            if completed % 20 == 0 or completed == total:
                print(f"[{completed}/{total}] done")
            return ok

    print(f"Generating {per_agent} traces × {len(AGENT_ORDER)} agents "
          f"= {total} total, concurrency {concurrency}...")
    results = await asyncio.gather(*[bounded(aid) for aid, _ in jobs])
    print(f"\nGenerated {sum(results)}/{total} traces successfully.")


if __name__ == "__main__":
    per_agent = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    asyncio.run(main(per_agent, concurrency))
