# Persona render pipeline — wiring point for the in-language notice

The user picked Q1 = "fallback to OpenRouter with notification in user language." This doc maps the exact integration point in the existing persona render pipeline.

## How AGENTS.md content reaches the container today

```
Django provision_tenant / config refresh
    │
    ▼
apps/orchestrator/services.py:256 / :504
    workspace_env = render_workspace_files(persona_key, tenant=tenant)
    │
    ▼
apps/orchestrator/personas.py:460 render_workspace_files
    result["NBHD_AGENTS_MD"] = render_agents_md(persona_key)  ← line 476
    │
    ▼
    if agents_extras := _get_tenant_prompt_extras(tenant, "agents_md"):
        result["NBHD_AGENTS_MD"] += "\n\n" + agents_extras    ← line 481-482
    │
    ▼
    [result dict serialized into container env vars]
    │
    ▼
runtime/openclaw/entrypoint.sh:23-27
    if [ -n "${NBHD_AGENTS_MD:-}" ]; then
        printf '%s\n' "$NBHD_AGENTS_MD" > "$NBHD_MANAGED_AGENTS_DST"
    fi
    │
    ▼
File on share at workspace/AGENTS.md → read by the agent at every turn
```

## Where to wire the BYO-Claude notice

**Inject in `render_workspace_files` immediately after the prompt_extras append.** Single function, one place:

```python
# apps/orchestrator/personas.py — around line 482, after prompt_extras append

if tenant is not None:
    has_claude = _tenant_has_active_claude_cred(tenant)
    if not has_claude:
        result["NBHD_AGENTS_MD"] += "\n\n" + _CLAUDE_FALLBACK_NOTICE
```

Where:

```python
# Near the top of personas.py — module-level constant

_CLAUDE_FALLBACK_NOTICE = """\
## NBHD platform note

This tenant has not connected their own Claude subscription, so this
conversation is running on a smaller fallback model (OpenRouter). Once per
conversation — but not in the first two turns and not when the user is
clearly mid-task — gently mention that connecting their own Claude account
at <https://neighborhoodunited.org/settings/ai-provider> will improve
response quality, and that the connection takes about 30 seconds. Phrase
the suggestion in whatever language the user has been writing in. Do not
nag, do not repeat it within the same conversation, do not mention it
during a cron / scheduled-task turn.
"""

def _tenant_has_active_claude_cred(tenant) -> bool:
    """True if the tenant has an Anthropic BYO credential that's not in
    the error state. False otherwise — used to gate the fallback notice
    in render_workspace_files.
    """
    if tenant is None:
        return False
    try:
        return tenant.byo_credentials.filter(
            provider="anthropic",
        ).exclude(status="error").exists()
    except Exception:
        return False
```

## Why here and not in the template

The existing `{{PERSONA_PERSONALITY}}` placeholder is a static template variable resolved in `render_agents_md(persona_key)` — that function doesn't have `tenant` in scope. We'd have to plumb tenant through three layers.

`render_workspace_files` already has `tenant` and already does `result["NBHD_AGENTS_MD"] += "\n\n" + extras`. The conditional notice is the same shape as the existing `agents_extras` append — minimum new code surface, minimum risk.

## When the notice gets refreshed

`render_workspace_files` runs at:
- Tenant provision (`apps/orchestrator/services.py:256`)
- Config refresh (`apps/orchestrator/services.py:504` — triggered by `tenant.bump_pending_config()`)

So the notice flips ON when a tenant disconnects Claude (because `apps/byo_models/views.py:179` calls `tenant.bump_pending_config()`) and flips OFF when they connect (same trigger at line 137). Good.

## Cron exception

The notice instructs the agent to avoid mentioning the connect prompt during cron turns. This is belt-and-suspenders — the cron preamble at `apps/orchestrator/config_generator.py` already biases the agent toward silent operation. But the explicit instruction in the notice prevents the morning briefing from awkwardly suggesting "by the way, connect Claude" before the user is awake.

## Verification

After implementation, render workspace files for a tenant with and without an Anthropic `BYOCredential`:

```python
# Django shell
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.models import Tenant

t = Tenant.objects.get(...)
out_with = render_workspace_files("neighbor", tenant=t)
# t.byo_credentials.all().delete()  (don't actually do this in prod)
out_without = render_workspace_files("neighbor", tenant=t)

assert "NBHD platform note" not in out_with["NBHD_AGENTS_MD"]
assert "NBHD platform note" in out_without["NBHD_AGENTS_MD"]
```

Add as a unit test in `apps/orchestrator/test_personas.py` (file may need to be created — there's no existing test of this exact function, just `test_prompt_extras.py`).
