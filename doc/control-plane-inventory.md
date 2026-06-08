# Control-Plane Migration — Phase 1 Inventory

Transition from the v5 Streamlit dashboard to the conversational AI Cost
Governance control plane. **Decision (fixed): rebuild the UI from scratch,
preserve ALL backend.** This doc inventories the reusable backend, marks the
seam, and records the archive + parity plan, before any code is touched.

## Headline finding

`import streamlit` appears in **exactly one file**: `src/accountant/ui/dashboard.py`.
The entire backend (`pipeline`, `analytics`, `wrapper`, `pricing`, `trace_race`)
is Streamlit-free. The only entanglement is **view-model compute defined inside
the dashboard** (Streamlit-free in logic, but living in the UI file). Phase 2
lifts that compute into a service layer; nothing else needs untangling.

## Reusable backend — reuse as-is (do not delete, do not reimplement)

| Capability | Lives in | Public entry points |
|---|---|---|
| Phoenix read path | `pipeline/backfill.py`, `pipeline/worker.py`, `pipeline/phoenix_cost.py` | `reconcile_from_phoenix`, `fetch_span_costs`, `project_gid`, `span_deeplink` |
| Cost reconciliation (Phoenix LLM + tool count × rate) | `pricing/{cost,gemini,tools}.py`, `pipeline/worker.py` | `compute_trace_cost`, `MODELS`, `TOOL_PRICES`, `_refresh_state` |
| Policy engine (cache + model-routing levers) | `wrapper/{wrapper,cache,store}.py` | `wrap_tools`, `set_policy_override`, `store.{active_policies,is_active,activate_policy,deactivate_policy,policy_activated_at}` |
| Trace-pair / before-after | `analytics/verification.py`, `trace_race/{align,capture,fixture}.py` | `measured_before_after`, `align`, `capture_pair`, `build_fixture`/`load_fixture` |
| Savings & intervention aggregation | `pipeline/db.py` | `savings_summary`, `policy_savings_series`, `policy_saving_spans`, `class_cost_stats`, `class_trace_costs`, `representative_saving_span` |
| Detection / recommendations | `analytics/{detection,savings,recommendations,reasoning}.py` | `run_detection`, `build_issues`, recommendation rows in `db` |
| Live state | `pipeline/db.py`, `pipeline/worker.py` | `get_meta('live_state')`, `_refresh_state` |

## The seam — view-model compute trapped in `ui/dashboard.py`

These functions return data (no `st.`), but are defined in the UI file. Phase 2
moves them verbatim into the service layer — **a move, not a reimplementation**:

`_issue_rows`, `_issue_of`, `_policy_for_issue`, `_affected_classes`,
`_class_reasons`, `_default_tool_rates`, `_tool_cost`, `_observed_hours`,
`_default_monthly_tickets`, `_load_recommendations`, `_load_live_state`,
`_cache_span_count`, `_project_gid`, and the `BASELINE_CLASS` constant.

They produce: per-class cost rows + totals, the volume→projection, the tool-cost
decomposition, the issue→policy lever mapping, and the live-state/recs loaders.

## Archive

`src/accountant/ui/dashboard.py` → `src/accountant/ui/_archive/dashboard_v5.py`
(git move, not delete). The only archival adaptation is guarding the trailing
`main()` call behind `if __name__ == "__main__"` so the module's view-model
functions can be imported for the parity check without launching the app. The
source stays diffable; to *run* v5 for visual parity, check out a pre-archive
commit (its `__file__`-relative `LOG_PATH` assumes the original depth).

## Parity pin (Phase 2 guards these — captured from v5 on the current cache)

The service layer must reproduce these exactly. The trace-pair totals are
deterministic (fixed cache trace ids); the class-cost/projection figures are
checked by asserting the service output equals the archived-v5 output on the
same live `live_state` + recommendations.

- `default_monthly_tickets`: 15712
- totals: cost/ticket 0.007123 · recoverable/ticket 0.005262 · pct_avoidable 0.738736 · total_n 1037
- class cost (cost · llm · tool · ×baseline):
  - refund_handling 0.019311 · 0.00403 · 0.015281 · 11.843
  - account_question 0.004436 · 0.00307 · 0.001365 · 2.72
  - password_reset 0.001631 · 0.00144 · 0.000191 · 1.0
  - plan_change 0.003012 · 0.0027 · 0.000312 · 1.847
  - unknown 0.013165 · 0.00302 · 0.010145 · 8.074
- realized savings 0.895865 · interventions 607 (live; pinned at capture)
- trace pair (`0074d533…` baseline vs `068c00f4…` governed): baseline 0.020501 ·
  governed 0.005093 · saved 0.015408 · 3 skipped

## Old surface → new home (Phase 3, per the product brief)

- policy toggles → the levers the optimizer agent enacts
- savings / intervention counts → intervention feed + forecast
- volume → projection → forecast
- trace race + per-class waste + verify tables → the proof drill-down
