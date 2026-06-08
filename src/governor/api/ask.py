"""Live "Ask the Governor" — runs the real ADK agent with the Phoenix MCP
toolset and streams its tool-calls + answer.

This is the load-bearing MCP path: on an operator's question the Governor
agent introspects its OWN operational data in Phoenix at runtime by calling the
Phoenix MCP server (get-trace, list-projects, get-span-annotations, …) through
its ADK tool-loop. The cockpit renders each MCP call as a visible step, so the
agentic loop and the MCP integration are both on screen — not buried in a
plain-Python GraphQL read.
"""

import asyncio
import json
import os

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types

from governor.agent import ASK_INSTRUCTION, MCP_DRILL_DOWN_TOOLS, build_agent

APP_NAME = "governor-ask"
USER_ID = "operator"
_MCP_TOOLS = set(MCP_DRILL_DOWN_TOOLS)
# Flash keeps the live panel snappy; pro is overkill for "fetch a trace and
# explain it". Override with GOVERNOR_ASK_MODEL if you want pro for the demo.
ASK_MODEL = os.environ.get("GOVERNOR_ASK_MODEL", "gemini-2.5-flash")

# One warmed runner (and its MCP subprocess) reused across questions; each
# question gets its own session, so concurrent asks don't share state.
_runner: InMemoryRunner | None = None
_runner_lock = asyncio.Lock()


async def _get_runner() -> InMemoryRunner:
    global _runner
    if _runner is None:
        async with _runner_lock:
            if _runner is None:
                agent = build_agent(instruction=ASK_INSTRUCTION, include_report=False,
                                    model=ASK_MODEL, disable_thinking=True)
                _runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    return _runner


def _compact(obj, limit: int = 700) -> str:
    """A short, human-legible summary of a tool result for the transcript."""
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    return s if len(s) <= limit else s[:limit] + " …"


async def ask_stream(question: str, session_id: str | None = None):
    """Async-generate step dicts as the agent works:

    {"type": "session",     "session_id"}      # the conversation id (reuse for follow-ups)
    {"type": "tool_call",   "name", "args", "mcp": bool}
    {"type": "tool_result", "name", "summary", "mcp": bool}
    {"type": "text",        "text"}            # answer chunk(s)
    {"type": "done"} | {"type": "error", "error"}

    Pass a prior session_id to continue the SAME conversation — the agent then
    remembers earlier turns (e.g. "the LLM call you just flagged").
    """
    try:
        runner = await _get_runner()
        session = None
        if session_id:
            try:
                session = await runner.session_service.get_session(
                    app_name=APP_NAME, user_id=USER_ID, session_id=session_id
                )
            except Exception:
                session = None
        if session is None:
            session = await runner.session_service.create_session(
                app_name=APP_NAME, user_id=USER_ID
            )
        yield {"type": "session", "session_id": session.id}
        content = types.Content(role="user", parts=[types.Part(text=question)])
        streamed_text = False  # de-dup: SSE mode emits text deltas THEN a final
                               # aggregated copy; emit the deltas, drop the repeat.
        async for event in runner.run_async(
            user_id=USER_ID, session_id=session.id, new_message=content,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            if not (event.content and event.content.parts):
                continue
            is_partial = bool(getattr(event, "partial", False))
            for part in event.content.parts:
                if part.text:
                    if is_partial:
                        streamed_text = True
                        yield {"type": "text", "text": part.text}
                    elif not streamed_text:  # non-streaming fallback / first emit
                        yield {"type": "text", "text": part.text}
                elif part.function_call:
                    name = part.function_call.name
                    args = dict(part.function_call.args) if part.function_call.args else {}
                    yield {"type": "tool_call", "name": name, "args": args,
                           "mcp": name in _MCP_TOOLS}
                elif part.function_response:
                    name = part.function_response.name
                    yield {"type": "tool_result", "name": name,
                           "mcp": name in _MCP_TOOLS,
                           "summary": _compact(part.function_response.response)}
        yield {"type": "done"}
    except Exception as e:  # surface, don't crash the stream
        yield {"type": "error", "error": f"{type(e).__name__}: {e}"}


def durable_trace_id(agent_id: str) -> str | None:
    """A real, durable trace id for an agent from the local corpus — one that
    reliably still resolves in Phoenix — so a seeded investigation's get-trace
    call always lands on real span data. Prefers the trace with the MOST tool
    calls, so the agent inspects one that actually exhibits the signature waste
    (e.g. the repeated web_search) rather than an arbitrary quiet one."""
    try:
        from governor.pipeline import db
        with db.connect() as con:
            row = con.execute(
                "SELECT w.trace_id, COUNT(*) AS n FROM spans w JOIN ("
                "  SELECT trace_id FROM spans"
                "  WHERE tool_name='task_classifier' AND classifier_task_class=?"
                ") c ON w.trace_id=c.trace_id "
                "WHERE w.tool_name IS NOT NULL AND w.tool_name <> 'task_classifier' "
                "GROUP BY w.trace_id ORDER BY n DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
            if row and row[0]:
                return row[0]
            # fallback: any trace for the agent
            row = con.execute(
                "SELECT trace_id FROM spans "
                "WHERE tool_name='task_classifier' AND classifier_task_class=? "
                "AND trace_id IS NOT NULL LIMIT 1",
                (agent_id,),
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def seed_question(agent_id: str, label: str, waste: str | None) -> str:
    """The seeded investigation prompt for an agent's 'investigate' button.

    get-trace-first: the agent goes straight to the Phoenix MCP server on a real,
    durable trace for this agent — fast (one or two MCP calls, no slow window
    scans) and coherent with the cockpit's finding. Falls back to the aggregate
    tools only when no durable trace id is on hand."""
    tid = durable_trace_id(agent_id)
    waste_line = f" The suspected waste is \"{waste}\"." if waste else ""
    if tid:
        return (
            f"Investigate the {label} agent. Call get-trace exactly once for trace "
            f"{tid} (project agent-accountant) and inspect its spans — the tool "
            f"calls, how many times each fired, the model, and any costs."
            f"{waste_line} Confirm from the raw spans whether that waste is real, "
            f"then answer in 3-5 sentences citing the trace id and concrete details "
            f"(which tool repeated and how many times)."
        )
    return (
        f"Investigate the {label} agent's cost. Call find_cost_anomalies(hours_back=2), "
        f"then get-trace (the Phoenix MCP tool) on one of its example_trace_ids to see "
        f"the raw spans.{waste_line} Answer in 3-5 sentences citing the trace id."
    )
