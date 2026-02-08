# MCP Directive: Domain Migration — Sautai Backend to api.sautai.com

**Objective:** Move Sautai's Django backend from `hoodunited.org` / `neighborhoodunited.org` to `api.sautai.com`, freeing those domains for the NBHD United project.

**Safety level:** HIGH — this affects a live production backend. Each phase must be verified before proceeding to the next. Do NOT skip verification steps.

---

## Prerequisites

Before starting, confirm:
- [ ] You have access to Cloudflare DNS for `sautai.com`
- [ ] You have access to Azure portal or CLI for resource group containing `sautai-django-westus2`
- [ ] You know the current FQDN of `sautai-django-westus2` (the `*.azurecontainerapps.io` address)

To find the FQDN:
```bash
az containerapp show \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --query "properties.configuration.ingress.fqdn" \
  -o tsv
```

---

## Phase 1: Add `api.sautai.com` to Sautai Backend

**Goal:** Backend accessible on new domain while old domains still work. Zero downtime.

### Step 1.1: Add DNS record in Cloudflare

1. Go to Cloudflare → `sautai.com` → DNS
2. Add record:
   - **Type:** CNAME
   - **Name:** `api`
   - **Target:** `<FQDN of sautai-django-westus2>` (e.g., `sautai-django-westus2.happyfield-12345.westus2.azurecontainerapps.io`)
   - **Proxy status:** DNS only (gray cloud, NOT proxied)
   
   ⚠️ **MUST be DNS only (gray cloud)** — Azure needs to reach the real hostname for TLS certificate validation. If proxied (orange cloud), Azure cert validation will fail.

3. Wait for DNS propagation (~1-5 minutes for Cloudflare)

### Step 1.2: Add custom domain in Azure

```bash
# Add hostname
az containerapp hostname add \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname api.sautai.com

# Bind managed certificate (Azure provides free TLS)
az containerapp hostname bind \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname api.sautai.com \
  --environment <CONTAINER_APP_ENV_NAME> \
  --validation-method CNAME
```

If using Azure Portal instead:
1. Container Apps → `sautai-django-westus2` → Custom domains
2. Add custom domain → `api.sautai.com`
3. Select "Managed certificate"
4. Validate and add

### Step 1.3: Update Django settings

Add `api.sautai.com` to the Sautai Django settings. The `ALLOWED_HOSTS` is read from the `ALLOWED_HOSTS` environment variable.

**Option A: Update env var in Azure** (preferred — no code deploy needed):
```bash
# Get current ALLOWED_HOSTS value first
az containerapp show \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --query "properties.template.containers[0].env[?name=='ALLOWED_HOSTS'].value" \
  -o tsv

# Update — append api.sautai.com to existing value
az containerapp update \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --set-env-vars "ALLOWED_HOSTS=<EXISTING_VALUE>,api.sautai.com"
```

**Option B: Update code** (if ALLOWED_HOSTS is hardcoded):

In `hood_united/settings.py`, in the CORS fallback block (~line 137), add:
```python
'https://api.sautai.com',
```

And in the production `CSRF_TRUSTED_ORIGINS` block (~line 665), add:
```python
'https://api.sautai.com',
```

Also update `CORS_ALLOWED_ORIGINS` env var in Azure if it's set:
```bash
az containerapp show \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --query "properties.template.containers[0].env[?name=='CORS_ALLOWED_ORIGINS'].value" \
  -o tsv

# If set, append https://api.sautai.com to it
```

### Step 1.4: Verify

Run ALL of these checks before proceeding:

```bash
# 1. DNS resolves
dig api.sautai.com CNAME +short
# Expected: sautai-django-westus2.<region>.azurecontainerapps.io

# 2. HTTPS works
curl -I https://api.sautai.com/admin/
# Expected: 200 or 302 (redirect to login)

# 3. Old domains still work (CRITICAL — they must still work)
curl -I https://hoodunited.org/admin/
curl -I https://neighborhoodunited.org/admin/
# Expected: 200 or 302

# 4. API responds
curl https://api.sautai.com/chefs/api/public/approved-chefs/
# Expected: JSON response
```

**⛔ STOP if any check fails. Do NOT proceed to Phase 2.**

---

## Phase 2: Switch Sautai Frontend to `api.sautai.com`

**Goal:** Frontend talks to `api.sautai.com` instead of old domains.

### Step 2.1: Identify current API base URL

Check the Sautai frontend for the API base URL configuration:
- Look for `VITE_API_BASE_URL`, `REACT_APP_API_URL`, or similar in:
  - `.env` / `.env.production` in the frontend repo
  - Azure Static Web App application settings
  - Hardcoded in source code (search for `hoodunited.org` or `neighborhoodunited.org`)

### Step 2.2: Update frontend API base URL

Change the API base URL from the current value to `https://api.sautai.com`

**If it's an Azure Static Web App env var:**
```bash
az staticwebapp appsettings set \
  --name sautai-frontend \
  --resource-group <RESOURCE_GROUP> \
  --setting-names "VITE_API_BASE_URL=https://api.sautai.com"
```

⚠️ Note: For Vite apps, `VITE_*` env vars are baked in at build time, not runtime. If the URL is a `VITE_*` variable, you need to **rebuild and redeploy** the frontend with the new value, not just update the env var.

### Step 2.3: Update Sautai Telegram webhook URL

The Sautai bot webhook currently points to one of the old domains. Update it:

```bash
# First, check current webhook
curl "https://api.telegram.org/bot<SAUTAI_BOT_TOKEN>/getWebhookInfo"

# Update to new domain
curl -X POST "https://api.telegram.org/bot<SAUTAI_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://api.sautai.com/api/telegram/webhook/",
    "secret_token": "<SAUTAI_TELEGRAM_WEBHOOK_SECRET>"
  }'

# Verify
curl "https://api.telegram.org/bot<SAUTAI_BOT_TOKEN>/getWebhookInfo"
# Expected: url should show https://api.sautai.com/api/telegram/webhook/
```

### Step 2.4: Update Stripe webhook URL (if applicable)

1. Go to Stripe Dashboard → Developers → Webhooks
2. Find the webhook endpoint pointing to `hoodunited.org` or `neighborhoodunited.org`
3. Update the URL to `https://api.sautai.com/<same-path>`
4. **Do NOT delete the old webhook yet** — update the URL in place

### Step 2.5: Check for OAuth callback URLs

Search for any OAuth redirect URIs registered with:
- Google (Gmail, Calendar) OAuth credentials
- Any other OAuth providers

If found, add `https://api.sautai.com` as an authorized redirect URI. Keep old URIs for now.

### Step 2.6: Verify

```bash
# 1. Frontend loads
curl -I https://sautai.com
# Expected: 200

# 2. Frontend can reach API (open browser, check Network tab)
# Go to https://sautai.com, log in, verify API calls go to api.sautai.com

# 3. Telegram bot still works
# Send a test message to the Sautai bot on Telegram — should get a response

# 4. Test a full flow: browse chefs, view profiles, etc.
```

**⛔ STOP if anything is broken. Rollback: revert frontend API URL to old domain. Old domains still work.**

---

## Phase 3: Remove Old Domains from Sautai Backend

**Goal:** Free `hoodunited.org` and `neighborhoodunited.org` for NBHD United.

⚠️ **Only proceed when Phase 2 is fully verified and has been stable for at least 24 hours.**

### Step 3.1: Remove custom domains from Azure

```bash
# Remove each old domain
az containerapp hostname delete \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname hoodunited.org \
  --yes

az containerapp hostname delete \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname www.hoodunited.org \
  --yes

az containerapp hostname delete \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname neighborhoodunited.org \
  --yes

az containerapp hostname delete \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname www.neighborhoodunited.org \
  --yes
```

### Step 3.2: Clean up Django settings

Remove old domains from `hood_united/settings.py` CORS fallback (~lines 141-144):
```python
# REMOVE these lines:
'https://hoodunited.org',
'https://www.hoodunited.org',
'https://neighborhoodunited.org',
'https://www.neighborhoodunited.org',
```

Also update `CORS_ALLOWED_ORIGINS` env var in Azure if it contains old domains.

### Step 3.3: Remove old DNS records

Go to the DNS registrar/provider for `hoodunited.org` and `neighborhoodunited.org`:
- Remove CNAME records pointing to `sautai-django-westus2`
- **Do NOT delete the domains** — you'll need them for NBHD United

### Step 3.4: Verify

```bash
# 1. api.sautai.com still works
curl -I https://api.sautai.com/admin/
# Expected: 200 or 302

# 2. Old domains no longer resolve to Sautai (may take time for DNS)
curl -I https://hoodunited.org/admin/
# Expected: connection error or different response

# 3. Sautai frontend still works end-to-end
# Browse https://sautai.com, test core flows
```

---

## Summary of Changes

| What | Before | After |
|------|--------|-------|
| Sautai frontend | `sautai.com` | `sautai.com` (unchanged) |
| Sautai backend | `hoodunited.org`, `neighborhoodunited.org` | `api.sautai.com` |
| Sautai Telegram webhook | `https://<old-domain>/api/telegram/webhook/` | `https://api.sautai.com/api/telegram/webhook/` |
| `hoodunited.org` | Sautai backend | Free (for NBHD United redirect) |
| `neighborhoodunited.org` | Sautai backend | Free (for NBHD United) |

## Rollback Plan

At any point during Phase 1-2:
- Old domains still work (they're not removed until Phase 3)
- Revert frontend API URL to old domain
- Revert Telegram webhook to old URL
- Everything goes back to exactly how it was

After Phase 3:
- Re-add old domains to Azure Container App
- Re-add DNS records
- Revert Django settings

---

*This directive is safe to execute step-by-step. Each phase is independent and verified before the next begins.*
