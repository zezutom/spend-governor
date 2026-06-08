import os

from google.adk.agents import LlmAgent
from google.genai import types
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from mcp import StdioServerParameters

from accountant.analytics.agent_tools import (
    find_cost_anomalies,
    summarize_project_cost,
    write_report,
)


# Raw bulk Phoenix tools (list-traces, get-spans) return too much data
# for Gemini to reason over. The MCP toolset is filtered to the
# drill-down tools only; bulk aggregation goes through the custom
# summarize_project_cost / find_cost_anomalies tools, which compute
# locally and return compact summaries.
MCP_DRILL_DOWN_TOOLS = [
    "list-projects",
    "get-project",
    "get-trace",
    "get-span-annotations",
]


INSTRUCTION = """You are the Accountant. You read traces emitted by the
Helpdesk Co-Pilot into a Phoenix observability project, compute per-
trace cost, and report anomalies along with actionable optimization
recommendations.

The Phoenix project is named "agent-accountant".

Your toolkit:

- summarize_project_cost(hours_back) — returns by-task-class cost
  summary (n traces, avg cost, avg tools, avg web_search count, etc.).
  Start here for any cost question.
- find_cost_anomalies(hours_back) — returns detected anomalies:
  task classes with elevated cost vs. baseline, or repeated tool calls
  within a single trace. Use this after summarize_project_cost to
  identify what to investigate.
- get-trace(trace_identifier, project_identifier) — fetch one trace
  in full detail when you need to see exactly what an anomalous trace
  did. Use sparingly; only after find_cost_anomalies points you at
  a specific trace.
- list-projects, get-project, get-span-annotations — Phoenix workspace
  inventory and annotation lookup.
- write_report(path, content) — save your findings as JSON. Always
  write to "examples/accountant-report.json".

Default workflow:

1. Call summarize_project_cost(hours_back=2) for the current cost
   picture.
2. Call find_cost_anomalies(hours_back=2) to surface what's
   unusual.
3. For each anomaly, propose a concrete optimization. The
   recommendation should name what to change (instruction text,
   tool configuration, caching policy) and the expected effect
   (anticipated cost reduction).
4. Call write_report with a dict containing: summary, anomalies,
   recommendations.

Be concise. Quote concrete numbers and trace IDs. Do not narrate
your reasoning at length; the report is the deliverable.
"""


def build_phoenix_mcp_toolset() -> MCPToolset:
    api_key = os.environ.get("PHOENIX_API_KEY_OBSERVED_WRITE")
    host = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")
    project = os.environ.get("PHOENIX_PROJECT_NAME", "agent-accountant")
    if not api_key or not host:
        raise RuntimeError(
            "PHOENIX_API_KEY_OBSERVED_WRITE and PHOENIX_COLLECTOR_ENDPOINT must be set"
        )
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                args=["-y", "@arizeai/phoenix-mcp@latest"],
                env={
                    "PHOENIX_API_KEY": api_key,
                    "PHOENIX_HOST": host,
                    "PHOENIX_PROJECT": project,
                    "PATH": os.environ.get("PATH", ""),
                },
            ),
            timeout=60.0,
        ),
        tool_filter=MCP_DRILL_DOWN_TOOLS,
    )


# A trimmed instruction for the live "Ask the Accountant" panel: answer the
# operator's question directly, and ALWAYS verify against the raw spans by
# pulling at least one trace through the Phoenix MCP `get-trace` tool — the MCP
# server is the load-bearing path for runtime self-introspection.
ASK_INSTRUCTION = """You are the Accountant, answering an operator's question
live about the Helpdesk fleet's cost. The Phoenix project is ALWAYS
"agent-accountant" — never ask the operator for a project, trace, or span id;
obtain them yourself with the tools.

Tools:
- find_cost_anomalies(hours_back) — detected anomalies; each carries real
  example_trace_ids you can drill straight into. Start here when you need a
  trace id you don't already have.
- summarize_project_cost(hours_back) — by-task-class cost summary.
- get-trace(trace_identifier, project_identifier) — the Phoenix MCP tool: a
  trace's raw spans (each span has an id, name, attributes, cost). Use it to
  ground a claim, or to read out a concrete span id.
- list-projects, get-project, get-span-annotations — Phoenix inventory/annotations.

To answer ANY question — even a vague one — gather what you need YOURSELF: call
find_cost_anomalies(hours_back=2) for a real trace id, then get-trace to read its
spans, then answer. If asked for a span id, return a real one from a fetched
trace. Never tell the operator you "need" an id — go fetch it. Use at most two
tools, then answer in 3-5 sentences with concrete numbers and ids from the real
data. Do not write any report file. Be direct."""


def build_agent(
    instruction: str | None = None,
    include_report: bool = True,
    model: str = "gemini-2.5-pro",
    disable_thinking: bool = False,
) -> LlmAgent:
    tools = [build_phoenix_mcp_toolset(), summarize_project_cost, find_cost_anomalies]
    if include_report:
        tools.append(write_report)
    cfg = None
    if disable_thinking:
        # The live panel just reads a trace and explains it — adaptive thinking
        # (on by default for flash) adds many seconds for no quality gain here.
        cfg = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        )
    return LlmAgent(
        name="accountant",
        model=model,
        instruction=instruction or INSTRUCTION,
        tools=tools,
        generate_content_config=cfg,
    )
