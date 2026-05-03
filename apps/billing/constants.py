"""Model pricing constants for usage transparency.

Rates are per 1M tokens in USD. Update when providers change pricing.
"""

# ── Canonical model IDs ────────────────────────────────────────────────────
# Change these once here; every other module imports from this file.
MINIMAX_MODEL = "openrouter/minimax/minimax-m2.7"
MINIMAX_DISPLAY = "MiniMax M2.7"
MINIMAX_RATE = {"input": 0.3, "output": 1.2}

KIMI_MODEL = "openrouter/moonshotai/kimi-k2.6"
KIMI_DISPLAY = "Kimi 2.6"
KIMI_RATE = {"input": 0.60, "output": 2.80}

GEMMA_MODEL = "openrouter/google/gemma-4-31b-it"
GEMMA_DISPLAY = "Gemma 4 31B"
GEMMA_RATE = {"input": 0.14, "output": 0.40}

# BYO subscription models — tenant pays the provider directly via their
# Pro/Max/Plus account. Not in MODEL_RATES because NBHD doesn't bill
# tokens for these.
#
# Model IDs use the canonical `anthropic/<model>` form (NOT `anthropic-cli/...`,
# which we shipped briefly in PR #419 — that prefix doesn't exist in OpenClaw
# 2026.4.25's registry). CLI routing is activated by the `anthropic:claude-cli`
# **auth profile** (registered at container boot by `runtime/openclaw/entrypoint.sh`
# via `openclaw models auth login --provider anthropic --method cli`). When that
# profile is present, OpenClaw routes any `anthropic/<model>` request through the
# bundled `claude` binary, which reads `CLAUDE_CODE_OAUTH_TOKEN` and bills the
# tenant's Pro/Max subscription. Without the profile, the same model id falls
# through to the HTTP plugin (which needs `ANTHROPIC_API_KEY`).
ANTHROPIC_SONNET_MODEL = "anthropic/claude-sonnet-4-6"
ANTHROPIC_SONNET_DISPLAY = "Claude Sonnet 4.6"

ANTHROPIC_OPUS_MODEL = "anthropic/claude-opus-4-7"
ANTHROPIC_OPUS_DISPLAY = "Claude Opus 4.7"

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

# ── Reasoning / slow-inference models ─────────────────────────────────────
# These models have longer time-to-first-token and generation times.
# The router gives them a higher forwarding timeout and sends a
# "still thinking" notice so users know the system hasn't stalled.
REASONING_MODELS: set[str] = {
    KIMI_MODEL,
    KIMI_MODEL.removeprefix("openrouter/"),
}

# ── BYO slow models ──────────────────────────────────────────────────────
# Anthropic Claude routed through OpenClaw's `claude` CLI backend on a
# tenant's Pro/Max subscription. Cold-start of the CLI session plus the
# full agent context (memory + journal + finance + fuel + google + line +
# telegram MCP plugins) regularly takes 150s+ for the first turn after a
# wake — well past the 120s default. These get the longer reasoning-model
# timeout so buffered-delivery doesn't bail and trigger a QStash retry
# storm that the OpenClaw CLI backend would then fall back off to MiniMax.
BYO_SLOW_MODELS: set[str] = {
    ANTHROPIC_SONNET_MODEL,
    ANTHROPIC_OPUS_MODEL,
}

DEFAULT_CHAT_TIMEOUT = 120.0  # seconds — standard models
REASONING_MODEL_TIMEOUT = 240.0  # seconds — reasoning + BYO Claude (within gunicorn 300s)

# Monthly token budget (informational — enforcement uses TIER_COST_BUDGETS).
TIER_TOKEN_BUDGETS: dict[str, int] = {
    "starter": 5_000_000,
}

# Monthly cost budget in USD.  Enforcement compares
# estimated_cost_this_month against this cap.
TIER_COST_BUDGETS: dict[str, float] = {
    "starter": 5.00,  # ~16M tokens of MiniMax
}
