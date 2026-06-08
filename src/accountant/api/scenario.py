"""The scripted-world clock — the demo's ONLY artifice, and it's disclosed.

The WORLD is scripted: a seeded beat schedule decides WHAT cost problem surfaces
and WHEN, on a time-compressed clock (3 min on stage ≈ 12h of traffic). The AGENT
is real — when a beat releases, the real governor genuinely diagnoses, reveals the
real pre-run eval, and renders the real verdict. Same seed → same beats at the same
compressed offsets → the same real verdicts (the evals are pre-run and deterministic;
cost is deterministic from the corpus). The clock is the only thing accelerated, and
the cockpit says so ("last 12h · time-compressed").

This module is PURE: clock math + the schedule, no async, no service I/O, no RNG.
The governor owns all I/O and reacts to `live_beats(now)` / `released_sigs(now)`.
"""

import math
import os
import time

# Wall-clock length of the whole demo window (carries COMPRESSED_WINDOW_HOURS of
# traffic). Override per run with ACCOUNTANT_WALL_WINDOW_SEC to spread the traffic out
# — e.g. 240 for a calmer ~4-min pass, 80 for the original fast one. SPEEDUP derives
# from it, so the clock, beats, arrival curve, and spine all stretch proportionally.
WALL_WINDOW_SEC = float(os.environ.get("ACCOUNTANT_WALL_WINDOW_SEC", "240"))
COMPRESSED_WINDOW_HOURS = 12.0   # ≈ 12h of traffic compressed into the window
SPEEDUP = (COMPRESSED_WINDOW_HOURS * 3600.0) / WALL_WINDOW_SEC  # window-derived speedup
_DAY_START_HOUR = 8.0            # the clock label reads as a business day from 08:00

# Economy routing drops the LLM portion to ~1/5 (flash-lite vs flash) — the SAME
# basis the replay lab shows (_lab_cost: projected = tool + llm×0.2). So a route
# saves llm×0.8 per ticket. Real, documented, and consistent across every surface.
ROUTE_ECON_DROP = 0.8

# The DEMO_ARC — four beats, each a DIFFERENT experience, every one on a REAL lever
# and (for the routes) a REAL pre-run eval. The agent acts alone on the safe one,
# defers the two it can't prove, and the human springs the trap on the last.
#   eval_key — the QUICK eval popup (data/evals/<key>.json): 'hold' holds, 'trip'
#              breaks, None → the evidence is the replay LAB (/api/lab/<use_case>).
DEMO_ARC = (
    {"id": "beat-1", "at_h": 0.7, "kind": "act_alone", "use_case": "support_copilot",
     "lever": "cache_tool:support_copilot", "lever_type": "cache_tool", "eval_key": None,
     "headline": "Support Co-Pilot — redundant searches",
     "trigger": "web_search ×3 redundant per ticket"},
    {"id": "beat-2", "at_h": 2.5, "kind": "act_alone", "use_case": "refund_auditor",
     "lever": "limit_tool_calls:refund_auditor", "lever_type": "limit_tool_calls", "eval_key": None,
     "headline": "Refund Auditor — verification loop",
     "trigger": "same web_search ×5 (identical loop)"},
    {"id": "beat-3", "at_h": 5.0, "kind": "defer", "use_case": "sales_assistant",
     "lever": "suppress_tool:sales_assistant", "lever_type": "suppress_tool", "eval_key": None,
     "headline": "Sales Assistant — needless search",
     "trigger": "competitor search the KB already covers"},
    {"id": "beat-4", "at_h": 7.5, "kind": "trap", "use_case": "docs_bot",
     "lever": "route_model:docs_bot", "lever_type": "route_model", "eval_key": "trip",
     "headline": "Docs Bot — over-powered model",
     "trigger": "premium model on trivial how-to Q&A"},
)
_EXTRA_SAFE_AT: dict[str, float] = {}  # all fixes are scheduled as beats now

SCHEDULES = {0: DEMO_ARC}


class Scenario:
    """A seeded schedule + a compressed clock. Reproducible: the seed selects the
    schedule; with no RNG anywhere, same seed → identical beats and volume curve."""

    def __init__(self, seed: int = 0, t0: float | None = None):
        self.seed = seed
        self.beats = SCHEDULES.get(seed, DEMO_ARC)
        self.t0 = t0 if t0 is not None else time.monotonic()

    # --- clock -------------------------------------------------------------
    def elapsed(self, now: float) -> float:
        return max(0.0, now - self.t0)

    def compressed_seconds(self, now: float) -> float:
        return min(self.elapsed(now) * SPEEDUP, COMPRESSED_WINDOW_HOURS * 3600.0)

    def compressed_hours(self, now: float) -> float:
        return self.compressed_seconds(now) / 3600.0

    def progress(self, now: float) -> float:
        return self.compressed_seconds(now) / (COMPRESSED_WINDOW_HOURS * 3600.0)

    def label(self, now: float) -> str:
        """HH:MM over a notional business day (08:00 + compressed hours, wraps 24h)."""
        h = (_DAY_START_HOUR + self.compressed_hours(now)) % 24.0
        return f"{int(h):02d}:{int((h % 1) * 60):02d}"

    def clock(self, now: float) -> dict:
        return {
            "wall": round(self.elapsed(now), 1),
            "wall_window": WALL_WINDOW_SEC,
            "compressed_hours": round(self.compressed_hours(now), 2),
            "window_hours": COMPRESSED_WINDOW_HOURS,
            "label": self.label(now),
            "progress": round(self.progress(now), 4),
            "speedup": round(SPEEDUP),
            "disclosure": f"last {int(COMPRESSED_WINDOW_HOURS)}h · time-compressed",
        }

    # --- beats -------------------------------------------------------------
    def live_beats(self, now: float) -> list[dict]:
        ch = self.compressed_hours(now)
        return [b for b in self.beats if b["at_h"] <= ch]

    def pending_beats(self, now: float) -> list[dict]:
        ch = self.compressed_hours(now)
        return [b for b in self.beats if b["at_h"] > ch]

    def next_beat(self, now: float) -> dict | None:
        pend = self.pending_beats(now)
        return pend[0] if pend else None

    def beat_for_lever(self, sig: str) -> dict | None:
        return next((b for b in self.beats if b["lever"] == sig), None)

    def released_sigs(self, now: float) -> set[str]:
        """Lever signatures whose beat has gone live. A lever (notably the trap's
        refund route) is invisible/unactable until its beat releases."""
        ch = self.compressed_hours(now)
        sigs = {b["lever"] for b in self.beats if b["at_h"] <= ch}
        sigs |= {s for s, h in _EXTRA_SAFE_AT.items() if h <= ch}
        return sigs

    # --- volume (seeded arrival curve — deterministic, no RNG) -------------
    def arrival_volume(self, now: float, base: float = 1.0) -> float:
        """A smooth business-day curve over the window: ramp up, midday peak,
        taper. Never zero, so the volume bars always show life."""
        p = min(max(self.progress(now), 0.0), 1.0)
        curve = 0.45 + 0.55 * math.sin(math.pi * p)
        return base * curve

    # --- controls ----------------------------------------------------------
    def fast_forward(self, hours: float, now: float) -> None:
        """Advance the compressed clock by `hours` on cue (shift t0 back in wall time)."""
        self.t0 -= (hours * 3600.0) / SPEEDUP

    def reset(self, now: float | None = None) -> None:
        self.t0 = now if now is not None else time.monotonic()
