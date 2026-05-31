"""Run baseline-vs-governed savings experiments in Phoenix (refactor #3).

Phoenix computes each experiment's aggregate cost; the delta is the savings,
on a URL-addressable compare page. Run offline (each agent run is ~30s).

Usage:
    uv run python -m accountant.cli.run_experiments [LABEL] [PER_CLASS]

LABEL defaults to "demo"; PER_CLASS (tickets per task class) defaults to 3.
Requires the policies to demonstrate to be ACTIVE in the store.
"""

import json
import sys

from dotenv import load_dotenv

load_dotenv()

from accountant.analytics.experiments import run_savings_experiments


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "demo"
    per_class = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    result = run_savings_experiments(label=label, per_class=per_class)
    print(json.dumps(result, indent=2))
    print("\nCompare in Phoenix:", result.get("compare_url"))


if __name__ == "__main__":
    main()
