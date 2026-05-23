"""Generate a synthetic dataset of Helpdesk Co-Pilot runs.

Drives the agent through many tickets across the four task types so
there is enough trace data for downstream analysis. The customer_id is
passed as the ADK user_id so it propagates into trace attributes for
per-customer rollups.

Usage:
    uv run python -m observed.generate_dataset [N] [CONCURRENCY]

Defaults: N=1200, CONCURRENCY=10. Start with N=20 to validate the wiring
before kicking off a full 1,200-run batch (which takes 20-30 minutes and
spends a few dollars of Gemini quota).
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

from observed.agent import build_agent


APP_NAME = "agent-accountant"


# Refund handling is intentionally a large slice (25%) because the
# anti-pattern lives there and we want enough refund traces for the
# aggregation to surface it cleanly.
TASK_DISTRIBUTION = {
    "password_reset": 0.35,
    "account_question": 0.25,
    "refund_handling": 0.25,
    "plan_change": 0.15,
}


# Refund messages are a mix of complete (with amount + date, so the
# agent reaches refund_api) and incomplete (vague, so the agent ends in
# a clarifying question). Both are realistic outcomes; both still
# exhibit the redundant-web_search anti-pattern.
MESSAGE_POOLS = {
    "password_reset": [
        "I can't log in to my Stratus Forms account. Help me reset my password.",
        "Forgot my password. How do I reset it?",
        "I'm locked out. Need a password reset.",
        "Can't sign in. My password isn't working.",
        "Reset my password please.",
        "Help, I'm locked out of my account.",
        "My password isn't working anymore. What do I do?",
        "I keep getting 'invalid credentials' when I try to log in.",
        "Password reset link expired. Need a new one.",
        "Account locked after too many login attempts.",
    ],
    "account_question": [
        "How do I add a teammate to my account?",
        "Where do I update my billing address?",
        "How do I transfer ownership of my account to a colleague?",
        "How do I enable SSO?",
        "Where's my invoice from last month?",
        "How do I change my email address?",
        "Where do I find my API key?",
        "Can I export all my form responses?",
        "How do I delete a form?",
        "What's the difference between Pro and Enterprise?",
        "Can I use a custom domain for my forms?",
        "Where do I see analytics for my forms?",
        "How do I integrate Stratus Forms with Zapier?",
        "Can I embed forms in my website?",
        "What payment methods do you accept?",
    ],
    "refund_handling": [
        # Complete — agent should reach refund_api.
        "I want a refund. My account is {customer_id}. The charge was $49 on May 1.",
        "Please refund me. Account {customer_id}, charged $49 last month.",
        "Refund $49 from my Pro subscription. Account {customer_id}.",
        "Account {customer_id}, refund the $49 charge from last month.",
        "I need a refund. {customer_id}, $499 on April 15.",
        "Please reverse the May 1 charge of $49 on {customer_id}.",
        # Incomplete — agent should ask a clarifying question.
        "Can you refund my last charge? My account is {customer_id}.",
        "I want my money back.",
        "Reverse the charge from last month.",
        "Refund please. Account {customer_id}.",
        "Got double-charged, need a refund.",
        "Refund the charge from last week.",
    ],
    "plan_change": [
        "I want to upgrade from Pro to Enterprise. My account is {customer_id}.",
        "Downgrade me to Free, please. {customer_id}.",
        "How do I switch to Enterprise? Account {customer_id}.",
        "Cancel my Pro subscription. {customer_id}.",
        "Upgrade {customer_id} to the next tier.",
        "Please downgrade my plan to Pro. {customer_id}.",
        "Move me from Enterprise back to Pro. Account {customer_id}.",
        "Can I change my billing cycle from monthly to annual?",
        "Switch my account {customer_id} to annual billing.",
    ],
}


CUSTOMER_POOL = [
    "ACME-001",
    "GLOBEX-001",
    "INITECH-001",
    "STARK-001",
    "WONKA-001",
    "WAYNE-001",
]


# Shared agent — LlmAgent is stateless across runs in the Helpdesk
# Co-Pilot's configuration (no per-session memory). Runner is per-call
# because InMemoryRunner's session service holds state we don't want to
# share between concurrent invocations.
_AGENT = build_agent()


def pick_task() -> str:
    r = random.random()
    cumulative = 0.0
    for task, weight in TASK_DISTRIBUTION.items():
        cumulative += weight
        if r < cumulative:
            return task
    return "account_question"


def build_message(task: str, customer_id: str) -> str:
    template = random.choice(MESSAGE_POOLS[task])
    return template.replace("{customer_id}", customer_id)


async def run_one(message: str, customer_id: str) -> bool:
    runner = InMemoryRunner(agent=_AGENT, app_name=APP_NAME)
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
        print(f"[error] {customer_id}: {type(e).__name__}: {e}", file=sys.stderr)
        return False


async def main(n: int, concurrency: int) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0

    async def bounded(_: int) -> bool:
        nonlocal completed
        async with semaphore:
            task = pick_task()
            customer = random.choice(CUSTOMER_POOL)
            message = build_message(task, customer)
            ok = await run_one(message, customer)
            completed += 1
            if completed % 50 == 0 or completed == n:
                print(f"[{completed}/{n}] done")
            return ok

    print(f"Generating {n} traces with concurrency {concurrency}...")
    results = await asyncio.gather(*[bounded(i) for i in range(n)])
    successes = sum(results)
    print(f"\nGenerated {successes}/{n} traces successfully.")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    asyncio.run(main(n, concurrency))
