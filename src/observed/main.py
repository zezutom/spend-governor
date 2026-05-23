import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from observed.telemetry import init_telemetry

init_telemetry()

from google.adk.runners import InMemoryRunner
from google.genai import types

from observed.agent import build_agent


APP_NAME = "agent-accountant"
USER_ID = "dev"
DEFAULT_MESSAGE = "I want a refund for last month's charge."


async def run_once(message: str) -> None:
    agent = build_agent()
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )
    content = types.Content(role="user", parts=[types.Part(text=message)])
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
    message = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MESSAGE
    asyncio.run(run_once(message))


if __name__ == "__main__":
    main()
