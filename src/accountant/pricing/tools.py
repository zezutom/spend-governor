"""Per-call costs for the observed agent's tools.

These are placeholder defaults chosen so the refund web_search cost is
visible above the LLM cost floor: web_search is the only external API
in the toolkit and is priced an order of magnitude higher than internal
calls, so three redundant web_search calls per refund show up as a
clear cost delta.

The internal Stratus Forms APIs (kb_lookup, refund_api, ticket_update,
customer_lookup, escalate_human) are priced at trivial fixed rates that
stand in for the marginal cost of a synchronous internal RPC.

task_classifier is local deterministic Python (no I/O, no LLM) so its
per-call cost is zero.

Adjust here when real pricing data is available — cost.py and downstream
aggregation read TOOL_PRICES, so no magic numbers leak elsewhere.
"""


TOOL_PRICES: dict[str, float] = {
    "task_classifier": 0.0,
    "kb_lookup": 0.0001,
    "web_search": 0.005,
    "customer_lookup": 0.0001,
    "refund_api": 0.001,
    "ticket_update": 0.0001,
    "escalate_human": 0.0001,
}
