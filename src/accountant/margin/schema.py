"""Structured output schema for the Margin Agent.

The agent is forced to emit exactly this shape via Gemini's
`response_schema`, so the output is validated at the model boundary — no
parsing, no free text. Every numeric field is copied verbatim from the
agent's INPUT (the agent performs no arithmetic); the schema only fixes
the structure it must return them in.
"""

from typing import Literal, Optional

from pydantic import BaseModel


# --- Job 1: credit tiering ------------------------------------------------

class Tier(BaseModel):
    credits: int
    task_types: list[str]
    # Set when a boundary task was rounded toward the customer's favor.
    rounding_note: Optional[str] = None


class FlatCreditRisk(BaseModel):
    task_type: str
    underpriced_factor: float
    statement: str


class CreditTiering(BaseModel):
    cost_basis: str
    credit_value_usd_raw_cost: float
    credit_value_usd_governed_cost: float
    cost_basis_note: str
    tiers: list[Tier]
    flat_credit_risk: FlatCreditRisk
    rate_sensitive: bool
    caveats: list[str]


# --- Job 2: margin-drift triage -------------------------------------------

class RejectedLever(BaseModel):
    lever: str
    projected_margin_after: float
    bill_change_pct: float
    lever_recovers_pts: float


class DriftRecommendation(BaseModel):
    segment_id: str
    raw_margin: float
    governed_margin: float
    governance_recovered_pts: float
    recommended_lever: Literal["policy", "reprice"]
    policy_id: Optional[str] = None
    projected_margin_after: float
    lever_recovers_pts: float
    bill_change_pct: float
    residual_gap: float
    rejected_lever: RejectedLever
    rate_sensitive: bool
    rationale: str


# --- Blocked items --------------------------------------------------------

class Blocked(BaseModel):
    item: str
    reason: Literal["missing_input", "out_of_scope"]


class MarginOutput(BaseModel):
    credit_tiering: CreditTiering
    drift_recommendations: list[DriftRecommendation]
    blocked: list[Blocked]
