# NBHD ↔ sautai Phase 0 — M2M Contract v1

Single source of truth for both sides. Any change to this contract must be made HERE first,
then mirrored in both implementations. Derived from
`nbhd-united/docs/sautai-nbhd-integration-research.md` (Phase 0, endorsed decisions 2026-07-10).

## Auth

Every request carries header `X-NBHD-Platform-Secret: <secret>`.
- sautai reads settings/env `NBHD_PLATFORM_SECRET`; compare with `hmac.compare_digest`.
- NBHD Django reads env/Key Vault `SAUTAI_PLATFORM_SECRET` (same value). Platform-level,
  control-plane to control-plane; never delivered to tenant containers.
- Missing/invalid → `401 {"status":"error","code":"invalid_secret"}`.

## Endpoints (all JSON; hosted under sautai `api/` app)

### 1. POST /api/m2m/meal-plan/generate/
Blocking (30–60s — the caller is NBHD's QStash worker; client timeout must be ≥120s).

Request:
```json
{
  "user_email": "person@example.com",   // required, the NBHD-verified email
  "week_start": "2026-07-13",           // optional ISO date, a Monday; default = current week's Monday
  "user_prompt": "high protein, no pork", // optional free text
  "number_of_days": 7                    // optional 1-7, default 7
}
```

Response 200:
```json
{
  "status": "ok",
  "user_created": false,          // true if a shell sautai account was auto-created for this email
  "already_existed": false,       // true if a plan already existed for (user, week) — idempotent return
  "plan": {
    "id": 123,
    "week_start": "2026-07-13",
    "week_end": "2026-07-19",
    "days": [
      {"day": "Monday", "meals": [{"meal_type": "Breakfast", "name": "..."},
                                    {"meal_type": "Lunch", "name": "..."},
                                    {"meal_type": "Dinner", "name": "..."}]}
    ]
  },
  "web_link": "https://sautai.com/..."   // deep link to view the plan (sautai web route)
}
```

Errors: `400 {"status":"error","code":"validation","detail":"..."}` ·
`401` as above · `500 {"status":"error","code":"generation_failed","detail":"<safe message>"}`
(never internal tracebacks).

### 2. POST /api/m2m/meal-plan/current/
Fast read. Request: `{"user_email": "...", "week_start": "YYYY-MM-DD"?}` (default current week).
- 200: `{"status":"ok", "plan": {<same plan shape>}, "web_link": "..."}`
- 404: `{"status":"not_found"}` (no auto-create on reads — unknown email is also 404)

### 3. GET /api/m2m/ping/
Secret-gated smoke: `200 {"status":"ok","service":"sautai-m2m","version":1}`.

## Semantics both sides must honor

- **User resolve-or-create** (generate only): case-insensitive email match on CustomUser; if
  absent, create a shell account (unusable password, email recorded, generation-capable).
  Report via `user_created`. Reads never create.
- **Idempotency**: `create_meal_plan_for_user()` is idempotent per (user, week);
  `already_existed: true` signals the plan was already there. NBHD's QStash task must also be
  idempotent per job id (safe on redelivery).
- **PII posture**: NBHD proxy REHYDRATES the real email before calling sautai and sends only
  this minimal structured payload — never raw conversation.
- **Provider invariant (sautai)**: generation stays on its existing Groq path with the OpenAI
  allergen check — no new LLM calls, no provider crossing.

## Fixture handshake (contract test gate)

sautai side dumps golden fixtures from the REAL views into
`api/tests/fixtures/m2m/` (generate_ok.json, generate_user_created.json, current_ok.json,
current_not_found.json, error_invalid_secret.json). These exact files are copied to the NBHD
repo and its proxy/QStash tests must parse them — both sides decode the same bytes.
