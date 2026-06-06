# Cron messages must ground on current state, not carried-forward narrative

**Status:** in progress (this PR) · **Date:** 2026-06-06 · **Tenant of record:** canary `148ccf1c`

## Summary

Scheduled/proactive messages (Morning Briefing, Heartbeat, Evening Check-in,
etc.) are LLM-authored. They compose from the **daily note + memory** — a
*narration* layer the crons both write and read — instead of re-deriving from
each domain's **system of record** (typed `Task`/`Goal` rows, the finance
ledger). When the user closes something out, the system of record updates
correctly, but the next cron parrots the stale narration and re-raises the
closed item. The `GROUNDING CONTRACT` in the cron preamble only covered
*quantitative* claims (numbers), so *status/existence* claims ("still pending?")
slipped through.

This is not finance-specific. It surfaced first on a paused-finance loan, but
the cleanest proof is a **pure typed task (TEIN SDS)** that was marked `done`
and resurfaced the next morning with no finance involvement at all.

## Worked example — 2026-06-05 → 06-06 (canary)

On the evening of June 5 the user told the assistant, in chat, that he paid the
student loans and shipped the TEIN shock absorbers. The assistant acknowledged
**and persisted to the system of record**:

| When (JST) | Event | Source of truth |
|---|---|---|
| 06-05 15:39 | typed task *"Follow up on TEIN Safety Data Sheet (SDS)"* → `done` | `journal_tasks` |
| 06-05 21:31 | typed task *"Verify student loan minimum payment was covered (due June 5)"* → `done` | `journal_tasks` |
| 06-05 21:31 | typed task *"Follow up with RHD Japan to confirm TEIN SDS shipment"* → `done` | `journal_tasks` |
| 06-06 00:27 | assistant (chat): *"TEIN shipped, loans paid, workout done. Three for three."* | main session |
| **06-06 06:03** | **Morning Briefing (cron): *"Student loan… Was it paid? … TEIN SDS… Any confirmation from RHD Japan?"*** | **daily-note narration** |
| **06-06 07:0x** | **Heartbeat (cron): *"Quick nudge — TEIN SDS… we haven't confirmed it's done yet."*** | **daily-note narration** |
| 06-06 07:08 | same run later writes to the note: *"TEIN SDS + student loan both verified done"* | (derived sections were correct) |

The store was correct **~8.5 h before** the nag. Two independent tells prove the
nag read narration, not state:

1. The 06:03 message cited *"overdue as of May 23"* — the **due date of the old,
   superseded task**, not the June-5 task that was already `done`.
2. The same cron's own derived sections (Open Tasks, Focus, Lessons) listed the
   loan and TEIN as resolved. The *message* step didn't ground on the data the
   *note* step had already computed.

## Root cause

- **Narration is treated as state.** Crons read `nbhd_daily_note_get` + memory
  to decide what to surface. Those are append-only narration that the crons
  themselves wrote; yesterday's output becomes today's "evidence" (the
  "top priority N days running" loop counts itself).
- **Grounding was prompt-only and numbers-only.** `_CRON_CONTEXT_PREAMBLE`
  step 5 required tool-sourced *quantities*. "Is TEIN still open?" is a status
  claim, not a number, so it was never covered.
- **Session isolation.** The chat acknowledgment lived in the main session;
  crons run isolated. The only bridge is the system of record — which *was*
  updated, but the crons don't read it as authoritative.
- **Finance-pause twist (loan only).** Gravity is paused platform-wide
  (`GRAVITY_ENABLED=False`, PR #759), so the finance tools aren't loaded and a
  payment **can't be written to the ledger**. The loan-paid fact can only live
  as a `done` task. Worse: `build_journal_status` computed obligations from the
  ledger gated on `finance_enabled` (still `True`), so even the projection would
  have reported the loan "unpaid" — there is no June transaction because none
  *could* be recorded.

## The fix (this change)

1. **`apps/journal/status_projection.py`** — gate obligations on
   `tenant.finance_active` (folds in the `GRAVITY_ENABLED` pause), not
   `finance_enabled`. While paused, the ledger is unwritable so its paid/unpaid
   projection is unreliable → omit obligations entirely. This is the
   paused-finance rule at the data layer.
2. **`apps/integrations/runtime_views.py` + `urls.py`** — new internal-auth
   runtime endpoint `RuntimeCurrentStatusView`
   (`/runtime/<tenant>/current-status/`) returning `build_journal_status` —
   the same projection the web journal page uses, now reachable by containers.
3. **`runtime/openclaw/plugins/nbhd-journal-tools/`** — new `nbhd_current_status`
   tool wrapping that endpoint (mirrors `nbhd_task_list`). *Requires an OpenClaw
   image rebuild to reach containers.*
4. **`apps/orchestrator/config_generator.py`** (`_CRON_CONTEXT_PREAMBLE`) —
   - Step 2 now leads with `nbhd_current_status` as the authoritative
     as-of-now snapshot, **with a fallback** to
     `nbhd_task_list({status:'open'})` + `nbhd_goal_list({status:'active'})` so
     the fix works *before* the image rebuild and is deployment-order safe.
   - Step 5 (GROUNDING CONTRACT) extends to **status/existence** claims and adds
     an explicit **do-not-resurface-closed-work** rule plus the paused-finance
     silence rule.
5. **`status_registry.py` (+ refactor of `status_projection.py`)** — the snapshot
   is now the **union of pluggable status providers**, not a hand-wired function.
   Each feature registers a provider (`key`, `enabled(tenant)`,
   `provide(tenant, today) -> dict`); `build_journal_status` runs every enabled
   one and merges the results. The three built-ins (tasks, goals, finance) are
   now providers. This is what keeps **future features from being left behind**:
   a feature ships with its provider and is in the snapshot automatically; a
   missing/paused domain contributes nothing (safe-silent); a failing provider is
   isolated under an `unavailable` key. `test_status_registry` pins the built-ins
   and proves a freshly-registered provider is auto-included.

### Why this fixes both cases

- **TEIN**: the `done` task is absent from the snapshot's `open_tasks`; the
  no-resurface rule forbids raising anything not in the snapshot. Fixed with
  no finance logic, and works via the fallback even before the new tool ships.
- **Loan**: with `finance_active` false under the pause, the snapshot carries no
  obligations, and step 5 forbids raising finance from any other source.

### Deployment ordering

Django + prompt changes ship on the next Django deploy (config push re-renders
the cron preamble fleet-wide via the reconciler — the preamble's opening line is
unchanged, so stored crons still classify as default and roll forward). The
`nbhd_current_status` tool becomes callable only after the next OpenClaw image
build; until then the step-2 fallback carries the fix.

## Adding a feature later (the provider contract)

A new domain plugs in without touching the cron/grounding layer:
- register from your app's `AppConfig.ready()`: a `key`, an `enabled(tenant)`
  gate, and `provide(tenant, today) -> dict` (merged into the snapshot);
- **scope every read to the tenant** (Postgres RLS is the backstop, not the
  primary control), **use the ORM only** (parameterized — no string-built SQL,
  no injection surface), and return **data, not instructions** — the snapshot is
  reported to the LLM as untrusted content, never executed;
- the generic shape means a reshaped domain (e.g. finance returning *money
  saved* instead of *money owed*) needs no change to the grounding rule, the
  cron prompts, or `build_journal_status`.

## Coverage across ALL crons (not just system crons)

System crons bake the full preamble at seed time. Custom crons take other paths
(typed patterns built by handlers; freeform/agent crons authored in the
container) and never hit `_prepare_cron_prompt`. To ground them too, the
`nbhd-cron-enforcement` plugin — which fires on **every** cron — now fetches a
fire-time directive from Django
(`GET /runtime/<tenant>/crons/<name>/grounding/`) and injects a **lightweight
grounding rule** (`CRON_GROUNDING_RULE`) via `before_prompt_build`:

- Django decides per cron by inspecting the stored message: one that already
  contains the preamble marker (system seed jobs) → `inject=false` (no
  double-injection); every other cron — typed, user/freeform, legacy,
  agent-created, or unknown (no row) → `inject=true` + the rule.
- The rule is deliberately light and **verbatim-safe**: a `pure_reminder`
  ("send this exact text, call no other tools, stop") makes no status claims,
  so the rule is a no-op for it — it won't break verbatim patterns.
- Takes effect once the new OpenClaw image ships to the container; the Django
  endpoint + policy ship on the next Django deploy.

## Out of scope / follow-ups

- Duplicate journal writes (the 02:00 background summary wrote ~10× on 06-05)
  thicken the narration the next cron ingests — journal-write idempotency.
- Memory holds stale *values* (e.g. "$240.90 overdue as of May 23"); memory
  should hold durable facts, not live values.
- The Morning Briefing template still has its own legacy `- [ ]` cross-reference
  steps; the preamble change supersedes them, but they could be slimmed later.
