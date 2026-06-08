"""The observed FLEET — several distinct agents under one Governor.

Instead of one support agent with four ticket types, the demo company runs a
small fleet of specialized agents, each with a different job and ONE signature
cost-waste pattern. The Governor watches the whole fleet and fixes each with
a different real lever:

  Support Co-Pilot  — resolves tickets       — redundant web_search ×3   → CACHE
  Refund Auditor    — checks refunds         — same web_search ×5 (loop)  → CAP
  Sales Assistant   — pricing / quotes       — needless web_search ×1     → SUPPRESS
  Docs Bot          — how-to answers         — premium model on trivial   → ROUTE (the trap)

Each agent stamps its identity as the grouping key (via its own
`task_classifier`, which simply returns the agent id), so the whole existing
pipeline — detection, cost breakdown, levers, canvas — groups by AGENT with no
schema change. The waste lives entirely in the instruction; the tools are honest
and shared. The wrapper never edits these prompts — it enforces policy at the
boundary.
"""

from google.adk.agents import LlmAgent

from governor.wrapper.wrapper import (
    cost_after_model_callback,
    model_routing_callback,
    trace_finalize_callback,
    trace_start_callback,
    wrap_tools,
)
from observed.tools import (
    customer_lookup,
    escalate_human,
    kb_lookup,
    refund_api,
    ticket_update,
    web_search,
)

_BASE = """You are part of Stratus Forms' automated support fleet (a SaaS form
builder). You handle one ticket end-to-end and close it with `ticket_update`
(status="resolved") including a concise customer_reply. If a request is outside
policy or needs human judgment, call `escalate_human` instead. Be concise.

ALWAYS call `task_classifier` with the customer's message FIRST, before any
other tool, on every ticket — it tags the handling agent. Then follow your
procedure below."""

# --- the four agents -------------------------------------------------------
# Each profile's `waste` paragraph is the intentional anti-pattern. It is real
# behavior the agent performs at runtime — the Governor detects it from the
# traces and fixes it with a lever, never by editing this text.
FLEET = {
    "support_copilot": {
        "label": "Support Co-Pilot",
        "purpose": "Resolves inbound helpdesk tickets",
        "model": "gemini-2.5-flash",
        "waste": "redundant web_search ×3",
        "instruction": _BASE + """

You are the Support Co-Pilot. Resolve general helpdesk tickets.
Procedure (mandatory, every ticket): (1) `kb_lookup` the relevant article
under /account/general or /account/password-reset, then (2) run exactly THREE
`web_search` calls to corroborate the answer against external sources — general
best practices, community guidance, and recent changes — before you reply. Do
not skip the three searches even if the KB already answers it.""",
        "messages": [
            "How do I add a teammate to my account?",
            "Where do I update my billing address?",
            "How do I enable SSO?",
            "How do I change my email address?",
            "Where do I find my API key?",
            "Can I export all my form responses?",
            "How do I delete a form?",
            "Can I use a custom domain for my forms?",
            "I can't log in. How do I reset my password?",
            "I'm locked out of my account, help.",
        ],
    },
    "refund_auditor": {
        "label": "Refund Auditor",
        "purpose": "Checks refund requests against policy",
        "model": "gemini-2.5-flash",
        "waste": "verification loop — same web_search ×5",
        "instruction": _BASE + """

You are the Refund Auditor. Decide refund requests against policy.
Procedure (mandatory, every request): (1) `kb_lookup` of /policies/refunds,
then (2) to be thorough, verify the policy is current by running `web_search`
with the EXACT query "current SaaS refund regulations" FIVE times in a row —
re-checking each time — before (3) `customer_lookup`, the decision, and
`refund_api` or `escalate_human`. Always run all five identical verification
searches; do not stop early.""",
        "messages": [
            "I want a refund. My account is {customer_id}. The charge was $49 on May 1.",
            "Please refund me. Account {customer_id}, charged $49 last month.",
            "Refund $49 from my Pro subscription. Account {customer_id}.",
            "Account {customer_id}, refund the $49 charge from last month.",
            "I need a refund. {customer_id}, $499 on April 15.",
            "Please reverse the May 1 charge of $49 on {customer_id}.",
            "Can you refund my last charge? My account is {customer_id}.",
            "Got double-charged, need a refund. Account {customer_id}.",
        ],
    },
    "sales_assistant": {
        "label": "Sales Assistant",
        "purpose": "Answers pricing questions and drafts quotes",
        "model": "gemini-2.5-flash",
        "waste": "needless web_search ×1",
        "instruction": _BASE + """

You are the Sales Assistant. Answer pricing and plan questions and draft quotes.
Procedure (mandatory, every ticket): (1) `kb_lookup` of /billing/plan-changes
for our own pricing, then (2) ALWAYS run ONE `web_search` for "competitor SaaS
form builder pricing" to benchmark before you quote — even though our pricing is
already in the KB — then (3) reply with the quote.""",
        "messages": [
            "What's the difference between Pro and Enterprise?",
            "How much is Enterprise per month?",
            "What payment methods do you accept?",
            "Can I get a quote for 50 seats on Pro?",
            "Is there an annual discount?",
            "What does the Pro plan include?",
            "How much to upgrade {customer_id} from Pro to Enterprise?",
            "Do you offer a nonprofit discount?",
        ],
    },
    "docs_bot": {
        "label": "Docs Bot",
        "purpose": "Answers how-to questions from the docs",
        "model": "gemini-2.5-flash",
        "waste": "premium model on trivial lookups",
        "instruction": _BASE + """

You are the Docs Bot. Answer short how-to questions straight from the docs.
Procedure (every ticket): (1) `kb_lookup` of the relevant /account/general or
/billing/plan-changes article, then (2) reply in one or two sentences. These are
simple, templated answers — keep it brief. Do not use web_search.""",
        "messages": [
            "How do I rename a form?",
            "Where is the dashboard?",
            "How do I duplicate a form?",
            "How do I see who's on my team?",
            "Where do I download an invoice?",
            "How do I turn on email notifications?",
            "How do I change my timezone?",
            "Where are my form analytics?",
        ],
    },
}

AGENT_ORDER = ["support_copilot", "refund_auditor", "sales_assistant", "docs_bot"]


def _make_classifier(agent_id: str):
    """A per-agent classifier: it stamps the AGENT's identity as the grouping
    key (task_class), so the existing pipeline groups by agent with no change.
    Named `task_classifier` so the wrapper recognizes and records it."""
    def task_classifier(message: str) -> dict:
        """Identify the handling agent for this ticket. MUST be called first,
        before any other tool, on every ticket. Returns {"task_class": <id>}."""
        return {"task_class": agent_id}
    return task_classifier


def build_fleet_agent(agent_id: str) -> LlmAgent:
    """Build one observed agent from its fleet profile. Tools + callbacks flow
    through the Governor wrapper exactly as the single agent did."""
    prof = FLEET[agent_id]
    tools = wrap_tools([
        _make_classifier(agent_id),
        kb_lookup,
        web_search,
        customer_lookup,
        refund_api,
        ticket_update,
        escalate_human,
    ])
    return LlmAgent(
        name=agent_id,
        model=prof["model"],
        instruction=prof["instruction"],
        tools=tools,
        before_model_callback=model_routing_callback,
        after_model_callback=cost_after_model_callback,
        before_agent_callback=trace_start_callback,
        after_agent_callback=trace_finalize_callback,
    )
