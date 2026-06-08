from google.adk.agents import LlmAgent

from governor.wrapper.wrapper import (
    cost_after_model_callback,
    model_routing_callback,
    trace_finalize_callback,
    trace_start_callback,
    wrap_tools,
)
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
    # Tool and model calls flow through the Accountant wrapper (the
    # enforcement-plane stand-in): active policies apply at the boundary
    # and every span is annotated with the accountant.* schema. Harmless
    # when no policy is active. The instruction is the baseline; the
    # wrapper never edits it.
    tools = wrap_tools([
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
        # before_model decides model routing; after_model computes the
        # LLM-call cost (baseline vs actual) once token counts are known.
        before_model_callback=model_routing_callback,
        after_model_callback=cost_after_model_callback,
        # Open a per-trace savings accumulator and flush it onto the root
        # span at finalization (trace-level accountant.* rollups).
        before_agent_callback=trace_start_callback,
        after_agent_callback=trace_finalize_callback,
    )
