# Domain & Infrastructure Migration Plan

**Goal:** Clean domain separation between Sautai and NBHD United, cost-efficient.

## Current State

| Resource | Type | Domains |
|----------|------|---------|
| `sautai-frontend` | Azure Static Web App | `sautai.com`, `www.sautai.com` |
| `sautai-django-westus2` | Azure Container App | `hoodunited.org`, `www.hoodunited.org`, `neighborhoodunited.org`, `www.neighborhoodunited.org` |
| NBHD United | Not deployed yet | — |

**Problem:** Sautai's backend is using NBHD United's domains. Need to untangle.

---

## Target State

| Resource | Type | Domains | Purpose |
|----------|------|---------|---------|
| `sautai-frontend` | Azure Static Web App | `sautai.com` | Sautai React frontend |
| `sautai-django-westus2` | Azure Container App | `api.sautai.com` | Sautai Django backend + API |
| `nbhd-united` | Azure Container App | `neighborhoodunited.org` | NBHD United (Django + Next.js static) |
| — | DNS redirect | `hoodunited.org` → `neighborhoodunited.org` | Shorter alias |

---

## Migration Steps

### Phase 1: Add `api.sautai.com` to Sautai Backend (zero downtime)

**Do this first — everything else depends on it.**

1. **DNS:** Add CNAME record for `api.sautai.com` → `sautai-django-westus2.<region>.azurecontainerapps.io`
2. **Azure:** Add `api.sautai.com` as custom domain on `sautai-django-westus2` Container App
   ```bash
   az containerapp hostname add \
     --name sautai-django-westus2 \
     --resource-group <rg> \
     --hostname api.sautai.com
   
   # Bind managed certificate
   az containerapp hostname bind \
     --name sautai-django-westus2 \
     --resource-group <rg> \
     --hostname api.sautai.com \
     --environment <env-name> \
     --validation-method CNAME
   ```
3. **Django settings:** Add `api.sautai.com` to:
   - `ALLOWED_HOSTS`
   - `CSRF_TRUSTED_ORIGINS` → `https://api.sautai.com`
   - `CORS_ALLOWED_ORIGINS` → `https://api.sautai.com`
4. **Test:** Hit `https://api.sautai.com/admin/` — should load Django admin

### Phase 2: Update Sautai Frontend to Use `api.sautai.com`

1. **Frontend env:** Change `VITE_API_BASE_URL` (or equivalent) from `https://neighborhoodunited.org` → `https://api.sautai.com`
2. **Deploy frontend** to `sautai-frontend` Static Web App
3. **Test:** Full Sautai flow works — login, chef profiles, orders, Sous Chef chat
4. **Check external references:**
   - Telegram webhook URL → update to `https://api.sautai.com/chefs/api/telegram/webhook/`
   - Stripe webhook URL → update to `https://api.sautai.com/...`
   - Any OAuth callback URLs → update
   - Email links (chef approval emails) → check `FRONTEND_URL` setting points to `sautai.com`
   - Zoho/Gmail redirect URIs if applicable

### Phase 3: Remove Old Domains from Sautai Backend

Once Phase 2 is verified and everything works on `api.sautai.com`:

1. **Azure:** Remove custom domains from `sautai-django-westus2`:
   ```bash
   az containerapp hostname delete \
     --name sautai-django-westus2 \
     --resource-group <rg> \
     --hostname hoodunited.org
   
   # Repeat for www.hoodunited.org, neighborhoodunited.org, www.neighborhoodunited.org
   ```
2. **Django settings:** Remove `hoodunited.org` and `neighborhoodunited.org` from `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS`
3. **DNS:** Remove old CNAME records for `hoodunited.org` and `neighborhoodunited.org` pointing to Sautai

### Phase 4: Deploy NBHD United

1. **Build:** Single container with Django + Next.js static export
   - `next build && next export` → static files in `frontend/out/`
   - Serve via whitenoise or nginx sidecar
   - Django handles `/api/v1/...`, static files handle everything else
2. **Azure:** Create Container App `nbhd-united` in same environment
   ```bash
   az containerapp create \
     --name nbhd-united \
     --resource-group <rg> \
     --environment <env-name> \
     --image <acr>/nbhd-united:latest \
     --target-port 8000 \
     --ingress external
   ```
3. **DNS:** Point `neighborhoodunited.org` → `nbhd-united.<region>.azurecontainerapps.io`
4. **Azure:** Add custom domain + managed cert for `neighborhoodunited.org`
5. **DNS:** Set up `hoodunited.org` as a 301 redirect to `neighborhoodunited.org`
   - Option A: Cloudflare page rule (free)
   - Option B: Simple redirect app in same container environment
   - Option C: DNS-level redirect if registrar supports it

### Phase 5: Register Telegram Webhook

Once `neighborhoodunited.org` is live:

```bash
curl -X POST "https://api.telegram.org/bot<NBHD_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://neighborhoodunited.org/api/v1/telegram/webhook/",
    "secret_token": "<NBHD_TELEGRAM_WEBHOOK_SECRET>"
  }'
```

### Phase 6: Stripe Setup

1. Create products in Stripe dashboard:
   - "NBHD United Basic" — $5/mo
   - "NBHD United Plus" — $10/mo (optional, for Opus access)
2. Get price IDs, add to NBHD United settings:
   ```python
   STRIPE_PRICE_IDS = {
       "basic": env("STRIPE_PRICE_ID_BASIC", default=""),
       "plus": env("STRIPE_PRICE_ID_PLUS", default=""),
   }
   ```
3. Register Stripe webhook → `https://neighborhoodunited.org/api/v1/billing/webhook/`

---

## Cost Estimate

| Resource | Monthly Cost |
|----------|-------------|
| `sautai-django-westus2` (existing) | ~$15-20 (no change) |
| `sautai-frontend` (existing) | Free (Static Web App) |
| `nbhd-united` Container App | ~$10-15 (minimal CPU/RAM) |
| Managed certs | Free (Azure managed) |
| DNS | Already paying for domains |
| **Total new cost** | **~$10-15/mo** |

---

## Checklist

- [ ] Phase 1: Add `api.sautai.com` to Sautai backend
- [ ] Phase 1: Test Django admin on new domain
- [ ] Phase 2: Update Sautai frontend API URL
- [ ] Phase 2: Update Telegram webhook URL
- [ ] Phase 2: Update Stripe webhook URL
- [ ] Phase 2: Update any OAuth callback URLs
- [ ] Phase 2: Verify full Sautai flow on new domains
- [ ] Phase 3: Remove old domains from Sautai backend
- [ ] Phase 3: Clean up DNS records
- [ ] Phase 4: Build NBHD United container image
- [ ] Phase 4: Deploy to Azure Container Apps
- [ ] Phase 4: Add custom domain + cert
- [ ] Phase 4: Set up hoodunited.org redirect
- [ ] Phase 5: Register Telegram webhook
- [ ] Phase 6: Create Stripe products
- [ ] Phase 6: Register Stripe webhook

---

## Rollback

If anything breaks after Phase 2:
- Revert frontend env var back to old domain
- Old domains still exist on Sautai backend until Phase 3
- Zero data loss risk — this is just URL routing

**Don't start Phase 3 until Phase 2 is fully verified.**

*Created 2026-02-08*
