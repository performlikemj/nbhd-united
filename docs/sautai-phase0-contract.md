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
  "user_email": "person@example.com",   // one of user_email / sautai_user_id
  "sautai_user_id": 501,                 // preferred when an account is linked
  "week_start": "2026-07-13",           // optional ISO date, a Monday; default = current week's Monday
  "user_prompt": "high protein, no pork", // optional free text
  "number_of_days": 7,                   // optional 1-7, default 7
  "regenerate": false                    // true replaces an existing plan for this user/week
}
```

Response 200:
```json
{
  "status": "ok",
  "user_created": false,          // true if a shell sautai account was auto-created for this email
  "already_existed": false,       // true if a plan already existed for (user, week) — idempotent return
  "complete": true,               // false when one or more requested days have zero populated meals
  "missing_days": [],             // ISO dates in the requested span with zero populated meals
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
Fast read. Request: one of `user_email` / `sautai_user_id`, plus optional
`{"week_start": "YYYY-MM-DD"}` (default current week).
- 200: `{"status":"ok", "complete":true, "missing_days":[], "plan": {<same plan shape>}, "web_link": "..."}`
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
- **Plan integrity**: generate and current return top-level `complete` and `missing_days`. A date is
  missing when it is inside the requested span and has zero populated meals. Consumers must never
  present `complete: false` or a non-empty `missing_days` week as complete.
- **Scoped repair**: a non-regenerate generate call for an existing partial week fills only missing
  slots and never changes existing meals. It remains the idempotent path; `regenerate: true` keeps
  its separate full-replacement meaning.
- **Replacement**: `regenerate: true` replaces the existing plan for (user, week). NBHD only
  forwards this after its runtime proxy has found a READY plan and the user has explicitly
  confirmed replacement.
- **PII posture**: NBHD proxy REHYDRATES the real email before calling sautai and sends only
  this minimal structured payload — never raw conversation.
- **Provider invariant (sautai)**: generation stays on its existing Groq path with the OpenAI
  allergen check — no new LLM calls, no provider crossing.

## NBHD runtime tool contract

The OpenClaw tools call NBHD's runtime proxy, not these M2M endpoints directly.

- Both generate and current-plan accept `week: "current" | "next"` (default `"current"`).
  NBHD resolves it to a Monday in the tenant's timezone. `week_start` is only for a user-named
  calendar date, takes precedence over `week`, and snaps backward to Monday.
- `regenerate: true` with no READY job for the target week is stripped and becomes a normal,
  non-destructive generation.
- `regenerate: true` with a READY job requires `confirm_replace: true`; without confirmation the
  proxy returns `status: "confirm_required"` with the existing plan and link and creates no job.
- A normal request for a week with a complete READY job returns `status: "exists"`, surfaces that
  plan, and creates no job. A prompt-bearing response explains that the new guidance was not
  applied and offers the confirm-gated replacement flow.
- If that READY job's captured integrity metadata has `complete: false` or non-empty
  `missing_days`, a non-regenerate request bypasses `exists` and enqueues scoped repair. Its ack
  says the missing days are being filled and existing meals will be left untouched. Absent fields
  on older cached jobs are treated as complete and retain the `exists` behavior.
- Current-plan proxy responses pass `complete` and `missing_days` through as top-level siblings of
  `plan`, including values recovered from a cached READY job's funnel metadata.
- Current-plan responses include `generation_in_progress` for a PENDING/GENERATING job created in
  the last 15 minutes so the assistant can explain the 1–2 minute asynchronous wait.
- At M2M egress, the worker snapshots `addressed_by` (`linked_id` or `email`) and the nullable linked
  `sautai_user_id` onto the job. It never stores the raw addressed email on the job.

## Fixture handshake (contract test gate)

sautai side dumps golden fixtures from the REAL views into `api/tests/fixtures/m2m/`. The shared
success fixtures (`current_ok.json`, `generate_ok.json`, `generate_ok_funnel.json`,
`generate_regenerated.json`, `generate_user_created.json`) are copied byte-for-byte to NBHD,
alongside NBHD's preserved error/not-found/link fixtures. Its proxy/QStash tests must parse them —
both sides decode the same bytes.
