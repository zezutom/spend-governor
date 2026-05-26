"""Gemini 2.5 pricing per Vertex AI.

Source: https://cloud.google.com/vertex-ai/generative-ai/pricing
Last verified: 2026-05-23

Cached input is priced at a separate (much lower) rate than uncached
input. Thinking tokens are billed at the output rate. The Pro rates
below are the small-context tier (≤200k input tokens) — if a call ever
exceeds 200k input tokens we'll need the large-context tier added
explicitly, but our agent's prompts are well under that ceiling.
"""

from accountant.pricing.cost import ModelPrice


GEMINI_2_5_FLASH = ModelPrice(
    name="gemini-2.5-flash",
    input_uncached_per_1m_usd=0.30,
    input_cached_per_1m_usd=0.075,
    output_per_1m_usd=2.50,
)


GEMINI_2_5_PRO = ModelPrice(
    name="gemini-2.5-pro",
    input_uncached_per_1m_usd=1.25,
    input_cached_per_1m_usd=0.3125,
    output_per_1m_usd=10.00,
)


# Cheaper tier the wrapper routes simple requests to.
GEMINI_2_5_FLASH_LITE = ModelPrice(
    name="gemini-2.5-flash-lite",
    input_uncached_per_1m_usd=0.10,
    input_cached_per_1m_usd=0.025,
    output_per_1m_usd=0.40,
)


MODELS: dict[str, ModelPrice] = {
    "gemini-2.5-flash": GEMINI_2_5_FLASH,
    "gemini-2.5-pro": GEMINI_2_5_PRO,
    "gemini-2.5-flash-lite": GEMINI_2_5_FLASH_LITE,
}
