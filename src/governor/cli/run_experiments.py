"""Run baseline-vs-governed savings experiments in Phoenix (refactor #3).

Phoenix computes each experiment's aggregate cost; the delta is the savings,
on a URL-addressable compare page. Run offline (each agent run is ~30s); the
dashboard's "Prove in Phoenix" button launches this as a detached subprocess
and reads the result from the `experiment_proof` state_meta key.

Usage:
    uv run python -m governor.cli.run_experiments [LABEL] [PER_CLASS]

LABEL defaults to "demo"; PER_CLASS (tickets per task class) defaults to 3.
Requires the policies to demonstrate to be ACTIVE in the store.
"""

import json
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from governor.analytics.experiments import run_savings_experiments
from governor.pipeline.db import set_meta


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "demo"
    per_class = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    set_meta("experiment_proof", json.dumps({
        "status": "running", "label": label,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }))
    try:
        result = run_savings_experiments(label=label, per_class=per_class)
        result.update(status="done", label=label,
                      ran_at=datetime.now(timezone.utc).isoformat())
        set_meta("experiment_proof", json.dumps(result))
        print(json.dumps(result, indent=2))
        print("\nCompare in Phoenix:", result.get("compare_url"))
    except Exception as e:
        set_meta("experiment_proof", json.dumps({
            "status": "error", "label": label,
            "error": f"{type(e).__name__}: {e}",
        }))
        raise


if __name__ == "__main__":
    main()
