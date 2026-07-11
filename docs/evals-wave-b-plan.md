# Wave B — Journey Canary Suite build plan

*Canonical build plan for Wave B of the production eval system. Companion to `docs/evals-directive.md` (the standing invariants every suite conforms to) and `project_evals_system` memory. Authored by the Wave B planner against `origin/main` @ `4e4b60af`; committed as the reference all Wave B PRs (B0–B6) are cut against.*

> **[team-lead amendment — provisioning scope]** This plan was drafted for **two** synthetic tenants (`eval-journey` + `eval-behavior`). The team lead has narrowed Wave B to **`eval-journey` only** — `eval-behavior` is **deferred to Wave D** (no idle container spun up now). Wherever the plan below says "two synthetic tenants" or "create both," read it as: **provision/target only `eval-journey` in Wave B; `eval-behavior` stays a settings/plumbing stub**. PR-B0 still plumbs **both** env-var names (`EVAL_JOURNEY_TENANT_ID` *and* `EVAL_BEHAVIOR_TENANT_ID`) so Wave D needs no settings change — only `eval-journey` gets a real tenant id + PAT + container in Wave B. Inline `[team-lead amendment]` markers below flag the two provisioning spots.

---

═══════════════════════════════════════════════════════════════════
PART 1 — probes + provisioning
═══════════════════════════════════════════════════════════════════

# Wave B — Journey Canary Suite build plan (1/2: probes + provisioning)

*Investigated against `origin/main` @ `4e4b60af` (local checkout is 94 commits stale — every anchor is at that sha). **Zero migrations in this wave:** `eval_runs`/`eval_results` and `Tenant.is_synthetic` already exist from Wave A. Wave B is pure app code + settings + QStash schedules.*

## The three facts that shape every probe
1. **`replied_at` proves nothing.** It's stamped on *every* terminal transition incl. `budget_exhausted`/`empty_response`/`stale`/`dropped` (`apps/router/pending_queue.py:1703-1715` success vs `1732-1738`/`1745-1760` errors; the no-container budget path stamps it at row creation, `apps/router/chat_views.py:397-407`). Real assertion is `status=="ready" AND error==""`.
2. **~300s wall-clock per QStash task.** `trigger_task` runs it **inline** (`execute_task_sync`, `apps/cron/views.py:401`) in a gunicorn worker capped at `--timeout 300` (`startup.sh:38`; called out at `config/settings/base.py:327`). Every probe finishes cleanly under 300s or the worker is SIGKILL'd mid-run (→ stranded `running` row → the reaper). SLOs sit below internal deadlines, which sit below 300s.
3. **Synthetic tenant must be budget-CAPPED, not budget-EXEMPT.** Confirmed the A2 fear in code: `check_budget` returns `""` immediately for `is_budget_exempt=True`, bypassing BOTH personal and global checks (`apps/billing/services.py:291-292`), while `record_usage` increments the shared global `MonthlyBudget.spent_dollars` **unconditionally** (`services.py:263-269`, default cap $100). So an exempt tenant's runaway loop never self-blocks and piles into the global pool → hibernates real paying tenants. A non-exempt tenant with a small `monthly_cost_budget` self-blocks with `"personal"` on its own turns only (`apps/tenants/models.py:954-971`).

## The four probes

**Probe 1 — chat round-trip (`journey_chat`, every 30m).**
- *Inject (real path):* `POST /api/v1/chat/messages/` → `ChatMessageView.post` (`chat_views.py:675`) → `enqueue_tenant_turn` (`chat_views.py:340`) → AppChatMessage(PENDING, source=TENANT) + PendingMessage(IOS) whose QStash drain POSTs the real container `https://{fqdn}/v1/chat/completions` (`pending_queue.py:1586-1613`). Fresh `client_msg_id` = idempotency + poll key.
- *Observe (metadata only):* poll `GET /api/v1/chat/messages/<client_msg_id>/` (`ChatMessageDetailView`), JSON carries status/source/error/created_at/replied_at/waking_at/phase (`chat_views.py:296-312`). **Assert `status=="ready" AND error=="" AND source=="tenant" AND (replied_at−created_at)≤SLO`.** Record kind=JOURNEY, score=round_trip_ms.
- *Traps avoided:* `replied_at`-only (fact #1); the `/turns/` endpoint (`ChatLocalTurnView`, `chat_views.py:823`) lets the client fabricate its own reply (source=on_device) → assert source==tenant; `error=="budget_exhausted"` (self-exhaust OR global cap) is a distinct soft outcome, not a pipeline-broken red.
- *Cred:* long-lived PAT for the synthetic tenant's user (`apps/tenants/authentication.py:104`). SLO ~30-45s, poll deadline ~90s.

**Probe 2 — journal write→search (`journey_journal`, daily). Cheapest — no container; land/fire-verify first.**
- *Real path:* write a `Document` (`apps/journal/models.py:145`) via `RuntimeDocumentView.put` (`apps/integrations/runtime_views.py:2048`, internal-auth) with a unique non-word marker token in `markdown`; **commit**; then `GET .../runtime/<tenant_id>/journal/search/?q=<marker>` → `RuntimeJournalSearchView` (`runtime_views.py:2116`), Postgres `SearchVector`/`SearchRank` websearch FTS (`runtime_views.py:2156-2163`) — confirms the invariant (Postgres-side, never SQLite on the share).
- *Observe:* assert `count>=1` and a result's `(kind,slug)` matches the probe's own values. Never read snippet/title.
- *Traps avoided:* PK/slug lookup ≠ FTS → must go through the search endpoint with a content-token `q`; `grounding_probe.py:43` is a re-impl, don't hit it; MVCC — commit the write before searching (no shared `atomic()`), else false `count=0`.
- *Cleanup:* fresh marker/run, delete the doc after. *Future flag:* journal-markdown encryption (per `docs/encryption-at-rest-directive.md:124`) will need a blind index — revisit this probe then.

**Probe 3 — cron-fire delivery (`journey_cron`, daily).**
- *Real path:* tenant awake, create a **one-shot** (`schedule.kind:"at"`) typed cron, unique per-run name, via `create_typed_cron` (`apps/cron/services.py`) using a pattern that deterministically delivers via `nbhd_send_to_user` (e.g. `pure_reminder`), ~60-90s out. OC's **internal** scheduler fires it → agent turn → `nbhd_send_to_user` → `CronDeliveryView` (`apps/router/cron_delivery.py:235-244`) → `record_proactive_outbound(job_name=<X-NBHD-Job-Name>)` writes a **`ProactiveOutbound`** row (`apps/router/models.py:246`, `proactive_context.py:93`).
- *Observe:* `ProactiveOutbound.objects.filter(tenant, job_name=<name>, created_at__gte=fire_time).exists()`.
- *Traps avoided:* `CronJob.enabled/last_synced_at/last_pushed_to_container_at` (`apps/cron/models.py:110,153,158`) only prove *registration into OC's SQLite mirror*, NOT firing (there's deliberately no `last_fired`) → assert only on ProactiveOutbound; never call CronDeliveryView directly with a synthetic header (fabrication); window-scope `created_at` (fresh one-shot per run) so a stale historical row can't read green forever.
- *300s risk:* schedule ~60-90s out, cap poll ~240s. Fallback if it brushes the ceiling: two-phase (arm day N, observe day N+1).

**Probe 4 — hibernation-wake (`journey_wake`, daily). The historically fragile one.**
- *Force precondition:* hibernated ⟺ `Tenant.hibernated_at` non-null, status stays ACTIVE (`apps/orchestrator/hibernation.py:12-13`). **No single-tenant force-hibernate entry point exists** (`force_hibernate_stale.py` is a bulk sweep) → this PR adds a thin wrapper around the real `hibernate_idle_tenant(tenant)` (`hibernation.py:66`). Then **confirm ground truth via Azure (0 active revisions), not just the flag** — code documents `hibernated_at` drifting from Azure both ways (`pending_queue.py:972-1000`).
- *Drive+observe:* send a chat message (Probe 1 path). Drain 404s → wakes → `_mark_ios_waking` stamps `waking_at` (`pending_queue.py:1069-1090`, wake branch `865-908`); container emits `phase` transitions (`chat_views.py:1015-1133`). Assert the FULL chain: `waking_at` flips non-null while still PENDING → (optional `phase` non-empty = container-emitted liveness) → terminal `status=="ready"` with `(replied_at−created_at)≤SLO`. Cross-check `hibernated_at` cleared + `last_wake_at≈t0` (`hibernation.py:530`).
- *Three hard gates:* (1) ground-truth-hibernated before sending — if it can't hibernate after bounded retry (something keeps waking it), that's a real FAIL, not a skip; (2) **positively assert `waking_at` was set** — a fast reply with `waking_at` null means the warm path ran, not the wake path (the #1 assertion); (3) assert terminal `status=="ready"` specifically (a stuck turn flips to ERROR, `pending_queue.py:1241-1243`) — not merely "left PENDING."
- *300s + scheduling:* cold start "regularly past 2 min, hit 3 in worst case" (`hibernation.py:29-41`) → SLO ~180s, deadline ~240s (60s headroom). **Interacts with Probe 1:** the 30-min chat probe keeps the tenant warm and could wake it mid-test → schedule `journey_wake` well off the `:00/:30` boundary (e.g. `12 5 * * *`); ground-truth precondition catches residual races. Two-phase fallback if cold starts brush the ceiling.

## Synthetic-tenant provisioning & budget (A2 constraint, settled)

> **[team-lead amendment]** Provision **`eval-journey` only** in Wave B. Create the `eval-behavior` row + provision its container in **Wave D**, not now — but keep its `EVAL_BEHAVIOR_TENANT_ID` setting stub (empty) so no settings change is needed later. The field values below apply to `eval-journey`; apply them to `eval-behavior` when Wave D provisions it.

Create both `eval-journey` and `eval-behavior` as normal `Tenant` rows **before** provisioning (no create-command exists; `provision_tenant` needs an existing PENDING row and never touches these fields — `apps/orchestrator/management/commands/provision_tenant.py`, `Makefile:84`). Set:
- `is_synthetic=True` — business-aggregate exclusion (`apps/tenants/models.py:280`), orthogonal to budget.
- **`is_budget_exempt=False`** — the load-bearing safety field (fact #3).
- `monthly_cost_budget=Decimal("10.00")` (explicit; `0` defaults to $5 starter tier) — self-caps its own spend with `"personal"` while staying far under the $100 global cap even in runaway; $10 gives headroom for a month of cheap 30-min chat probes.
- cheapest `model_tier`; `purchased_credit=0` (credit bypasses the cap like exempt does).

When the per-tenant cap trips, only *this* tenant's turns get `budget_exhausted` — acceptable canary behavior, no other tenant touched. Optionally call `sync_or_key_limit(tenant)` once post-provision to pin the OpenRouter sub-key ceiling low too (not required — Django's pre-turn `check_budget` already trips at the smaller figure).

*Targeting decoupled from provisioning:* no `EVAL_*_TENANT_ID` convention exists yet (only `PLATFORM_OWNER_EMAIL`, `config/settings/base.py:591`). PR-B0 adds `EVAL_JOURNEY_TENANT_ID`/`EVAL_BEHAVIOR_TENANT_ID`/`EVAL_JOURNEY_PAT` via `env()` so probes resolve by env id, never a hardcoded UUID.

═══════════════════════════════════════════════════════════════════
PART 2 — PR-by-PR, ops steps, Mailgun, reaper, risks
═══════════════════════════════════════════════════════════════════

# Wave B build plan (2/2: PR-by-PR, ops steps, Mailgun, reaper, risks)

Every probe PR lands **inert** — a `TASK_MAP` entry is only operator-fireable; a schedule needs a separate `SYSTEM_CRONS` entry (`apps/cron/views.py:292-296` says so explicitly). So each probe is landed, **hand-fired in prod** (`POST /api/cron/trigger/<name>/`, watch it write real EvalRun/EvalResult rows + emit `eval <suite>: PASS n/m`), *before* the final PR turns on any schedule. No migrations in any PR.

## PR-by-PR (dependency order: A✓ → B0 → {B1,B2,B3,B4,B5 parallel} → B6)

**PR-B0 — foundation (blocks B1–B4).**
- `config/settings/base.py`: `EVAL_JOURNEY_TENANT_ID`, `EVAL_BEHAVIOR_TENANT_ID`, `EVAL_JOURNEY_PAT` via `env()` (mirror names in `production.py` per env-var-name-match hook).
- `apps/evals/journey/targets.py::resolve_journey_tenant()` → Tenant or raises a clear config error (a misconfigured probe is broken, not a silent pass — INVARIANT #3).
- `apps/evals/alerting.py::send_eval_failure_alert(run)` — content-free body from `run.suite/id/git_sha/image_tag/trigger`, passed/total, failed `case_id`s, timestamps (all already content-safe; `_assert_details_safe` scrubbed `details`). Modeled exactly on `apps/router/line_quota_handlers.py:41` `handle_pre_warn`: gate on `PLATFORM_OWNER_EMAIL`, render `email/evals/failure_{subject,body}.txt`, `send_mail(fail_silently=False)`, catch+log, return bool. **Never raises** into the caller.
- Shared task finalizer: non-pass run → `send_eval_failure_alert` (best-effort) → `raise RuntimeError` (DLQ), matching the `eval_smoke_task` contract.
- *Accept:* `manage.py test apps.evals` green; resolver raises when unset; alert body asserted content-free + skips-with-log when owner email unset; `ruff format --check` + `makemigrations --check --dry-run` clean.
- *Rollback:* revert; nothing scheduled, no data, no schema.

**PR-B1 — chat round-trip probe** (dep B0). `apps/evals/suites/journey.py::run_chat_roundtrip_suite`, `eval_journey_chat_task`, `TASK_MAP["eval_journey_chat"]`. Tests (mocked HTTP): a `replied_at`-set-but-`status=error` row must NOT pass; `source==on_device` must NOT pass; `budget_exhausted` classified soft; `details` metadata-only. *Accept:* unit green + operator fires in prod against eval-journey → genuine PASS. *Rollback:* delete TASK_MAP entry / revert.

**PR-B2 — journal write→search** (dep B0; independent of B1). Cheapest/no-container — fire-verify first. Tests mirror `apps/integrations/tests_journal_search.py`; assert it goes through the search endpoint (not PK), `q` is a content token, write commits before search, doc cleaned up. *Accept/Rollback* as B1.

**PR-B3 — cron-fire probe** (dep B0). Includes choosing the deterministic-delivery typed-cron pattern. Tests assert on ProactiveOutbound (never CronJob registration fields), `created_at` window scoping, no direct-CronDeliveryView fabrication. *Accept:* a real one-shot cron fires in prod → ProactiveOutbound row. *Flag:* two-phase fallback if single-request brushes 300s.

**PR-B4 — hibernation-wake probe** (dep B0). Adds the thin single-tenant force-hibernate wrapper around `hibernate_idle_tenant` + Azure ground-truth confirmation. Tests assert the three hard gates. *Accept:* in prod force-hibernate eval-journey, confirm 0 active revisions, drive a message, observe waking_at→ready within SLO. Land **last** among probes (riskiest).

**PR-B5 — reaper** (dep chassis only; land anytime). `reap_stuck_eval_runs_task`: flip `EvalRun` rows `status='running'` with `started_at < now−30min` to ERROR (stamp finished_at), log count. 30min sits safely above every probe's <300s deadline, so it only catches truly-dead runs (worker SIGKILL'd at the gunicorn timeout where `record_run`'s except/finally never ran). `TASK_MAP["reap_stuck_eval_runs"]`; optionally emails via B0 helper. *Accept:* backdated running row flips to error, fresh one untouched.

**PR-B6 — schedule wiring (turn it on; only after B1–B5 each fire-verified in prod).** Add `SYSTEM_CRONS` 3-tuples `(name, cron_expr, path)` in `register_system_crons.py:19`: `eval-journey-chat` `*/30 * * * *`; journal/cron/wake daily at **staggered** minutes off the `:00/:30` boundary (e.g. `05 5`, `20 5`, `12 5`); `reap-stuck-eval-runs` daily; optionally flip `eval_smoke` to scheduled. Dedup-id hygiene is NOT a concern here — schedule registration is idempotent by destination URL via `sync_system_crons` (`apps/cron/system_cron_registry.py:38`); the no-`:`/whitespace rule (`apps/cron/publish.py:39`) only bites ad-hoc publishes, which none of these do. *Accept:* deploy-time register view + daily `reconcile_system_crons_task` create the schedules; each fires on cadence; a forced failure → DLQ + owner email. *Rollback:* remove tuples + add retired paths to `RETIRED_CRON_PATHS` (`register_system_crons.py:182`) + re-run register → tasks revert to inert.

## OPS STEPS the orchestrator runs (NOT code PRs)

> **[team-lead amendment]** Step 1 runs for **`eval-journey` only** in Wave B (one container, not two). `eval-behavior` provisioning moves to Wave D.

1. **Provision the two synthetic tenants** — create rows with the field values above, then `make provision TENANT_ID=<uuid>` each (real container/DEK/share/identity, `apps/orchestrator/services.py:215`). Billable containers — deliberately outside the code PRs.
2. **Mint the chat PAT** for eval-journey's user → store as `EVAL_JOURNEY_PAT` secret.
3. **Set container-app env vars** to match new settings: `EVAL_JOURNEY_TENANT_ID`, `EVAL_BEHAVIOR_TENANT_ID`, `EVAL_JOURNEY_PAT`; confirm `PLATFORM_OWNER_EMAIL` set.
4. **Verify Mailgun BEFORE relying on alerting** (directive §3; key was invalid during the comeback campaign). IMPORTANT: this platform sends over **SMTP** (`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` → `smtp.mailgun.org:587`, `config/settings/production.py:96-115`), **not** a `MAILGUN_API_KEY` REST key — so the Mailgun MCP tools can check domain/DNS health but CANNOT validate the SMTP credential. Verify end-to-end by firing the existing `preview_email` task in prod (`POST /api/cron/trigger/preview_email/` with `{"kwargs":{"kind":1,"to":"mj1@duck.com"}}`, `apps/tenants/tasks.py:47`) and confirming the mail physically arrives with no SMTP-auth traceback. Don't wire B6's alert dependency until this passes.
5. **Fire-verify each probe in prod** after its PR deploys; force one FAIL to confirm the DLQ + owner-email path — before B6 schedules it.
6. **Register schedules** — normal deploy runs the register view + daily reconcile self-heal once B6 merges.

## Green-theater & risk register (consolidated)
- `replied_at` proves nothing → assert `status==ready AND error==""` (chat + wake).
- Client-fabricated reply via `/turns/` → inject only via `/messages/`, assert `source==tenant`.
- Warm-path wake pass → positively assert `waking_at` set; ground-truth-confirm hibernation (Azure revisions=0) before sending.
- Partial wake → assert terminal `status==ready`, not "left PENDING."
- Cron registration ≠ fired → assert ProactiveOutbound, never CronJob fields; never fabricate via CronDeliveryView; window-scope `created_at`.
- Journal PK lookup ≠ FTS → go through RuntimeJournalSearchView with a content-token `q`; commit before search (MVCC).
- **300s worker ceiling** → per-probe deadlines <300s, SLOs below that; two-phase fallback for wake/cron if cold starts brush it.
- **Probe-1↔Probe-4 race** (chat probe waking the tenant mid-wake-test) → staggered schedules + ground-truth precondition; `could_not_hibernate` is a real FAIL not a skip (avoids a false green under INVARIANT #3, which has no "skip" state — zero recorded cases already closes ERROR).
- **No-external-call-in-transaction** → `record_run` opens no transaction around the suite body; keep every httpx call outside any `atomic()` (HTTP first, `record()` after).
- **Metric pollution** (two residues, handle in Wave E not here): (a) synthetic spend still lands in the global `MonthlyBudget.spent_dollars` — a *feature* (caps runaways) but Suite-4 infra-cost/global readouts include a few synthetic dollars; (b) chat/wake probes poll `ChatMessageDetailView`, which server-side `reveal()`s the synthetic tenant's own text → emits **decrypt-audit** events under the probe principal → Suite-4's "unexplained owner-read spike" metric must exclude the synthetic tenants. Neither is an INVARIANT #1 violation (the eval sink never receives content; probe reads only status/replied_at/error).
- **Unbounded growth** → journal probe deletes its doc each run; chat/cron/proactive rows accumulate only on synthetic tenants (harmless, excluded from metrics).
- **Future journal encryption** → `journey_journal`'s plaintext-FTS assumption revisited when `Document.markdown` encryption lands.
