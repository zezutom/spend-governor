"""Margin Agent — turns measured cost + an operator price into pricing
mechanics (credit tiering) and defends gross margin (drift triage)."""

from accountant.margin.agent import DEFAULT_MARGIN_MODEL, run_margin
from accountant.margin.schema import MarginOutput

__all__ = ["run_margin", "MarginOutput", "DEFAULT_MARGIN_MODEL"]
