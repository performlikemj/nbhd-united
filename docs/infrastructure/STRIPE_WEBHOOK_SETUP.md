# Stripe Webhook Setup & Account Migration

The Django control plane only grants credit, provisions tenants, and handles
cancellations **from verified Stripe webhooks**. A checkout session creating
successfully is *not* enough — if the webhook never arrives or fails signature
verification, a customer can be **charged with no credit granted and no
visible error**. This doc is the runbook for getting (and keeping) that path
correct, especially across a Stripe account or test→live migration.

## The invariant

`DJSTRIPE_WEBHOOK_SECRET` MUST be the signing secret of a webhook endpoint
**registered in the same Stripe account + mode that `STRIPE_LIVE_MODE` /
`STRIPE_LIVE_SECRET_KEY` point at.** A secret from a different account (or
test vs live) makes every event fail `stripe.Webhook.construct_event` →
`apps/billing/views.py` returns HTTP 400 and logs `Stripe webhook verification
failed`. Billing silently stops working.

Stripe object IDs encode their account in the suffix (e.g. `sub_1T0Goj`**`7UcLgRWeMY`**
lives in account `acct_…7UcLgRWeMY`). If stored `stripe_customer_id` /
`stripe_subscription_id` values and your live key disagree on the suffix, you
have a cross-account mismatch — see "Account migration" below.

## First-time / per-account setup

1. Stripe Dashboard → **Developers → Webhooks → Add endpoint**.
2. Endpoint URL = the Django app URL + `/api/v1/billing/webhook/`, e.g.
   `https://nbhd-django-westus2.<region>.azurecontainerapps.io/api/v1/billing/webhook/`
3. Select events (the handlers in `apps/billing/views.py:stripe_webhook`):
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `charge.refunded`
   - `charge.dispute.created`
   - `customer.subscription.deleted`
   - `customer.subscription.updated`
   - `invoice.payment_failed`
4. Copy the endpoint's **Signing secret** (`whsec_…`).
5. Store it in Key Vault and point the Container App env at it (never inline a
   stale value — that is how the OpenRouter key broke; keep Stripe KV-backed):
   ```bash
   az keyvault secret set --vault-name kv-nbhd-prod --name djstripe-webhook-secret --value <whsec_…>
   # ensure the Container App env DJSTRIPE_WEBHOOK_SECRET → secretRef djstripe-webhook-secret
   ```
6. Set the matching subscription price + keys (all from the **same** account):
   - `STRIPE_LIVE_MODE=True`
   - `STRIPE_LIVE_SECRET_KEY` = that account's `sk_live_…`
   - `STRIPE_PRICE_STARTER` = a price in that account (→ `settings.STRIPE_PRICE_ID`)

## Verify (do this after every account change)

End-to-end is the only true proof. On an internal/exempt tenant (e.g. the
canary), buy the smallest credit pack and watch the logs:

```bash
az containerapp logs show --name nbhd-django-westus2 -g rg-nbhd-prod --tail 100 --follow false \
  | grep -iE "Stripe webhook:|granted .* credit|verification failed|No such"
```

Expect, in order:
- checkout session created (no `No such customer`),
- `Stripe webhook: checkout.session.completed`,
- `granted 5.00 credit to tenant <id>`.

Then refund the charge in the Dashboard and confirm `charge.refunded` →
`clawed back …`. A test webhook from the Dashboard (Send test event) checks
signature verification but does **not** exercise the grant (no real session
metadata), so prefer a real small top-up.

## Account migration (old → new)

When the live account changes (the 2026-06 `7UcLgRWeMY` → `K0gdcRr9J4` move):

1. Register a **new** webhook endpoint in the new account; set
   `DJSTRIPE_WEBHOOK_SECRET` to its secret. **Never reuse a secret across
   accounts.**
2. Repoint `STRIPE_LIVE_SECRET_KEY` + `STRIPE_PRICE_STARTER` to the new account.
3. Clear stale linkage on existing tenants so checkout/portal mint fresh
   customers in the new account:
   ```sql
   update tenants set stripe_customer_id = '', stripe_subscription_id = ''
   where stripe_customer_id <> '' or stripe_subscription_id <> '';
   ```
   The code self-heals a stale `stripe_customer_id` at checkout/portal time
   (`_is_missing_customer_error` / `_clear_stale_customer` in
   `apps/billing/views.py`) and a stale `stripe_subscription_id` on
   account-delete/cancel (`_is_missing_subscription_error`), but clearing
   up-front avoids the first failed call per tenant.
4. Re-run the Verify step above.
