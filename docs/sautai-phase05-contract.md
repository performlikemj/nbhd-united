# NBHD ↔ sautai Phase 0.5 — M2M Contract Addendum v2

Extends `docs/sautai-phase0-contract.md`. Adds account linking and a small
funnel so meal planning can act as sautai's front door: unlinked users still get
real plans (shell accounts) but the ready message becomes a CLAIM link; users
with an existing sautai account link it with a one-time connect key so their real
dietary profile applies.

Auth is unchanged — every request carries `X-NBHD-Platform-Secret` (sautai reads
`NBHD_PLATFORM_SECRET`, NBHD reads `SAUTAI_PLATFORM_SECRET`).

## 1. POST /api/m2m/link/resolve/

Exchange a one-time connect key (minted in sautai, pasted into the NBHD console)
for the sautai user id. Single-use, 1h expiry.

- Request: `{"link_key": "...", "nbhd_tenant_id": "..."}`. The tenant id is
  NBHD's opaque tenant UUID string (non-blank, at most 255 characters).
- Optional request fields: `"nbhd_account_email": "..."` and
  `"nbhd_display_name": "..."` (strings at most 255 characters each). Both are
  informational/display-only and omitted when unavailable.
- 200: `{"status":"ok","sautai_user_id":<int>,"email":"...","nbhd_tenant_id":"<echoed>"}`
- Malformed request or missing/invalid tenant id → 400 `{"status":"error","code":"validation","detail":"..."}`
- Missing/invalid platform secret → 401 `{"status":"error","code":"invalid_secret"}`
- Unknown / expired / already-used key → 404 `{"status":"error","code":"invalid_key"}`
- Saturated resolve capacity → 503 `{"status":"error","code":"busy","detail":"..."}`;
  retry the exchange, matching `/generate/` busy handling.
- Unexpected consume failure → 500 `{"status":"error","code":"internal_error","detail":"..."}`

NBHD calls this SERVER-SIDE from the console connect endpoint, verifies the tenant
echo, and never stores the raw key (burn after resolve). See
`apps/integrations/link_views.py`.

## 2. Identity by `sautai_user_id`

`generate/` and `current/` accept `"sautai_user_id":<int>` as an alternative to
`user_email` (user_id wins if both are sent). The user_id path NEVER auto-creates:

- `generate/` unknown id → 404 `{"code":"unknown_user"}`
- `current/` unknown id → 404 `{"status":"not_found"}`

NBHD chooses the identity in `sautai_client.sautai_identity(tenant)`: a linked
`Integration(Provider.SAUTAI).sautai_user_id` wins, else the tenant email. On a
`404 unknown_user` for a linked generate, NBHD clears the link (email auto-create
resumes) and fails the job with a reconnect hint.

## 3. `regenerate`

`generate/` accepts `"regenerate": true` → REPLACES an existing `(user, week)`
plan honoring `user_prompt`, instead of the idempotent stale return.
`already_existed` keeps meaning "existed before this call". Carried on
`SautaiMealPlanJob.regenerate`; the plugin exposes an optional `regenerate` param.

## 4. Funnel fields on 200s

`generate/` and `current/` 200 bodies gain:

- `account_claimed` (bool)
- `plan_count` (int)
- `claim_link` (url) — present ONLY when `account_claimed` is false; carries `?src=nbhd`

NBHD stores these on `SautaiMealPlanJob.funnel`. The ready-notification uses
`claim_link` + `plan_count` for unclaimed accounts ("You've created N plans; claim
your sautai account…"), and the plain `web_link` for claimed/linked accounts.
Both carry a "powered by sautai" attribution.

## Fixture handshake

NBHD ships its own fixtures matching this contract under
`apps/integrations/fixtures/m2m/` (`link_resolve_ok`, `link_resolve_invalid`,
`generate_ok_funnel`, `generate_regenerated`, `generate_unknown_user`). **Once the
sautai side dumps its goldens, re-copy them byte-for-byte** (same handshake gate as
Phase 0) — the sync is a review-time task.
