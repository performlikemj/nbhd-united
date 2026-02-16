# Tier & BYOK Refactor Plan

## New Tier Structure

| Tier | Name | Price | Model | Message Cap | Token Budget |
|------|------|-------|-------|-------------|-------------|
| `starter` | Starter | ~$5-8/mo | Kimi K2.5 or MiniMax M2.1 | 50/day | 500K/mo |
| `premium` | Premium | ~$20-30/mo | Anthropic Sonnet (Opus available) | 200/day | 2M/mo |
| `byok` | Bring Your Own Key | ~$5-8/mo | User's choice | 200/day | No platform limit |

## Work Streams

### Stream 1: Backend Tier Refactor
- Update `Tenant.ModelTier` choices: `basic`→`starter`, `plus`→`premium`, add `byok`
- Migration to rename existing tiers
- Update `config_generator.py` TIER_MODELS and TIER_MODEL_CONFIGS for starter/premium/byok
- Update `tool_policy.py` tier mappings
- Update `billing/constants.py` — remove hardcoded $5, add model rates for Kimi/MiniMax
- Update `billing/views.py` — checkout accepts new tier names
- Update `config/settings/base.py` — STRIPE_PRICE_IDS for starter/premium/byok
- Add Kimi K2.5 and MiniMax M2.1 provider configs to config_generator

### Stream 2: BYOK Backend
- New model: `UserLLMConfig` (encrypted API key storage)
  - Fields: user FK, provider (openai/anthropic/groq/etc), encrypted_api_key, model_preference, created_at, updated_at
  - Encryption: Fernet symmetric encryption using Django SECRET_KEY derived key
- API endpoints: GET/PUT /api/v1/settings/llm-config/
  - GET returns provider + model (never the raw key, just masked)
  - PUT accepts provider + api_key + model_preference
- `config_generator.py`: When tenant is `byok` tier, read UserLLMConfig and inject into OpenClaw config
  - Set `auth.profiles` with user's key
  - Set `agents.defaults.model.primary` to user's chosen model
  - Fallback to starter-tier model if BYOK key fails

### Stream 3: Frontend Updates
- Remove hardcoded "$5" from `app/legal/terms/page.tsx`
- Update `apps/router/services.py` welcome message
- Onboarding: tier selection step (starter/premium/byok cards)
- Settings: new "AI Provider" tab for BYOK users
  - Provider dropdown (OpenAI, Anthropic, Groq, Google, OpenRouter)
  - API key input (masked after save)
  - Model selector (populated based on provider)
  - Test connection button

### Stream 4: Config Generator — Provider Configs
Based on OpenClaw docs, add provider configs for:

**Kimi K2.5 (starter tier):**
```json5
{
  models: {
    providers: {
      moonshot: {
        baseUrl: "https://api.moonshot.ai/v1",
        apiKey: "${MOONSHOT_API_KEY}",
        api: "openai-completions",
        models: [{ id: "kimi-k2.5", name: "Kimi K2.5" }],
      }
    }
  }
}
```

**MiniMax M2.1 (starter fallback):**
```json5
{
  models: {
    providers: {
      minimax: {
        baseUrl: "https://api.minimax.io/anthropic",
        apiKey: "${MINIMAX_API_KEY}",
        api: "anthropic-messages",
        models: [{ id: "MiniMax-M2.1", name: "MiniMax M2.1" }],
      }
    }
  }
}
```

**BYOK provider templates** (injected based on user's chosen provider):
- OpenAI: built-in, just needs api key
- Anthropic: built-in, just needs api key
- Groq: built-in, just needs api key
- Google: built-in, needs GEMINI_API_KEY
- OpenRouter: built-in, needs OPENROUTER_API_KEY

## Migration Strategy
- Existing `basic` tenants → `starter`
- Existing `plus` tenants → `premium`
- Data migration in Django migration file
