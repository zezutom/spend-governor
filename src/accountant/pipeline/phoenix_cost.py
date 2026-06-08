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
    """Resolve the Phoenix project node id (e.g. 'UHJvamVjdDo0') for building
    span deeplinks. Stable per project — callers should cache."""
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
    """Phoenix UI URL that opens a trace with one span selected — the exact
    shape the spans table builds for a row. node_id is the span's Phoenix
    node id (`spans.phoenix_node_id`); without it we still open the trace."""
    ui_base = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").rstrip("/")
    if not (ui_base and project_gid and trace_id):
        return None
    base = f"{ui_base}/projects/{project_gid}/spans/{trace_id}"
    if not node_id:
        return base
    # URL-encode the node id — it's base64 ('==' padding); Phoenix's router drops
    # the param if the '=' isn't percent-encoded, falling back to the whole trace.
    from urllib.parse import quote
    return f"{base}?selectedSpanNodeId={quote(node_id, safe='')}"


_EXPERIMENT_COST_QUERY = """
query($id:ID!){ node(id:$id){ ... on Experiment {
  name runCount averageRunLatencyMs
  costSummary{ total{cost tokens} prompt{cost tokens} completion{cost tokens} }
}}}
"""


def experiment_cost(experiment_gid: str, *, timeout: float = 30.0) -> dict | None:
    """Read a Phoenix experiment's aggregate cost (refactor #3). Phoenix
    computes `Experiment.costSummary` from the runs' traced LLM spans — so
    this number is Phoenix's own, the parity-clean basis for the savings
    delta. Returns {name, run_count, total_cost_usd, total_tokens} or None."""
    url, key = _endpoint_and_key()
    try:
        r = httpx.post(
            url,
            json={"query": _EXPERIMENT_COST_QUERY, "variables": {"id": experiment_gid}},
            headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
            timeout=timeout,
        )
        r.raise_for_status()
        node = (r.json().get("data") or {}).get("node")
        if not node:
            return None
        total = (node.get("costSummary") or {}).get("total") or {}
        return {
            "name": node.get("name"),
            "run_count": node.get("runCount"),
            "total_cost_usd": total.get("cost"),
            "total_tokens": total.get("tokens"),
        }
    except Exception:
        return None


def compare_url(dataset_gid: str, *experiment_gids: str) -> str | None:
    """Phoenix compare-page URL for one or more experiments (baseline +
    governed → side-by-side cost delta). URL-addressable via repeated
    `experimentId=` params (confirmed from Phoenix's own emitted links)."""
    ui = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").rstrip("/")
    gids = [g for g in experiment_gids if g]
    if not (ui and dataset_gid and gids):
        return None
    qs = "&".join(f"experimentId={g}" for g in gids)
    return f"{ui}/datasets/{dataset_gid}/compare?{qs}"


def annotate_savings(spans: list[dict]) -> list[str]:
    """Tag each saving span in Phoenix with an `accountant.savings` annotation
    (label cache_hit / model_swap, score = saved USD, explanation), so the
    intervention is visible in the trace view's Annotations tab.

    spans: [{span_id (OTEL hex), kind ('cache hit'|'model downgrade'),
    savings_usd}]. Returns the span_ids successfully tagged. Idempotent on
    Phoenix's side (annotation upserts by span_id + name)."""
    if not spans:
        return []
    # phoenix.client reads PHOENIX_API_KEY; mirror analysis.py's key fallback.
    if "PHOENIX_API_KEY_OBSERVED_WRITE" in os.environ:
        os.environ.setdefault("PHOENIX_API_KEY", os.environ["PHOENIX_API_KEY_OBSERVED_WRITE"])
    try:
        from phoenix.client import Client
        sp = Client().spans
    except Exception:
        return []
    labels = {
        "cache hit": ("cache_hit", "Served a semantic-cache hit; avoided the paid tool call"),
        "model downgrade": ("model_swap", "Routed to a cheaper model"),
    }
    done: list[str] = []
    for s in spans:
        label, expl = labels.get(s.get("kind"), (s.get("kind") or "saving", "Wrapper intervention"))
        saved = float(s.get("savings_usd") or 0)
        try:
            sp.add_span_annotation(
                span_id=s["span_id"], annotation_name="accountant.savings",
                annotator_kind="CODE", label=label, score=round(saved, 6),
                explanation=f"{expl} — saved ${saved:.6f}.", sync=False,
            )
            done.append(s["span_id"])
        except Exception:
            pass
    return done


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
