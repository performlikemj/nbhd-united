# Invariants — permanent platform rules

Each of these exists because violating it caused a real production incident. They are not style preferences. If a change requires breaking one, stop and raise it with MJ instead.

## 1. Never put SQLite on the per-tenant Azure File Share

SMB lock/fsync semantics don't preserve SQLite durability — a container kill mid-write leaves a 0-byte file. OpenClaw's built-in `memory_search` did exactly this and corrupted 23/26 tenant shares before we caught it (PR #525). It stays disabled (`memorySearch.enabled: False` in `config_generator.py` + `tools.deny` in `tool_policy.py`; regression test `apps/orchestrator/tests.py::test_memorysearch_disabled_and_denied` pins both). Anything that wants SQLite on the share goes through Postgres instead (search routes via `nbhd_journal_search` → `/journal/search/`), or container-local ephemeral storage rebuilt on cold start.

## 2. All File Share text writes go through the sanitize chokepoint

`apps/orchestrator/azure_client.py::_put_share_file` — `text=` writes auto-run `sanitize_share_text` (strips `\x00` + C0 control bytes). Never hand-roll a share upload. A null-byte tail once inflated a USER.md to 23KB of `\x00` that was silently injected into every prompt.

## 3. Every inbound handler claims the event before side effects

LINE redelivers webhooks at-least-once; the Telegram poller replays unacked updates after redeploy. Any new inbound channel/handler MUST call `apps/router/inbound_dedup.py::claim_inbound_event(event_key)` with a provider-stable id at entry (fail-open on blank id/DB error). Do NOT put dedup in `enqueue_message_for_tenant` — internal enqueues (cron, buffered redelivery) have no provider id and must not be gated.

## 4. Cover ALL channels, always

Any feature touching message routing must hit every path, not just the one you tested:
- Inbound: `poller.py::_forward_to_container`, `views.py::telegram_webhook` → `forward_to_openclaw`, `line_webhook.py::_forward_to_container`
- Outbound: `poller.py::_send_rich_response`, `cron_delivery.py::_send_via_telegram/_send_via_line`, LINE conversation replies in `line_webhook.py`

## 5. Azure revision ops must be idempotent

`activate_revision`/`deactivate_revision` raise `ResourceExistsError` (`RevisionAlreadyInRequestedState`) when a stale `list_revisions()` read races a no-op. Treat already-in-requested-state as **success** (match the specific code/message — don't swallow all 409s). A non-idempotent wake once wedged message delivery permanently. Telegram containers must stay **single-revision** (prevents 409 poller conflicts); note the same-tag re-bump wedge in `debugging.md`.

## 6. QStash, not Celery — and dedup-id hygiene

All scheduling goes through QStash (`apps/cron/publish.py`). Never add `django_celery_beat`. `Upstash-Deduplication-Id` rejects `:` and whitespace with a silent-ish 400 — use `-`/`_`/alphanumerics; `publish_task` validates eagerly and will fail your tests if you regress.

## 7. Timezone lookups go through the front door

`apps/common/tenant_tz.py` (`tenant_tz_name`, `tenant_tz`, `safe_zoneinfo`) is the canonical tenant-timezone lookup with UTC fallback. Never write another private `_tenant_zone` helper. Keep the module import-free of `apps.tenants`.

## 8. No external calls inside `transaction.atomic()`

`app_user` has `idle_in_transaction_session_timeout = 60s`, and a transaction pins a transaction-pooler backend. Use the **lease pattern**: claim/mark the row inside the txn, COMMIT, then do the network/LLM/Azure work (see `router/pending_queue`, `orchestrator/hibernation`). For long QStash tasks with a no-DB gap before a final write: on `OperationalError`/`InterfaceError`, `connection.close()`, **re-set the RLS GUC** (`set_rls_context(service_role=True)`), retry once (see `apps/core/services._save_session`).

## 9. OpenClaw cron lifecycle facts

- Startup catch-up is by design: on Gateway boot, enabled jobs with missed fires run (max 5 immediate). Disabled jobs are excluded — suspend before hibernation.
- `cron.update {enabled: true}` does NOT trigger catch-up; runtime state (`lastRunAtMs`) is NOT patchable. To reset it: `cron.remove` + `cron.add`.
- Design wake/resume code as if catch-up will fire anyway.

## 10. Config/env coupling

- Env var names in `config/settings/production.py` must match the Azure Container App env vars on `nbhd-django-westus2` — renaming in code alone breaks prod at next deploy (a hook reminds you on edit).
- Key Vault `identityref:` uses the `mi-nbhd-` identity name, NOT the `oc-` container name.
- Image before config: never push an OpenClaw config that requires a newer image than what's deployed (live-reload → last-good rollback wedge).

## 11. Secrets discipline

Never print secrets or dump env vars into the conversation. Rotation flows through the `/rotate-keys` skill (reads via `read -s`, writes to `kv-nbhd-prod`). OpenRouter platform key lives at KV secret `openrouter-api-key`.

## 12. Tenant share artifacts are renders, not write surfaces

`workspace/AGENTS.md`, `openclaw.json`, and workspace docs on tenant shares are RENDERED from source (`personas.py`, `config_generator.py`, tenant DB fields, `users.preferences.prompt_extras`) and fully overwritten by every apply sweep, container boot reassert (`RuntimeContainerStartedView`), and hibernation wake. Hand-edits to the share silently vanish at the next render. The write path is always: change the source → `bump_pending_config()` → apply. The sweep is idle-only and skips hibernated tenants — they converge at wake; don't force-wake to hurry it.

## 13. Never gate a capability on `openclaw_version`

That field tracks the RUNNING BINARY and is actively reconciled: the hibernation wake path (`apps/orchestrator/hibernation.py` ~502) rewrites it from the image tag, so any future-version you stamp is fiction the platform corrects — and your gate silently closes on the next re-render. This erased the tour-guide plugin config + AGENTS.md gate on both canaries overnight (2026-07-20, fixed in #1255). Capability gates use a dedicated field nothing reconciles (precedent: `Tenant.tour_guide_manifest_ok`, migration 0135), read through ONE shared helper (`tour_guide_delivery_ready`) at every call site so delivery surfaces can't diverge.

## 14. New plugin-config keys need a manifest-ready gate

Plugin manifests (`openclaw.plugin.json`) declare `configSchema` with `additionalProperties: false`, validated at container LOAD — a config carrying keys the tenant's image doesn't declare hard-rejects the WHOLE config at boot (2026-07-06 image-boot-smoke failure; #917-class wedge). Emit new keys only for tenants whose image verifiably carries the new manifest, gated per invariant 13 (per-tenant flag flipped after image verification; fleet backfill at the fleet image pass). The maximal-config boot smoke renders at the fleet default, so it does NOT exercise gated keys — the first real coverage is the canary.

## 15. `set_prompt_extras` replaces the whole section

`users.preferences.prompt_extras.<section>` carries live per-tenant content (both canaries: MJ's canary extras; Kiho's assistant identity block). The management command REPLACES the section wholesale. To append: jsonb string-concat in SQL (`jsonb_set(... coalesce(existing,'') || $$new$$ ...)`), keep the exact appended text for revert, and verify the render on the share afterwards.

## 16. AGENTS.md budget is chars-on-the-share; chat agents cannot read docs

The bootstrap cap is 24,000 CHARS (not bytes; `BOOTSTRAP_MAX_CHARS`), the sentinel warns at 23k, and truncation is silent from the tail — always measure the rendered share file, never the template. Chat-context tool policy strips fs `read` (`tools.allow` = group:openclaw/group:plugins/pdf), so "read `docs/X.md` THIS TURN" gates can never fire in chat — that pattern is cron-only. Behavioral contracts for chat ride a TOOL RESPONSE (the Gravity `nbhd_insights_signals` pattern; tour-guide's `nbhd_tour_guide`): zero bootstrap budget, deterministic, verbatim.
