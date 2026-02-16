"""Model pricing constants for usage transparency.

Rates are per 1M tokens in USD. Update when providers change pricing.
"""

MODEL_RATES: dict[str, dict[str, float]] = {
    "claude-opus-4.6": {
        "input": 5.0,
        "output": 25.0,
        "display_name": "Claude Opus 4.6",
    },
    "claude-sonnet-4.5": {
        "input": 3.0,
        "output": 15.0,
        "display_name": "Claude Sonnet 4.5",
    },
    "claude-haiku-4.5": {
        "input": 1.0,
        "output": 5.0,
        "display_name": "Claude Haiku 4.5",
    },
    # OpenRouter-style model identifiers (map to same rates)
    "anthropic/claude-opus-4-20250514": {
        "input": 5.0,
        "output": 25.0,
        "display_name": "Claude Opus 4.6",
    },
    "anthropic/claude-sonnet-4-20250514": {
        "input": 3.0,
        "output": 15.0,
        "display_name": "Claude Sonnet 4.5",
    },
    "anthropic/claude-haiku-4-20250514": {
        "input": 1.0,
        "output": 5.0,
        "display_name": "Claude Haiku 4.5",
    },
    "moonshot/kimi-k2.5": {
        "input": 0.5,
        "output": 2.0,
        "display_name": "Kimi K2.5",
    },
}

DEFAULT_RATE = {"input": 3.0, "output": 15.0, "display_name": "Unknown Model"}

# Platform pricing defaults
PLATFORM_MARGIN_TARGET = 0.30  # 30% target margin
