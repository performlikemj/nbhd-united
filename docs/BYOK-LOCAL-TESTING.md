# BYOK Local Testing Guide

Test the Bring Your Own Key flow without Stripe approval.

## Prerequisites
- Local Django dev server running
- Local frontend running (`npm run dev`)
- A test user account (sign up through the frontend or create via Django admin)
- An API key from any supported provider (OpenAI, Anthropic, Groq, Google, OpenRouter, xAI)

## Step 1: Set the test user's tier to BYOK

```bash
cd /path/to/nbhd-united
python manage.py shell
```

```python
from apps.tenants.models import Tenant

# Find your test user
t = Tenant.objects.get(user__email="your-test-user@example.com")

# Set tier to BYOK and activate
t.model_tier = "byok"
t.status = "active"
t.save()

print(f"Tenant {t.id} set to BYOK, status: {t.status}")
```

## Step 2: Test the frontend

1. Log in as the test user
2. Go to **Settings → AI Provider** (`/settings/ai-provider`)
3. You should see the BYOK form (provider dropdown, API key input, model ID)
4. If you see "Upgrade to BYOK" instead, the tier didn't save — recheck Step 1

## Step 3: Save a BYOK configuration

1. Select a provider (e.g., Anthropic)
2. Enter your API key
3. Optionally enter a model ID (e.g., `anthropic/claude-sonnet-4-20250514`)
4. Click Save
5. Verify the success message appears

## Step 4: Verify the API stored it correctly

```python
from apps.tenants.models import UserLLMConfig
from apps.tenants.crypto import decrypt_api_key

config = UserLLMConfig.objects.get(user__email="your-test-user@example.com")
print(f"Provider: {config.provider}")
print(f"Model: {config.model_id}")
print(f"Has key: {bool(config.encrypted_api_key)}")
print(f"Key (decrypted): {decrypt_api_key(config.encrypted_api_key)[:8]}...")
```

## Step 5: Verify config generation

```python
from apps.tenants.models import Tenant
from apps.orchestrator.config_generator import build_openclaw_config

t = Tenant.objects.get(user__email="your-test-user@example.com")
config = build_openclaw_config(t)

# Check that the API key is injected in the env block
print("Env keys:", list(config.get("env", {}).keys()))
# Should show something like: ['ANTHROPIC_API_KEY'] or ['OPENAI_API_KEY']

# Check the model is set
print("Primary model:", config["agents"]["defaults"]["model"]["primary"])
```

## Step 6: Test the full round trip (optional)

If you have a local OpenClaw gateway running:

1. Save BYOK config via the frontend
2. Check that `bump_pending_config()` was called (look for `pending_config_version` increment on the tenant)
3. The next config apply cycle should pick up the new config with your key injected

## What to verify

| Check | Expected |
|-------|----------|
| Non-BYOK user sees upgrade prompt | ✅ "Want to use your own model?" card |
| BYOK user sees the config form | ✅ Provider dropdown, key input, model input |
| Saving without a key keeps existing key | ✅ `has_key` stays true, `key_masked` unchanged |
| Saving with a new key updates it | ✅ New masked key shown on reload |
| Config generator includes the key | ✅ Correct env var set (e.g., `ANTHROPIC_API_KEY`) |
| Config generator sets the model | ✅ `agents.defaults.model.primary` matches input |
| "Test Connection" button | 🚧 Currently disabled — future feature |

## Supported providers and their env vars

| Provider | Env var injected |
|----------|-----------------|
| openai | `OPENAI_API_KEY` |
| anthropic | `ANTHROPIC_API_KEY` |
| groq | `GROQ_API_KEY` |
| google | `GEMINI_API_KEY` |
| openrouter | `OPENROUTER_API_KEY` |
| xai | `XAI_API_KEY` |

## Cleanup

To revert a test user back to a normal tier:

```python
t = Tenant.objects.get(user__email="your-test-user@example.com")
t.model_tier = "starter"  # or "premium"
t.save()

# Optionally delete the LLM config
UserLLMConfig.objects.filter(user=t.user).delete()
```
