"""The Margin Agent — a single Gemini structured-output call.

Unlike the Accountant (an ADK tool-loop that reads Phoenix), the Margin
Agent has no tools and measures nothing: it receives a fully-computed
INPUT (cost from the Accountant + the operator's price) and reasons over
it to produce credit tiering + margin-drift triage. The deterministic
INPUT builder owns all arithmetic; this agent only selects, orders, and
justifies — and Gemini's `response_schema` forces the result into the
validated MarginOutput shape.
"""

import json
import os

from google import genai
from google.genai import types

from accountant.margin.prompt import MARGIN_AGENT_INSTRUCTION
from accountant.margin.schema import MarginOutput


# The builder does all arithmetic; the agent only selects, orders, and
# justifies — light, grounded work. So a fast model with a SMALL thinking
# budget is both quicker and as accurate as a reasoning model here: flash
# with budget 256 returns in ~7s vs ~48s for pro's default thinking, same
# answers. (flash-lite is faster still but slips — it mis-triaged an
# above-target segment.) All env-overridable.
DEFAULT_MARGIN_MODEL = os.environ.get("ACCOUNTANT_MARGIN_MODEL", "gemini-2.5-flash")
DEFAULT_THINKING_BUDGET = int(os.environ.get("ACCOUNTANT_MARGIN_THINKING_BUDGET", "256"))


_client: genai.Client | None = None


def _genai_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def run_margin(margin_input: dict, *, model: str = DEFAULT_MARGIN_MODEL) -> MarginOutput:
    """Run the Margin Agent over one fully-built INPUT object.

    Returns a validated MarginOutput. temperature=0 keeps it deterministic
    and discourages the model from inventing numbers — every value should
    be copied from `margin_input`.
    """
    resp = _genai_client().models.generate_content(
        model=model,
        contents=f"INPUT:\n{json.dumps(margin_input, indent=2)}",
        config=types.GenerateContentConfig(
            system_instruction=MARGIN_AGENT_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=MarginOutput,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=DEFAULT_THINKING_BUDGET),
        ),
    )
    parsed = resp.parsed
    if isinstance(parsed, MarginOutput):
        return parsed
    # Fallback: validate the raw JSON text ourselves if the SDK didn't parse.
    return MarginOutput.model_validate_json(resp.text)
