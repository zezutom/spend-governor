"""Custom OpenTelemetry span exporter that POSTs spans to the Accountant.

This is the second exporter on the observed agent's TracerProvider — the
first stays Phoenix Cloud (historical store, drill-down), this one fans
out the same spans to the Accountant's real-time ingest endpoint.

Failure semantics: the Accountant's outbox is durable, but this network
hop is best-effort. If the Accountant ingest server is down, the export
fails and OTel logs a warning — we don't block the observed agent.
Phoenix still receives the spans regardless. For an MVP this is
acceptable; production would need a local persistent queue on the
emitter side.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Sequence

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


log = logging.getLogger(__name__)


def _ns_to_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()


def _attr(attrs: dict, *keys):
    for k in keys:
        if k in attrs and attrs[k] not in (None, ""):
            return attrs[k]
    return None


def _serialize_span(span: ReadableSpan) -> dict:
    ctx = span.get_span_context()
    attrs = dict(span.attributes or {})
    parent_id = None
    if span.parent is not None:
        parent_id = format(span.parent.span_id, "016x")

    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_id": parent_id,
        "name": span.name,
        "span_kind_otel": span.kind.name if span.kind else None,
        "start_time": _ns_to_iso(span.start_time),
        "end_time": _ns_to_iso(span.end_time),
        "openinference_kind": _attr(
            attrs,
            "openinference.span.kind",
            "attributes.openinference.span.kind",
        ),
        "tool_name": _attr(attrs, "tool.name", "attributes.tool.name"),
        "output_value": _attr(attrs, "output.value", "attributes.output.value"),
        "prompt_tokens": _attr(attrs, "llm.token_count.prompt"),
        "cached_input_tokens": _attr(
            attrs,
            "llm.token_count.prompt_details.cache_read",
            "llm.token_count.cache_read",
        ),
        "completion_tokens": _attr(attrs, "llm.token_count.completion"),
        "reasoning_tokens": _attr(
            attrs,
            "llm.token_count.completion_details.reasoning",
        ),
        "model_name": _attr(attrs, "llm.model_name"),
    }


class AccountantHTTPExporter(SpanExporter):
    """POSTs spans as JSON to the Accountant ingest server."""

    def __init__(self, endpoint: str, timeout: float = 2.0):
        self._endpoint = endpoint.rstrip("/") + "/ingest"
        self._client = httpx.Client(timeout=timeout)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        payload = {"spans": [_serialize_span(s) for s in spans]}
        try:
            resp = self._client.post(self._endpoint, json=payload)
            if resp.status_code >= 300:
                log.warning(
                    "accountant export non-2xx %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return SpanExportResult.FAILURE
            return SpanExportResult.SUCCESS
        except Exception as e:
            log.warning("accountant export failed: %s", e)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._client.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def build_accountant_exporter() -> AccountantHTTPExporter | None:
    """Return an exporter if ACCOUNTANT_INGEST_URL is set, else None."""
    endpoint = os.environ.get("ACCOUNTANT_INGEST_URL")
    if not endpoint:
        log.warning(
            "ACCOUNTANT_INGEST_URL is not set — observed-agent spans will "
            "only go to Phoenix, not the Accountant. Set the env var "
            "(e.g. http://localhost:8765) to enable the live fan-out."
        )
        print(
            "[telemetry] ACCOUNTANT_INGEST_URL not set; Accountant fan-out disabled.",
            flush=True,
        )
        return None
    print(f"[telemetry] Accountant fan-out → {endpoint}", flush=True)
    return AccountantHTTPExporter(endpoint=endpoint)
