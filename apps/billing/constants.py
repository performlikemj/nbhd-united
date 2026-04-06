"""Model pricing constants for usage transparency.

Rates are per 1M tokens in USD. Update when providers change pricing.
"""

# ── Canonical model IDs ────────────────────────────────────────────────────
# Change these once here; every other module imports from this file.
MINIMAX_MODEL = "openrouter/minimax/minimax-m2.7"
MINIMAX_DISPLAY = "MiniMax M2.7"
MINIMAX_RATE = {"input": 0.3, "output": 1.2}

KIMI_MODEL = "openrouter/moonshotai/kimi-k2.5"
KIMI_DISPLAY = "Kimi 2.5"
KIMI_RATE = {"input": 0.38, "output": 1.72}

GEMMA_MODEL = "openrouter/google/gemma-4-31b-it"
GEMMA_DISPLAY = "Gemma 4 31B"
GEMMA_RATE = {"input": 0.14, "output": 0.40}

MODEL_RATES: dict[str, dict[str, float]] = {
    MINIMAX_MODEL: {
        **MINIMAX_RATE,
        "display_name": MINIMAX_DISPLAY,
    },
    # OpenClaw sometimes reports without the openrouter/ prefix
    MINIMAX_MODEL.removeprefix("openrouter/"): {
        **MINIMAX_RATE,
        "display_name": MINIMAX_DISPLAY,
    },
    KIMI_MODEL: {
        **KIMI_RATE,
        "display_name": KIMI_DISPLAY,
    },
    KIMI_MODEL.removeprefix("openrouter/"): {
        **KIMI_RATE,
        "display_name": KIMI_DISPLAY,
    },
    GEMMA_MODEL: {
        **GEMMA_RATE,
        "display_name": GEMMA_DISPLAY,
    },
    GEMMA_MODEL.removeprefix("openrouter/"): {
        **GEMMA_RATE,
        "display_name": GEMMA_DISPLAY,
    },
}

DEFAULT_RATE = {"input": 0.3, "output": 1.2, "display_name": "Unknown Model"}

# Monthly token budget (informational — enforcement uses TIER_COST_BUDGETS).
TIER_TOKEN_BUDGETS: dict[str, int] = {
    "starter": 5_000_000,
}

# Monthly cost budget in USD.  Enforcement compares
# estimated_cost_this_month against this cap.
TIER_COST_BUDGETS: dict[str, float] = {
    "starter": 5.00,     # ~16M tokens of MiniMax
}
