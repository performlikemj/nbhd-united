# Stripe Review: Policy and Runtime Controls Package

This document captures the policy controls relevant to OAuth integrations, callback handling, and runtime access in one place for Stripe audit review.

It is intended as an audit packet artifact with no API contract changes.

## Scope

- Integration connect endpoints: Gmail and Google Calendar (including Composio-managed OAuth)
- Runtime tool policy exposed to subscriber tenants
- Internal call path between OpenClaw runtime plugins and Django control plane
- Preview environment access control
- Policy-critical settings required in deployment

## 1) Tool Policy Matrix

Runtime tool control originates from:

- `apps/orchestrator/tool_policy.py`

### Canonical policy object (from `generate_tool_config()`)

- `allow`
  - `group:network`
  - `group:memory`
  - `group:files`
  - `group:messaging`
  - `group:tts`
  - `group:image`
  - `group:browser` (plus tier only)
  - `exec` (plus tier only)
- `deny`
  - `group:automation`
  - `gateway`
  - `cron`
  - `sessions_spawn`
  - `sessions_send`
  - `sessions_list`
  - `sessions_history`
  - `session_status`
  - `agents_list`
- `elevated`
  - `enabled: false` (all subscriber tiers)
- `web.search.enabled`
  - `true`

### Runtime configuration generation

`generate_tool_config()` is called by:

- `apps/orchestrator/config_generator.py::_build_tools_section()`

The generated OpenClaw config is assembled in:

- `apps/orchestrator/config_generator.py:generate_openclaw_config()`

Config behavior is:

- `tools` block is always present
- `tools.allow`/`tools.deny` always follows policy in `tool_policy.py`
- optional plugin enablement adds `tools.alsoAllow = ["group:plugins"]` when `OPENCLAW_GOOGLE_PLUGIN_ID` is configured

## 2) Runtime Auth Contract

Runtime-to-control-plane communication path (OpenClaw runtime plugin to Django) is in:

- `runtime/openclaw/plugins/nbhd-google-tools/index.js`

For every runtime request, plugin sets:

- `X-NBHD-Internal-Key`: internal shared key
- `X-NBHD-Tenant-Id`: tenant UUID
Header assembly is centralized in `getRuntimeConfig()` + `callNbhdRuntimeRequest()`.

Runtime endpoint auth contract is enforced by:

- `apps/integrations/internal_auth.py` (internal API-key + tenant-id checks)

## 3) Endpoint Allowlist + Preview Bypass Map

### Integration callback endpoints

- `apps/integrations/urls.py`
  - `authorize/<str:provider>/`
  - `callback/<str:provider>/`
  - `composio-callback/<str:provider>/`

### Frontend callback-result rendering path

- `frontend/components/app-shell.tsx`
  - Public pages (`/login`, `/signup`, `/legal/*`) render without authentication.
  - Authenticated pages redirect to `/login` when no session is present.

### Signup invite-code gate

- `apps/tenants/auth_views.py`
  - `SignupView` validates `invite_code` from request body against `PREVIEW_ACCESS_KEY`.
  - When `PREVIEW_ACCESS_KEY` is empty, signup is open (no invite code required).
  - When set, signup returns 403 if the invite code is missing or incorrect.

## 4) Policy-Critical Settings and Deployment Inputs

### Django policy and runtime settings

From `config/settings/base.py`:

- `PREVIEW_ACCESS_KEY`
- `REDIS_URL` (Django cache and session-linked OAuth state cache)
- `UPSTASH_REDIS_URL` (optional REST-compatible helper; not required by Django-redis)
- `NBHD_INTERNAL_API_KEY`
- `API_BASE_URL`
- `FRONTEND_URL`
- `OPENCLAW_GOOGLE_PLUGIN_ID`
- `OPENCLAW_GOOGLE_PLUGIN_PATH`
- `OPENCLAW_CONTAINER_SECRET_BACKEND`
- `AZURE_KV_SECRET_ANTHROPIC_API_KEY`
- `AZURE_KV_SECRET_TELEGRAM_BOT_TOKEN`
- `AZURE_KV_SECRET_NBHD_INTERNAL_API_KEY`
- `AZURE_KV_SECRET_TELEGRAM_WEBHOOK_SECRET`
- `PREVIEW_ACCESS_KEY` (invite code for gated signup; leave empty for open registration)

### Deployment env sketch (placeholders only)

```bash
# Django / control-plane
REDIS_URL=rediss://<upstash-user>:<upstash-token>@<upstash-host>:<port>/<db>
UPSTASH_REDIS_URL=<optional-rest-url>
PREVIEW_ACCESS_KEY=<shared-preview-key>
NBHD_INTERNAL_API_KEY=<shared-internal-api-key>
API_BASE_URL=https://<tenant-api-host>
FRONTEND_URL=https://<tenant-frontend-host>

# OpenClaw runtime injection
OPENCLAW_GOOGLE_PLUGIN_ID=nbhd-google-tools
OPENCLAW_GOOGLE_PLUGIN_PATH=/opt/nbhd/plugins/nbhd-google-tools
OPENCLAW_CONTAINER_SECRET_BACKEND=keyvault
```

`UPSTASH_REDIS_URL` is intentionally not required by Django cache; use `REDIS_URL` for Redis protocol clients.

## 5) Composio + OAuth Behavior (No Password Propagation)

- Callback endpoints are always session/state-validated and do not require preview or bearer headers.
- OAuth redirects are not appended with preview keys.
- `apps/integrations/views.py` only redirects to `/integrations?connected=<provider>` or `/integrations?error=<code>` after callback processing.

## 6) Reviewer Checklist

1. Verify policy matrix in runtime config payload:
   - assert `tools.deny` includes runtime-management and session/cross-agent controls
   - assert `tools.elevated.enabled` is false
2. Verify internal auth headers are always sent from runtime plugin.
3. Verify invite-code signup gate:
   - signup endpoint validates invite code against `PREVIEW_ACCESS_KEY`
   - all pages are publicly browsable; authenticated pages redirect to login
4. Verify env keys above exist in deployed secrets/config (redacted values only).
5. Verify composio callback endpoints accept unauthenticated callbacks but enforce state/tenant integrity and expiry.

## 7) Evidence Links / Commands

- Tool policy and runtime generation tests:
  - `apps/orchestrator/test_tool_policy.py`
  - `apps/orchestrator/test_azure_client.py`
- Integration policy and callback-path tests:
  - `apps/integrations/test_views.py`
  - `apps/integrations/test_runtime_views.py`
  - `apps/integrations/test_services.py`
- End-to-end callback/result scenario:
  - Integrations page shows:
    - `Integrations?connected=<provider>`
    - `Integrations?error=<code>`
