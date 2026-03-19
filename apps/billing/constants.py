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
    "anthropic/claude-opus-4.6": {
        "input": 5.0,
        "output": 25.0,
        "display_name": "Claude Opus 4.6",
    },
    "anthropic/claude-opus-4-20250514": {
        "input": 5.0,
        "output": 25.0,
        "display_name": "Claude Opus 4.6",
    },
    "anthropic/claude-sonnet-4.6": {
        "input": 3.0,
        "output": 15.0,
        "display_name": "Claude Sonnet 4.6",
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
    "openrouter/minimax/minimax-m2.7": {
        "input": 0.3,
        "output": 1.2,
        "display_name": "MiniMax M2.7",
    },
    "minimax/minimax-m2.7": {
        "input": 0.3,
        "output": 1.2,
        "display_name": "MiniMax M2.7",
    },
}

DEFAULT_RATE = {"input": 0.3, "output": 1.2, "display_name": "Unknown Model"}

# Per-tier monthly token budgets (informational — enforcement uses TIER_COST_BUDGETS).
# 0 = unlimited (BYOK users pay their own costs).
TIER_TOKEN_BUDGETS: dict[str, int] = {
    "starter": 5_000_000,
    "premium": 10_000_000,
    "byok": 0,
}

# Per-tier monthly cost budgets in USD.  Enforcement compares
# estimated_cost_this_month against these caps.  0 = unlimited.
TIER_COST_BUDGETS: dict[str, float] = {
    "starter": 5.00,     # ~16M tokens of MiniMax M2.7
    "premium": 40.00,    # ~27M Sonnet or ~133M MiniMax — user picks the tradeoff
    "byok": 0,           # unlimited (user pays their own API costs)
}
