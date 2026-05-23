"""Sanity-check the cost computation against a known usage_metadata.

Run with: uv run python -m accountant.verify_cost
"""

from accountant.cost import compute_llm_cost, token_usage_from_gemini
from accountant.pricing.gemini import GEMINI_2_5_FLASH


SAMPLE_USAGE = {
    "prompt_token_count": 1953,
    "candidates_token_count": 31,
    "thoughts_token_count": 329,
}


def main() -> None:
    usage = token_usage_from_gemini(SAMPLE_USAGE)
    breakdown = compute_llm_cost(usage, GEMINI_2_5_FLASH)
    for key, value in breakdown.items():
        if isinstance(value, float):
            print(f"{key}: {value:.8f}")
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
