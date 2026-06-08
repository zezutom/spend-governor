import os

from opentelemetry import trace
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from phoenix.otel import register

from observed.accountant_exporter import build_accountant_exporter


_INITED = False


def init_telemetry() -> None:
    # Idempotent: OTel refuses to override the global TracerProvider on a second
    # register() ("Overriding ... not allowed"), which leaves span export broken.
    # Callers (run_replay_lab, iter_lab_rows, generate_dataset) may each call this,
    # so register exactly once per process.
    global _INITED
    if _INITED:
        return
    _INITED = True
    api_key = os.environ.get("PHOENIX_API_KEY_OBSERVED_WRITE")
    if api_key:
        os.environ["PHOENIX_API_KEY"] = api_key

    project_name = os.environ.get("PHOENIX_PROJECT_NAME", "agent-accountant")
    tracer_provider = register(project_name=project_name, auto_instrument=True)

    # If ACCOUNTANT_INGEST_URL is set, fan-out the same spans to the
    # Accountant's real-time ingest endpoint as a SECOND processor.
    # Crucial: replace_default_processor=False — Phoenix's TracerProvider
    # otherwise removes its own exporter when you add one, which would
    # stop traces reaching Phoenix entirely. The observed agent must keep
    # emitting to Phoenix (it's the system of record / trace-proof).
    extra_exporter = build_accountant_exporter()
    if extra_exporter is not None:
        provider = trace.get_tracer_provider()
        provider.add_span_processor(
            BatchSpanProcessor(extra_exporter),
            replace_default_processor=False,
        )
