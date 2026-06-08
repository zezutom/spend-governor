"""Parity guard for the Phase 2 extraction.

The service layer must return exactly what the v5 dashboard showed. This
asserts the service economics/projection/tool-rate outputs equal the archived
v5 functions on the same live inputs (proving the lift was faithful, not a
reimplementation), pins the deterministic trace-pair totals, and confirms the
savings passthrough is identity.

    uv run python -m governor.service._parity
"""

from governor import service
from governor.pipeline.db import savings_summary
from governor.trace_race.align import align, load_spans
from governor.ui._archive import dashboard_v5 as v5


# Deterministic trace pair (fixed cache ids) — captured from v5.
_BASE_TID = "0074d533b658fab74f9b03d8b5be0624"
_GOV_TID = "068c00f48afcdc73854ea5a7e9b085b6"
_PIN_PAIR = {"baseline_total_usd": 0.020501, "governed_total_usd": 0.005093,
             "saved_usd": 0.015408, "skipped_calls": 3}


def check() -> None:
    live = service.live_state()
    recs = service.recommendations()
    rates = service.default_tool_rates()

    # economics: service == archived v5, same inputs
    s_rows, s_tot = service.cost_breakdown(live, recs, rates)
    v_rows, v_tot = v5._issue_rows(live, recs, rates)
    assert s_rows == v_rows, "per-class cost rows drifted from v5"
    assert s_tot == v_tot, "totals drifted from v5"
    assert service.default_monthly_volume(live, recs) == v5._default_monthly_tickets(live, recs), \
        "volume projection base drifted"
    assert service.default_tool_rates() == v5._default_tool_rates(), "tool rates drifted"
    assert service.class_reasons(recs) == v5._class_reasons(recs), "diagnosis text drifted"

    # deterministic trace pair
    a = align(load_spans(_BASE_TID), load_spans(_GOV_TID))
    for k, want in _PIN_PAIR.items():
        assert a[k] == want, f"trace pair {k}: {a[k]} != pinned {want}"

    # savings passthrough is identity
    assert service.realized_savings() == savings_summary(), "savings passthrough diverged"

    print("PARITY OK — economics, projection, tool rates, diagnosis, trace pair, savings "
          "all match v5.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    check()
