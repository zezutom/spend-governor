"""Run the Margin Agent over an INPUT JSON file and print the result.

    uv run python -m accountant.cli.run_margin [path/to/input.json]

With no path it uses examples/margin-input.example.json — the contract
sample — so the agent can be exercised end-to-end before the live INPUT
builder is wired in.
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from accountant.margin import run_margin


_DEFAULT_INPUT = Path(__file__).resolve().parents[3] / "examples" / "margin-input.example.json"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_INPUT
    margin_input = json.loads(path.read_text())
    out = run_margin(margin_input)
    print(json.dumps(out.model_dump(), indent=2))


if __name__ == "__main__":
    main()
