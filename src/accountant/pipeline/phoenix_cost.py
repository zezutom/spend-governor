"""Read Phoenix's computed cost at scale via GraphQL (refactor #2).

Phoenix computes LLM **actual** cost (`costSummary`) from token attributes
+ its pricing table, and stores our `accountant.*` span attributes. Neither
is exposed by the `phoenix-client` REST API — both live only in Phoenix's
GraphQL. This module cursor-paginates the project's spans connection and
returns per-span cost so the pipeline can make Phoenix the source of truth
for actual LLM cost and realized savings, retiring the worker's local
compute to an instant fallback that the reconcile pass overwrites.

One sweep returns everything we need per span:
- `costSummary.total.cost` → Phoenix's actual LLM cost (None for tool spans
  Phoenix doesn't price, or LLM spans Phoenix hasn't costed yet);
- `attributes.accountant.cost.savings_usd` → realized saving on that span
  (model_swap or cache_hit), the headline "Saved so far" re-derived from
  Phoenix rather than our private interventions log.
"""

import json
import os

import httpx


_SPANS_QUERY = """
query($p:String!, $after:String, $first:Int!){
  getProjectByName(name:$p){
    spans(first:$first, after:$after, sort:{col:startTime, dir:desc}){
      pageInfo{ hasNextPage endCursor }
      edges{ node{
        id spanId spanKind
        trace{ traceId }
        costSummary{ total{ cost } }
        attributes
      }}
    }
  }
}
"""


def _endpoint_and_key() -> tuple[str, str]:
    base = os.environ["PHOENIX_COLLECTOR_ENDPOINT"].rstrip("/")
    # Prefer a dedicated read key; fall back to the write key like analysis.py.
    key = (
        os.environ.get("PHOENIX_API_KEY_ACCOUNTANT_READ")
        or os.environ["PHOENIX_API_KEY_OBSERVED_WRITE"]
    )
    return base + "/graphql", key


def _accountant_savings(attributes) -> float:
    """Pull accountant.cost.savings_usd out of the span's attributes blob
    (Phoenix returns `attributes` as a JSON string)."""
    if isinstance(attributes, str):
        try:
            attributes = json.loads(attributes)
        except (TypeError, ValueError):
            return 0.0
    if not isinstance(attributes, dict):
        return 0.0
    cost = (attributes.get("accountant") or {}).get("cost") or {}
    try:
        return float(cost.get("savings_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def project_gid(project: str | None = None, *, timeout: float = 15.0) -> str | None:
    """Resolve the Phoenix GraphQL node id of the project (e.g. 'UHJvamVjdDo0'),
    needed to build span deeplinks. Stable per project — callers should cache."""
    project = project or os.environ.get("PHOENIX_PROJECT_NAME")
    if not project:
        return None
    url, key = _endpoint_and_key()
    try:
        resp = httpx.post(
            url,
            json={"query": "query($n:String!){ getProjectByName(name:$n){ id } }",
                  "variables": {"n": project}},
            headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return ((resp.json().get("data") or {}).get("getProjectByName") or {}).get("id")
    except Exception:
        return None


def span_deeplink(project_gid: str, trace_id: str, node_id: str | None) -> str | None:
    """Build the Phoenix UI URL that opens a trace with one span selected —
    the exact shape the Phoenix spans table builds for a row. node_id is the
    span's Phoenix node id (`spans.phoenix_node_id`); without it we still
    open the trace (the span is visible in the tree)."""
    ui_base = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").rstrip("/")
    if not (ui_base and project_gid and trace_id):
        return None
    base = f"{ui_base}/projects/{project_gid}/spans/{trace_id}"
    return f"{base}?selectedSpanNodeId={node_id}" if node_id else base


def fetch_span_costs(
    project: str | None = None,
    *,
    first: int = 500,
    max_spans: int = 4000,
    timeout: float = 90.0,
) -> list[dict]:
    """Cursor-paginate the project's spans newest-first, returning a list of
    {span_id, span_kind, phoenix_cost_usd, savings_usd}.

    Walks at most `max_spans` (newest-first) so a recurring reconcile is
    bounded — fresh traffic is always covered, and Phoenix's cost-compute
    lag settles within a sweep or two. Raises on a GraphQL error so the
    caller can log and keep the existing (local) cost in place.
    """
    project = project or os.environ["PHOENIX_PROJECT_NAME"]
    url, key = _endpoint_and_key()
    headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}

    out: list[dict] = []
    after: str | None = None
    with httpx.Client(timeout=timeout) as client:
        while len(out) < max_spans:
            resp = client.post(
                url,
                json={"query": _SPANS_QUERY,
                      "variables": {"p": project, "after": after, "first": first}},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(f"Phoenix GraphQL error: {data['errors']}")
            conn = ((data.get("data") or {}).get("getProjectByName") or {}).get("spans") or {}
            for edge in conn.get("edges") or []:
                node = edge["node"]
                total = (node.get("costSummary") or {}).get("total") or {}
                out.append({
                    "span_id": node["spanId"],
                    "span_kind": node.get("spanKind"),
                    "phoenix_node_id": node.get("id"),
                    "phoenix_cost_usd": total.get("cost"),
                    "savings_usd": _accountant_savings(node.get("attributes")),
                })
                if len(out) >= max_spans:
                    break
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
    return out
