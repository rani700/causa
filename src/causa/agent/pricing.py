"""What an answer cost the language model provider, in US dollars.

Standard-tier prices per million tokens (input, cached input, output), from the providers' pricing
pages. Models not listed report tokens only; ``CAUSA_AI_PRICES`` style overrides can be added here.
"""

from __future__ import annotations

# model prefix -> (input, cached input, output) USD per 1M tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
}


def usd(model: str | None, input_tokens: int, cached_tokens: int, output_tokens: int) -> float | None:
    """Cost of one answer, or None when the model's price is not known."""
    if not model:
        return None
    prefix = max((p for p in PRICES if model.startswith(p)), key=len, default=None)
    if prefix is None:
        return None
    price_in, price_cached, price_out = PRICES[prefix]
    fresh = max(input_tokens - cached_tokens, 0)
    return (fresh * price_in + cached_tokens * price_cached + output_tokens * price_out) / 1_000_000
