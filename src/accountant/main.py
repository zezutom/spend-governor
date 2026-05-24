import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from google.adk.runners import InMemoryRunner
from google.genai import types

from accountant.agent import build_agent


APP_NAME = "accountant"
USER_ID = "dev"
DEFAULT_QUESTION = (
    "Analyze the last 24 hours of traces in the agent-accountant "
    "project. Compute the cost picture, find anomalies, propose "
    "optimizations, and write the report."
)


async def run_once(question: str) -> None:
    agent = build_agent()
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )
    content = types.Content(role="user", parts=[types.Part(text=question)])
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=content,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(part.text)
                elif part.function_call:
                    args = dict(part.function_call.args) if part.function_call.args else {}
                    print(f"[tool] {part.function_call.name}({args})")
                elif part.function_response:
                    print(f"[result] {part.function_response.name}")


def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    asyncio.run(run_once(question))


if __name__ == "__main__":
    main()
