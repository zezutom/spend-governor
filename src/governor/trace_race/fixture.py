"""Build the trace-race replay fixture from a captured pair.

The fixture is the already-aligned, replay-ready contract the component
renders — alignment computed once, here, never during playback. It carries:
the ticket, both lanes' totals + answer + a Phoenix link, the aligned rows
(each with per-lane cost, status, and a per-span Phoenix deeplink), the
reconciled saving, and the same-answer verdict (whitespace-tolerant; the
preservation proof shows only when it is genuinely true).
"""

import json
import re
import time
from pathlib import Path

from governor.trace_race.align import align, load_spans


_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_FIXTURE_PATH = _DATA_DIR / "trace_race_fixture.json"


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _gid():
    try:
        from governor.pipeline.phoenix_cost import project_gid
        return project_gid()
    except Exception:
        return None


def _deeplink(gid, trace_id, node_id):
    if not gid:
        return None
    try:
        from governor.pipeline.phoenix_cost import span_deeplink
        return span_deeplink(gid, trace_id, node_id)
    except Exception:
        return None


def build_fixture(cap: dict, *, seeded: bool = True) -> dict:
    """cap: {ticket, baseline_trace_id, baseline_answer, governed_trace_id,
    governed_answer}. seeded=False marks a dev fixture built from a non-paired
    capture (animation development only — never the shipped same-answer claim)."""
    base_id, gov_id = cap["baseline_trace_id"], cap["governed_trace_id"]
    if not (base_id and gov_id):
        raise ValueError("capture is missing a trace id — nothing to align")
    b, g = load_spans(base_id), load_spans(gov_id)
    a = align(b, g)
    gid = _gid()

    rows = []
    for r in a["rows"]:
        rows.append({
            **r,
            "baseline": {**r["baseline"], "url": _deeplink(gid, base_id, r["baseline"].get("node"))},
            "governed": {**r["governed"], "url": _deeplink(gid, gov_id, r["governed"].get("node"))},
        })

    # The preservation proof is true only when the answers genuinely match.
    same = seeded and _norm(cap.get("baseline_answer")) == _norm(cap.get("governed_answer"))

    return {
        "seeded": seeded,
        "same_answer": same,
        "ticket": cap.get("ticket", ""),
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline": {
            "trace_id": base_id,
            "total_usd": a["baseline_total_usd"],
            "answer": cap.get("baseline_answer", ""),
            "phoenix_url": _deeplink(gid, base_id, None),
        },
        "governed": {
            "trace_id": gov_id,
            "total_usd": a["governed_total_usd"],
            "answer": cap.get("governed_answer", ""),
            "phoenix_url": _deeplink(gid, gov_id, None),
        },
        "saved_usd": a["saved_usd"],
        "skipped_calls": a["skipped_calls"],
        "swapped_calls": a["swapped_calls"],
        "rows": rows,
    }


def build_and_save(cap: dict, *, seeded: bool = True, path: Path = _FIXTURE_PATH) -> str:
    fx = build_fixture(cap, seeded=seeded)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fx, indent=2))
    return str(path)


def load_fixture(path: Path = _FIXTURE_PATH) -> dict | None:
    """The UI reads this. None ⇒ no seed captured yet (show the placeholder)."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
