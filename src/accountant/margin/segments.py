"""Customer segments for margin-drift triage — behind a provider seam.

The drift-triage job needs business facts the cost pipeline does not
measure: a segment's price per ticket, its usage mix, which policies
already govern it, and whether it is near renewal. Those come from a
`SegmentProvider`. Today the only implementation is `FixtureProvider`
(curated synthetic segments, clearly labelled). When per-customer trace
attribution exists, a `TraceDerivedProvider` can implement the same
interface — usage_mix measured from customer_id traffic, the rest still
operator-supplied — and replace the fixtures WITHOUT touching the builder
or the agent. The INPUT builder computes every margin and lever from
these facts + live cost; a provider supplies facts only, never arithmetic.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SegmentSpec:
    """Business facts about one customer segment — the inputs to the
    margin math, not the math itself."""

    segment_id: str
    price_per_ticket_usd: float
    usage_mix: dict[str, float]          # task_type -> fraction of this segment's tickets (sums ~1)
    near_renewal: bool
    # Policies already governing THIS segment (signatures). Per-segment so
    # an available-but-not-yet-applied policy can be offered as a drift
    # lever even when it is active elsewhere.
    active_policy_ids: list[str] = field(default_factory=list)
    synthetic: bool = True               # honesty flag — surfaced in the UI
    note: str = ""                       # why this fixture exists (the contrast it demonstrates)


@runtime_checkable
class SegmentProvider(Protocol):
    def segments(self) -> list[SegmentSpec]:
        ...


class FixtureProvider:
    """Curated synthetic segments, each chosen to drive a DIFFERENT drift
    outcome so the triage logic is visible:

    - acme    — refund-heavy, near renewal, refund-cache not yet applied →
                a cost policy restores margin with no bill change (policy wins).
    - globex  — structurally under-priced, all relevant policies already on →
                only reprice can reach target (reprice wins, by necessity).
    - initech — priced healthily, already above target → no recommendation
                (demonstrates the below-target filter).

    Numbers here are business facts only (price, mix, renewal); the builder
    computes margins and levers from live measured cost.
    """

    def segments(self) -> list[SegmentSpec]:
        return [
            SegmentSpec(
                segment_id="acme",
                price_per_ticket_usd=0.024,
                usage_mix={"refund_handling": 0.62, "account_question": 0.20, "password_reset": 0.18},
                near_renewal=True,
                active_policy_ids=["route_model:simple", "cache_tool:kb_lookup"],
                note="Refund-heavy + near renewal; refund web_search cache not yet on for them.",
            ),
            SegmentSpec(
                segment_id="globex",
                price_per_ticket_usd=0.006,
                usage_mix={"refund_handling": 0.62, "account_question": 0.20, "password_reset": 0.18},
                near_renewal=False,
                active_policy_ids=["route_model:simple", "cache_tool:kb_lookup", "cache_tool:web_search"],
                note="Refund-heavy but priced near cost with every policy already on; only reprice can reach target.",
            ),
            SegmentSpec(
                segment_id="initech",
                price_per_ticket_usd=0.020,
                usage_mix={"refund_handling": 0.05, "account_question": 0.25, "password_reset": 0.70},
                near_renewal=False,
                active_policy_ids=["route_model:simple", "cache_tool:kb_lookup", "cache_tool:web_search"],
                note="Reset-heavy and priced well; already above target — should yield no lever.",
            ),
        ]


def default_provider() -> SegmentProvider:
    return FixtureProvider()
