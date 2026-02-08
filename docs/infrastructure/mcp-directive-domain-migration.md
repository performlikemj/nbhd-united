# MCP Directive: Domain Migration — Sautai Backend to api.sautai.com

**Objective:** Move Sautai's Django backend from `hoodunited.org` / `neighborhoodunited.org` to `api.sautai.com`, freeing those domains for the NBHD United project.

**Safety level:** HIGH — this affects a live production backend. Each phase must be verified before proceeding to the next. Do NOT skip verification steps.

**Important context:**
- All environment variables for `sautai-django-westus2` are stored in **Azure Key Vault** and referenced by the Container App. Nothing is hardcoded in the container config.
- Django reads these via `os.getenv()`. When you update a Key Vault secret, you must **restart the Container App** (or create a new revision) to pick up the new values.
- The relevant env vars: `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `TELEGRAM_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_SECRET`

---

## Prerequisites

Before starting, confirm:
- [ ] You have access to Cloudflare DNS for `sautai.com`
- [ ] You have access to Azure portal or CLI for the resource group containing `sautai-django-westus2`
- [ ] You have access to the Azure Key Vault used by `sautai-django-westus2`
- [ ] You know the current FQDN of `sautai-django-westus2`

To find the FQDN:
```bash
az containerapp show \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --query "properties.configuration.ingress.fqdn" \
  -o tsv
```

To find which Key Vault is used:
```bash
az containerapp show \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --query "properties.template.containers[0].env" \
  -o table
```
Look for env vars with `secretRef` or Key Vault reference URIs.

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

### Step 1.3: Update Key Vault secrets

All Django config is driven by environment variables stored in Azure Key Vault. Update these secrets:

**1. `ALLOWED_HOSTS`** — Append `api.sautai.com`

```bash
# First, read the current value
az keyvault secret show \
  --vault-name <KEY_VAULT_NAME> \
  --name ALLOWED-HOSTS \
  --query "value" -o tsv

# Update with api.sautai.com appended (comma-separated)
az keyvault secret set \
  --vault-name <KEY_VAULT_NAME> \
  --name ALLOWED-HOSTS \
  --value "<EXISTING_VALUE>,api.sautai.com"
```

⚠️ Key Vault secret names use hyphens, not underscores. The secret name might be `ALLOWED-HOSTS` or `ALLOWEDHOSTS` — check the actual name first:
```bash
az keyvault secret list \
  --vault-name <KEY_VAULT_NAME> \
  --query "[?contains(name, 'ALLOWED') || contains(name, 'CORS') || contains(name, 'CSRF')].name" \
  -o tsv
```

**2. `CORS_ALLOWED_ORIGINS`** — Append `https://api.sautai.com`

```bash
# Read current value
az keyvault secret show \
  --vault-name <KEY_VAULT_NAME> \
  --name CORS-ALLOWED-ORIGINS \
  --query "value" -o tsv

# Update with https://api.sautai.com appended
az keyvault secret set \
  --vault-name <KEY_VAULT_NAME> \
  --name CORS-ALLOWED-ORIGINS \
  --value "<EXISTING_VALUE>,https://api.sautai.com"
```

Note: If `CORS_ALLOWED_ORIGINS` is NOT set in Key Vault, Django falls back to a hardcoded list in `settings.py` that already includes the old domains and `sautai.com`. In that case, you need to either:
- Set it in Key Vault with the full list including `https://api.sautai.com`, OR
- The `CSRF_TRUSTED_ORIGINS` in production mode is auto-built from `CORS_ALLOWED_ORIGINS`, so updating CORS covers CSRF too.

### Step 1.4: Restart the Container App

Key Vault secret changes are NOT picked up automatically. Restart to load new values:

```bash
# Create a new revision to pick up updated secrets
az containerapp revision restart \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --revision <LATEST_REVISION_NAME>
```

Or force a new revision:
```bash
az containerapp update \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --revision-suffix refresh-$(date +%s)
```

### Step 1.5: Verify

Run ALL of these checks before proceeding:

```bash
# 1. DNS resolves
dig api.sautai.com CNAME +short
# Expected: sautai-django-westus2.<region>.azurecontainerapps.io

# 2. HTTPS works on new domain
curl -I https://api.sautai.com/admin/
# Expected: 200 or 302 (redirect to login)

# 3. Old domains STILL work (CRITICAL)
curl -I https://hoodunited.org/admin/
curl -I https://neighborhoodunited.org/admin/
# Expected: 200 or 302

# 4. API responds on new domain
curl https://api.sautai.com/chefs/api/public/approved-chefs/
# Expected: JSON response
```

**⛔ STOP if any check fails. Do NOT proceed to Phase 2.**

---

## Phase 2: Switch Sautai Frontend to `api.sautai.com`

**Goal:** Frontend talks to `api.sautai.com` instead of old domains.

### Step 2.1: Identify current API base URL

The Sautai frontend is a Vite/React app on Azure Static Web App `sautai-frontend`. Find the API base URL:
- Check for `VITE_API_BASE_URL` or similar in the Static Web App application settings
- Or search the frontend source for `hoodunited.org` or `neighborhoodunited.org`

⚠️ **Vite `VITE_*` env vars are baked in at build time.** Changing a Static Web App application setting for a `VITE_*` var requires a **rebuild and redeploy** of the frontend, not just an env var update.

### Step 2.2: Update frontend API base URL

Change the API base URL to `https://api.sautai.com`

**If the value is in a `.env.production` or similar build-time config:**
- Update the value in the repo
- Rebuild and redeploy the frontend

**If the value is a runtime config (unlikely for Vite but check):**
```bash
az staticwebapp appsettings set \
  --name sautai-frontend \
  --resource-group <RESOURCE_GROUP> \
  --setting-names "VITE_API_BASE_URL=https://api.sautai.com"
```

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

The `SAUTAI_BOT_TOKEN` and `SAUTAI_TELEGRAM_WEBHOOK_SECRET` values are in the Key Vault:
```bash
az keyvault secret show --vault-name <KEY_VAULT_NAME> --name TELEGRAM-BOT-TOKEN --query "value" -o tsv
az keyvault secret show --vault-name <KEY_VAULT_NAME> --name TELEGRAM-WEBHOOK-SECRET --query "value" -o tsv
```

### Step 2.4: Update Stripe webhook URL (if applicable)

1. Go to Stripe Dashboard → Developers → Webhooks
2. Find the webhook endpoint pointing to `hoodunited.org` or `neighborhoodunited.org`
3. Update the URL to `https://api.sautai.com/<same-path>`
4. **Do NOT delete the old webhook** — update the URL in place

### Step 2.5: Check for OAuth callback URLs

Check if any OAuth providers have redirect URIs registered with old domains:
- Google (Gmail, Calendar) OAuth credentials
- Any other OAuth providers

If found, add `https://api.sautai.com` as an authorized redirect URI. Keep old URIs temporarily.

### Step 2.6: Verify

```bash
# 1. Frontend loads
curl -I https://sautai.com
# Expected: 200

# 2. Frontend reaches API (open browser, check Network tab)
# Go to https://sautai.com, log in, verify API calls go to api.sautai.com

# 3. Telegram bot still works
# Send a test message to the Sautai bot on Telegram — should get a response

# 4. Full flow test: browse chefs, view profiles, any Stripe-related actions
```

**⛔ STOP if anything is broken. Rollback: revert frontend API URL to old domain, revert Telegram webhook. Old domains still work.**

---

## Phase 3: Remove Old Domains from Sautai Backend

**Goal:** Free `hoodunited.org` and `neighborhoodunited.org` for NBHD United.

⚠️ **Only proceed when Phase 2 is fully verified and has been stable for at least 24 hours.**

### Step 3.1: Remove custom domains from Azure

```bash
az containerapp hostname delete \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname hoodunited.org --yes

az containerapp hostname delete \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname www.hoodunited.org --yes

az containerapp hostname delete \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname neighborhoodunited.org --yes

az containerapp hostname delete \
  --name sautai-django-westus2 \
  --resource-group <RESOURCE_GROUP> \
  --hostname www.neighborhoodunited.org --yes
```

### Step 3.2: Clean up Key Vault secrets

Update `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS` in Key Vault to remove the old domains:

```bash
# Read current ALLOWED_HOSTS, remove hoodunited.org and neighborhoodunited.org entries
az keyvault secret show --vault-name <KEY_VAULT_NAME> --name ALLOWED-HOSTS --query "value" -o tsv
# Edit the value, removing old domains, then:
az keyvault secret set --vault-name <KEY_VAULT_NAME> --name ALLOWED-HOSTS --value "<CLEANED_VALUE>"

# Same for CORS_ALLOWED_ORIGINS
az keyvault secret show --vault-name <KEY_VAULT_NAME> --name CORS-ALLOWED-ORIGINS --query "value" -o tsv
# Remove https://hoodunited.org, https://www.hoodunited.org, https://neighborhoodunited.org, https://www.neighborhoodunited.org
az keyvault secret set --vault-name <KEY_VAULT_NAME> --name CORS-ALLOWED-ORIGINS --value "<CLEANED_VALUE>"
```

### Step 3.3: Also clean up the hardcoded fallbacks in Django settings

In `hood_united/settings.py`, remove the old domains from the CORS fallback block (~lines 141-144):
```python
# REMOVE these lines:
'https://hoodunited.org',
'https://www.hoodunited.org',
'https://neighborhoodunited.org',
'https://www.neighborhoodunited.org',
```

Add `https://api.sautai.com` if not already present in the fallback list.

Deploy this code change and restart the container app.

### Step 3.4: Remove old DNS records

Go to the DNS registrar/provider for `hoodunited.org` and `neighborhoodunited.org`:
- Remove CNAME/A records pointing to `sautai-django-westus2`
- **Do NOT delete the domains themselves** — they'll be used for NBHD United

### Step 3.5: Verify

```bash
# 1. api.sautai.com still works
curl -I https://api.sautai.com/admin/
# Expected: 200 or 302

# 2. Old domains no longer resolve to Sautai
curl -I https://hoodunited.org/admin/
curl -I https://neighborhoodunited.org/admin/
# Expected: connection error or timeout

# 3. Full Sautai flow still works on sautai.com + api.sautai.com
```

---

## Summary of Changes

| What | Before | After |
|------|--------|-------|
| Sautai frontend | `sautai.com` | `sautai.com` (unchanged) |
| Sautai backend | `hoodunited.org`, `neighborhoodunited.org` | `api.sautai.com` |
| Sautai Telegram webhook | `https://<old-domain>/api/telegram/webhook/` | `https://api.sautai.com/api/telegram/webhook/` |
| Key Vault `ALLOWED_HOSTS` | includes old domains | includes `api.sautai.com`, old domains removed |
| Key Vault `CORS_ALLOWED_ORIGINS` | includes old domains | includes `https://api.sautai.com`, old domains removed |
| `hoodunited.org` | Sautai backend | Free (for NBHD United redirect) |
| `neighborhoodunited.org` | Sautai backend | Free (for NBHD United) |

## Rollback Plan

**During Phase 1-2** (old domains still attached):
- Revert frontend API URL to old domain
- Revert Telegram webhook to old URL
- Everything goes back to exactly how it was — zero risk

**After Phase 3** (old domains removed):
- Re-add old domains to Azure Container App
- Re-add DNS records
- Restore old values in Key Vault secrets
- Restart container app

---

*This directive is safe to execute step-by-step. Each phase is independent and verified before the next begins. All configuration is managed through Azure Key Vault — no container rebuilds needed for env var changes, only restarts.*
