from google.adk.agents import LlmAgent

from governor.governor import govern_tools, model_routing_callback
from observed.config import load_instruction
from observed.tools import (
    customer_lookup,
    escalate_human,
    kb_lookup,
    refund_api,
    task_classifier,
    ticket_update,
    web_search,
)


def build_agent() -> LlmAgent:
    # Tools are wrapped by the runtime governor (the enforcement-plane
    # stand-in): their execution flows through the boundary where
    # active economic policies — semantic-cache interception, etc. —
    # apply. Harmless when no policy is active. The instruction is the
    # baseline; the governor never edits it.
    tools = govern_tools([
        task_classifier,
        kb_lookup,
        web_search,
        customer_lookup,
        refund_api,
        ticket_update,
        escalate_human,
    ])
    return LlmAgent(
        name="helpdesk_copilot",
        model="gemini-2.5-flash",
        instruction=load_instruction(),
        tools=tools,
        # Enforcement hook for model routing — downgrades simple requests
        # to a cheaper model when a route_model policy is active. No-op
        # otherwise.
        before_model_callback=model_routing_callback,
    )
