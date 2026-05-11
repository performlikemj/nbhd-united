# `~/.claude/.credentials.json` schema

Authoritative contract for the file the `claude` binary reads at startup and rewrites on refresh. Derived from three sources:
- OpenClaw 2026.5.7 parser at `/tmp/oc-pack/package/dist/store-CSOk8Bno.js:62-82` (`parseClaudeCliOauthCredential`)
- Claude Code 2.1.123 binary strings at `/tmp/cc-pack/native/package/claude`
- OpenClaw upstream docs at `/tmp/openclaw-audit/openclaw-core/docs/help/testing-live.md:202`

## Top-level shape

```json
{
  "claudeAiOauth": {
    // fields below
  }
}
```

`claudeAiOauth` is the only documented top-level key. OpenClaw's parser specifically extracts `JSON.parse(content).claudeAiOauth` — anything else is ignored.

## Fields inside `claudeAiOauth`

| Field | Type | Required for BYO Claude OAuth? | Notes |
|---|---|---|---|
| `accessToken` | non-empty string | **Required** | Short-lived (hours). Refreshed by binary in-place. |
| `refreshToken` | string | **Required for OAuth subscription routing** | When present + non-empty, `parseClaudeCliOauthCredential` returns `type:"oauth"`. When absent, returns `type:"token"` (setup-token shape — extra-usage billing, NOT what we want). |
| `expiresAt` | finite positive number (ms epoch) | **Required** | OpenClaw validates `typeof expiresAt === "number"` + finite. Binary refreshes when current time approaches this. |
| `scopes` | array of strings | Optional but expected | OpenClaw passes through to its credential model. Setup-token only carries `user:inference`; full `claude login` carries broader scopes including `user:profile`. |
| `subscriptionType` | string | Optional, **strongly recommended for display** | OpenClaw upstream test docs explicitly check this field (`"~/.claude/.credentials.json` with `claudeAiOauth.subscriptionType`"). Values seen: `"max"`, `"pro"`. Use this for `provider_email` display on `BYOCredential` row. |
| `emailAddress` | string | Optional | Account email. Privacy-safe to display in UI alongside `subscriptionType`. |
| `accountUuid` | string | Optional | Stable account identifier. Not needed for routing. |
| `organizationUuid` | string | Optional | Workspace/org identifier if logged into a team. Most personal accounts won't have it. |
| `organizationName` | string | Optional | |
| `organizationRole` | string | Optional | |
| `isOwner` | boolean | Optional | |

All "Optional" fields should be **accepted and passed through unchanged** by the upload validator — Anthropic may add fields without notice. Do not strict-mode-reject unknown keys.

## Validation rules for the upload endpoint

`apps/byo_models/views.py:74 POST` must enforce, on the new `oauth_credentials` mode:

1. **Body is parseable JSON.** Reject 400 otherwise. Filter ensures body never logs.
2. **Has `claudeAiOauth` object.** Reject 400 if absent or not an object.
3. **`claudeAiOauth.accessToken` is non-empty string.** Reject 400 otherwise. Min length ~80 chars in observed real tokens; do NOT bound max length too tightly — leave room for Anthropic schema changes.
4. **`claudeAiOauth.refreshToken` is non-empty string.** Reject 400 with explicit message: *"This looks like a `claude setup-token` token (no refresh). For subscription billing, run `claude login` instead and upload the resulting `~/.claude/.credentials.json`."*
5. **`claudeAiOauth.expiresAt` is a positive finite number.** Reject 400 otherwise.
6. **Token isn't expired at upload time.** Reject 400 if `expiresAt < now()`. Friendly: *"This token has already expired. Re-run `claude login` and upload the fresh file."*
7. **Total body size cap.** Set ~8 KB — real files are ~2 KB; bigger is suspicious.

## Persisted on `BYOCredential` row

- `mode = "oauth_credentials"` (new value — see model migration in Phase 1)
- `status = "pending"` until first successful agent turn flips it to `verified`
- `provider_email` ← string built from available identity fields. Suggested format: `f"{subscriptionType.title()} ({emailAddress})"` if both present, else fall back to `emailAddress` or `subscriptionType` alone. Empty if neither.
- `token_expires_at` ← parsed from `expiresAt` (ms → datetime)
- `key_vault_secret_name` ← `f"{tenant.key_vault_prefix}-byo-anthropic-oauth"`
- `seed_version` ← bumped on every upload (already wired at `apps/byo_models/services.py:141`)

## Storage

- **KV value**: the entire `{"claudeAiOauth": {...}}` JSON blob, serialized as a single string, stored at `{tenant.key_vault_prefix}-byo-anthropic-oauth`. KV is the source of truth.
- **Mirror to share**: `entrypoint.sh` reads via env var binding (`BYO_CLAUDE_CREDS_JSON`), writes to `/home/node/.openclaw/claude-state/.credentials.json` when `BYO_CLAUDE_SEED_VERSION` doesn't match the on-disk marker. See [07-billing-policy-flag.md](./07-billing-policy-flag.md) about whether this even achieves the billing goal.

## Example real file (sanitized, fields verified against parser)

```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat01-XXXXX...",
    "refreshToken": "sk-ant-ort01-XXXXX...",
    "expiresAt": 1746028800000,
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "max",
    "emailAddress": "user@example.com",
    "accountUuid": "01234567-89ab-cdef-0123-456789abcdef"
  }
}
```

## Failure modes the validator must produce friendly errors for

| Symptom | Diagnosis | Error message to surface |
|---|---|---|
| No `claudeAiOauth` key | User pasted a different file shape (e.g. `.claude.json` settings file) | *"That doesn't look like a credentials file. Did you mean to upload `~/.claude/.credentials.json`?"* |
| Has `claudeAiOauth.accessToken` but no `refreshToken` | Setup-token shape | *"This token is from `claude setup-token`. For subscription billing, please re-run `claude login` and upload the resulting `~/.claude/.credentials.json` instead."* |
| `expiresAt` in the past | Stale file from an old login | *"This token has already expired. Re-run `claude login` and upload the fresh file."* |
| JSON parse error | Truncated paste, wrong file | *"Couldn't read that as JSON. Make sure you uploaded the entire file."* |
| Body > 8 KB | Wrong file or attack | Generic *"That file is unusually large. Make sure you uploaded `~/.claude/.credentials.json`."* |

## Reference

OpenClaw parser (verbatim from `store-CSOk8Bno.js:62-82`):

```js
function parseClaudeCliOauthCredential(claudeOauth) {
  const accessToken = claudeOauth.accessToken;
  const refreshToken = claudeOauth.refreshToken;
  const expiresAt = claudeOauth.expiresAt;
  if (typeof accessToken !== "string" || !accessToken) return null;
  if (typeof expiresAt !== "number" || !Number.isFinite(expiresAt) || expiresAt <= 0) return null;
  if (typeof refreshToken === "string" && refreshToken) return {
    type: "oauth", provider: "anthropic",
    access: accessToken, refresh: refreshToken, expires: expiresAt
  };
  return { type: "token", provider: "anthropic", token: accessToken, expires: expiresAt };
}
```
