import os

from phoenix.otel import register


def init_telemetry() -> None:
    api_key = os.environ.get("PHOENIX_API_KEY_OBSERVED_WRITE")
    if api_key:
        os.environ["PHOENIX_API_KEY"] = api_key

    project_name = os.environ.get("PHOENIX_PROJECT_NAME", "agent-accountant")
    register(project_name=project_name, auto_instrument=True)
