"""
QStash webhook handlers for executing scheduled and on-demand tasks.

QStash sends HTTP POST requests on a schedule (or on-demand via publish),
which this endpoint executes synchronously. This eliminates the need for
Celery workers polling Redis continuously.
"""

import inspect
import json
import logging
import traceback
import uuid
from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.db import models
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.cron.qstash_verify import verify_qstash_signature
from apps.orchestrator.azure_client import restart_container_app
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def execute_task_sync(task_path: str, *args, **kwargs):
    """
    Execute a task function synchronously by importing and calling it directly.

    Args:
        task_path: Dotted path to the task function.
        *args, **kwargs: Arguments to pass to the task function.

    Returns:
        The result of the task function.
    """
    module_path, func_name = task_path.rsplit(".", 1)
    module = import_module(module_path)
    func = getattr(module, func_name)
    return func(*args, **kwargs)


def _validate_task_signature(task_path: str, args: list, kwargs: dict) -> str | None:
    """Return None if ``(args, kwargs)`` binds to ``task_path``'s function signature.

    Returns a human-readable error string otherwise. Lets ``ImportError`` /
    ``AttributeError`` propagate — those mean the TASK_MAP entry itself is
    wrong (programmer bug), which the caller should surface as a 500.

    Used by ``trigger_task`` to convert "QStash gave us a bad message" from
    an opaque 500 (which QStash retries 3x) into a clear 400 (which it
    parks in DLQ immediately). See issue #557.
    """
    module_path, func_name = task_path.rsplit(".", 1)
    func = getattr(import_module(module_path), func_name)
    try:
        inspect.signature(func).bind(*args, **kwargs)
    except TypeError as exc:
        return str(exc)
    return None


# Map of URL-safe task names to task module paths.
# Tasks are executed synchronously — no Celery queue involved.
TASK_MAP = {
    # Tenant maintenance (scheduled via QStash cron)
    "reset_daily_counters": "apps.tenants.tasks.reset_daily_counters_task",
    "reset_monthly_counters": "apps.tenants.tasks.reset_monthly_counters_task",
    "cleanup_expired_telegram_tokens": "apps.tenants.tasks.cleanup_expired_telegram_tokens",
    "revoke_apple_token": "apps.cron.tasks.revoke_apple_token_task",
    # Privacy-rotation campaign (June 2026) — one-off scheduled fires.
    # Set via the upstash QStash MCP a day or two before the campaign
    # dates. See apps/tenants/management/commands/rotate_all_passwords.py
    # and send_promo_campaign.py for the underlying commands.
    "rotate_all_passwords": "apps.tenants.tasks.rotate_all_passwords_task",
    "send_promo_campaign": "apps.tenants.tasks.send_promo_campaign_task",
    # iOS relaunch win-back (June 2026) — re-send of the 14-day trial offer
    # with the App Store launch + a fresh redemption window (the prior
    # privacy-zdr-2026 blast closed with 0 redemptions). Zero-arg; fire via a
    # QStash publish to /api/cron/trigger/send_ios_relaunch_campaign/.
    "send_ios_relaunch_campaign": "apps.tenants.tasks.send_ios_relaunch_campaign_task",
    # Comeback win-back (July 2026) — free trial extension + App Store, sent to
    # the wider onboarded-and-once-active cohort (audience="comeback", includes
    # paid-then-lapsed). Redemption restores suspended-tenant runtime and the
    # send carries List-Unsubscribe headers. Zero-arg; fire via a QStash publish
    # to /api/cron/trigger/send_comeback_campaign/.
    "send_comeback_campaign": "apps.tenants.tasks.send_comeback_campaign_task",
    # Operator-fired preview of the campaign emails — accepts
    # {"kwargs": {"kind": 1|2, "to": "<email>", "display_name": "..."}}
    # in the QStash body. Used pre-launch to sanity-check rendered HTML
    # in the platform owner's inbox.
    "preview_email": "apps.tenants.tasks.preview_email_task",
    "refresh_expiring_integrations": "apps.integrations.tasks.refresh_expiring_integrations_task",
    # sautai Phase 0 — async meal-plan generation (proxy creates the PENDING
    # job, this task does the slow 30-60s sautai M2M call). See
    # docs/sautai-phase0-contract.md.
    "generate_sautai_meal_plan": "apps.integrations.tasks.generate_sautai_meal_plan_task",
    # Sweep PENDING action-gate rows past their 5-min expiry: flip to EXPIRED,
    # audit-log, and refresh the platform message so stale Approve/Deny buttons
    # are cleared. GatePollView expires lazily on poll, but abandoned actions
    # (container never polls again) need this backstop. See apps/actions/tasks.py.
    "expire_stale_actions": "apps.actions.tasks.expire_stale_pending_actions",
    # PR #1.6: hourly OR-spend truing-up (per-tenant + platform).
    "reconcile_openrouter_spend": "apps.billing.tasks.reconcile_openrouter_spend_task",
    # Journal memory sync (on-demand via signal or QStash publish)
    "sync_documents_to_workspace": "apps.journal.tasks.sync_documents_to_workspace",
    # Provisioning (on-demand via QStash publish)
    "provision_tenant": "apps.orchestrator.tasks.provision_tenant_task",
    "deprovision_tenant": "apps.orchestrator.tasks.deprovision_tenant_task",
    "update_tenant_config": "apps.orchestrator.tasks.update_tenant_config_task",
    "seed_cron_jobs": "apps.orchestrator.tasks.seed_cron_jobs_task",
    "repair_stale_tenant_provisioning": "apps.orchestrator.tasks.repair_stale_tenant_provisioning_task",
    # Encryption-at-rest Phase 1 activation (2026-07) — one-off operator fire that
    # runs the DEK backfill INSIDE the running container (which holds prod DB
    # access + the provisioner managed identity), so no secret leaves Azure and
    # no new auth surface is added. Fired via a no-body QStash publish to
    # /api/cron/trigger/<name>/ — hence the zero-arg pair (the publish path we
    # use can't carry a body). Safe to leave registered: the backfill is
    # idempotent, so once every tenant has a DEK a re-fire is a no-op.
    "backfill_tenant_deks": "apps.orchestrator.tasks.backfill_tenant_deks_task",
    "backfill_tenant_deks_dry_run": "apps.orchestrator.tasks.backfill_tenant_deks_dry_run_task",
    # Encryption-at-rest Phase 1->2 bridge smoke. Operator-fired via a no-body
    # QStash publish to /api/cron/trigger/crypto_roundtrip_smoke/. Proves the
    # full box path — envelope codec + AAD + per-process DEK cache + broker
    # unwrap — end-to-end on a real DEK (the broker gate proved unwrap only).
    # Pure in-memory round-trip on ONE keyed tenant with a throwaway sentinel:
    # no DB writes, no user data. Raises on failure so QStash DLQs it; safe to
    # re-fire anytime.
    "crypto_roundtrip_smoke": "apps.orchestrator.tasks.crypto_roundtrip_smoke_task",
    # Encryption-at-rest Phase 2 PR-3 — one-off operator fire that seals legacy
    # chat plaintext (user_text / title) into the *_enc sidecars for every
    # write-flag-ON tenant's not-yet-encrypted rows. Fired via a no-body QStash
    # publish to /api/cron/trigger/<name>/ — hence the zero-arg pair (the publish
    # path we use can't carry a body). Idempotent (only _enc IS NULL rows), so a
    # re-fire after a completed backfill is a no-op; safe to leave registered.
    "encrypt_chat_history": "apps.orchestrator.tasks.encrypt_chat_history_task",
    "encrypt_chat_history_dry_run": "apps.orchestrator.tasks.encrypt_chat_history_dry_run_task",
    # Encryption-at-rest Phase 2 — MJ-gated convergence of the post-2026-07-11
    # cohort: tenants provisioned before chat-encryption flags were set at
    # provision time (and never covered by the one-time fleet UPDATE) still
    # write+read plaintext chat. Per tenant this flips encrypt_chat_writes ON,
    # runs the PR-3 backfill, verifies zero plaintext-only rows remain, then
    # flips read_encrypted_chat ON — the fleet ladder compressed and idempotent.
    # Fired via a no-body QStash publish to /api/cron/trigger/<name>/ — hence the
    # zero-arg pair. No-op once the fleet is converged; ships DARK (nothing fires
    # it until an operator does). Blocks the PR-6 plaintext erase until it has run.
    "converge_unencrypted_chat_tenants": "apps.orchestrator.tasks.converge_unencrypted_chat_tenants_task",
    "converge_unencrypted_chat_tenants_dry_run": (
        "apps.orchestrator.tasks.converge_unencrypted_chat_tenants_dry_run_task"
    ),
    # Eval system (see docs/evals-directive.md) — chassis proof. Operator-fired via
    # a no-body QStash publish to /api/cron/trigger/eval_smoke/ (zero-arg, because
    # the publish path we use can't carry a body). Writes real EvalRun/EvalResult
    # rows and emits the one-line run summary; RAISES when the run doesn't close
    # 'pass' so a failing eval lands in the DLQ instead of a silent green. Safe to
    # re-fire anytime — each fire is its own run.
    "eval_smoke": "apps.evals.tasks.eval_smoke_task",
    # Eval Wave B Probe 1 — chat round-trip journey canary. Drives one real turn
    # (message → drain → the synthetic journey tenant's container → reply) and
    # asserts the round trip actually completed (status==ready AND error=="" AND
    # source==tenant within SLO) — NOT merely that replied_at got stamped, which
    # happens on failures too. Operator-fired via a no-body QStash publish to
    # /api/cron/trigger/eval_journey_chat/ (zero-arg); RAISES on a non-pass run so
    # a broken pipeline DLQs + emails the owner. budget_exhausted is a soft pass.
    # Inert until PR-B6 schedules it. See apps/evals/suites/journey_chat.py.
    "eval_journey_chat": "apps.evals.tasks.eval_journey_chat_task",
    # Journey canary — journal write→search (Wave B, Probe 2). Drives the real
    # RuntimeDocumentView write + Postgres-FTS RuntimeJournalSearchView read
    # against the synthetic EVAL_JOURNEY_TENANT_ID tenant. Operator-fired via a
    # no-body publish to /api/cron/trigger/eval_journey_journal/ (zero-arg);
    # PR-B6 adds the daily schedule. RAISES on a non-'pass' run so a broken
    # journal path lands in the DLQ + owner alert, never a silent green.
    "eval_journey_journal": "apps.evals.tasks.eval_journey_journal_task",
    # Eval system — cron-fire delivery canary (Wave B / Probe 3). Operator-fired via
    # a no-body QStash publish to /api/cron/trigger/eval_journey_cron/ (zero-arg).
    # Arms a REAL one-shot pure_reminder cron on the synthetic journey tenant and
    # asserts OpenClaw actually fired it by observing a fresh ProactiveOutbound row
    # (never CronJob registration fields — those don't prove a fire). RAISES on a
    # non-delivery so it DLQ's + alerts the owner instead of a silent green.
    "eval_journey_cron": "apps.evals.tasks.eval_journey_cron_task",
    # Eval Wave B Probe 4 — hibernation-wake journey canary (the historically
    # fragile path). Force-hibernates the synthetic journey tenant (confirmed via
    # Azure ground truth — 0 active revisions, not the drifting DB flag), then
    # drives one real message and asserts the FULL wake chain: waking_at was set
    # (NOT the warm path) AND the turn reached 'ready' within SLO — not merely that
    # a timestamp got stamped. Operator-fired via a no-body QStash publish to
    # /api/cron/trigger/eval_journey_wake/ (zero-arg); RAISES on a non-pass run so a
    # broken wake DLQs + emails the owner. budget_exhausted is a soft pass; a
    # could-not-hibernate precondition is a hard FAIL. Inert until PR-B6 schedules
    # it (staggered off the chat probe). See apps/evals/suites/journey_wake.py.
    "eval_journey_wake": "apps.evals.tasks.eval_journey_wake_task",
    # Eval reaper (Wave B5) — crash-recovery sweep for orphaned runs. A worker
    # SIGKILL'd at the 300s gunicorn ceiling leaves record_run's except/finally
    # un-run, stranding an EvalRun at status='running' forever; this daily zero-arg
    # sweep flips any run 'running' longer than 30min to 'error'. The 30min floor
    # sits far above every probe's sub-300s deadline, so it only ever reaps a
    # truly-dead run, never a live one. Safe to re-fire anytime (idempotent — only
    # touches still-stuck rows).
    "reap_stuck_eval_runs": "apps.evals.tasks.reap_stuck_eval_runs_task",
    # Eval Wave D — model-behavior suite (docs/evals-directive.md §Suite 2). Drives
    # YAML scenario fixtures against the synthetic behavior tenant, GATES on
    # deterministic hard assertions (observable DB rows + reply-text patterns), and
    # scores soft dimensions with a pinned, spend-capped judge (Claude Sonnet 5 via
    # OpenRouter) recorded ADVISORY / non-gating. Operator-fired via a no-body QStash
    # publish to /api/cron/trigger/eval_behavior/ (zero-arg); RAISES on any non-pass
    # outcome so it DLQs + emails the owner — including the config-error path (the
    # task wrapper alerts on the 'error'-closed run before re-raising). Lands INERT:
    # no schedule, and the behavior tenant is not provisioned yet, so a fire today
    # closes 'error' → owner email + DLQ — the correct loud signal.
    # Fire-verification follows provisioning.
    "eval_behavior": "apps.evals.tasks.eval_behavior_task",
    # Eval Suite 4 — nightly production-SLO snapshot (metadata only). Computes
    # reply/wake latency percentiles, error-status rate, proactive-delivery volume
    # (ALL ProactiveOutbound producers, not cron health — see the suite's named
    # deferrals), stranded/error EvalRun count, and journey-canary budget-cap
    # saturation over the last 24h (synthetic excluded; no content read) and records
    # one EvalResult per metric. A threshold BREACH closes the run 'fail' → owner
    # alert + DLQ (that IS the breach-flag mechanism). Fired via a no-body QStash
    # delivery to /api/cron/trigger/slo_snapshot/ (zero-arg). See
    # apps/evals/suites/slo_snapshot.py.
    "slo_snapshot": "apps.evals.tasks.slo_snapshot_task",
    # Eval Suite 4 — Monday weekly SLO digest. Reads the trailing 7 days of
    # slo_snapshot runs/results and emails the platform owner a one-page plain-text
    # trend (per-metric min/max/latest vs threshold + breach days) via the gated
    # send_slo_digest. A READOUT, not an alarm: it sends even when all-green; no owner
    # is a quiet skip, while an attempted send failure raises and this endpoint returns
    # 500 for QStash visibility/retry. Zero-arg, no-body QStash publish.
    "weekly_slo_digest": "apps.evals.tasks.weekly_slo_digest_task",
    # Media cleanup (daily)
    "cleanup_inbound_media": "apps.router.tasks.cleanup_inbound_media_task",
    # LINE Push monthly quota — daily poll + on-demand handler dispatch.
    # poll_line_quota refreshes the singleton state and emits transitions;
    # dispatch_line_quota_handler runs the user-facing fan-out (idempotent
    # so it's safe to invoke from the 429 tripwire and the poll alike).
    "poll_line_quota": "apps.router.tasks.poll_line_quota_task",
    "dispatch_line_quota_handler": "apps.router.tasks.dispatch_line_quota_handler_task",
    # Lesson constellation maintenance
    "dedup_lessons": "apps.lessons.tasks.dedup_lessons_task",
    # Async LLM cluster-naming pass (enqueued by refresh_constellation after the
    # deterministic label pass; cached + capped, deterministic labels on failure)
    "name_clusters": "apps.lessons.tasks.name_clusters_task",
    "reseed_lessons": "apps.lessons.tasks.reseed_lessons_task",
    "reseed_lessons_single_tenant": "apps.lessons.tasks.reseed_lessons_single_tenant_task",
    # Neighborhood: fail-closed share scrub (async, never inline in a request)
    "scrub_shared_lesson": "apps.friends.tasks.scrub_shared_lesson_task",
    # Neighborhood: weekly Mission digest (one warm nudge per member, idempotent)
    "mission_weekly_digest": "apps.friends.tasks.mission_weekly_digest_task",
    # Neighborhood: coords-only copy-forward onto shared snapshots after a recluster
    "refresh_shared_positions": "apps.friends.tasks.refresh_shared_positions_task",
    # Hibernate suspended containers (one-off cleanup)
    "hibernate_suspended": "apps.orchestrator.tasks.hibernate_suspended_task",
    # Daily reaper for orphaned tenant containers (oc-* apps with no Tenant
    # row — e.g. a User account deletion whose teardown was lock-blocked).
    # Detects + hibernates awake orphans (lock-safe) + alerts. Does NOT delete
    # (prod CanNotDelete locks block it; run the management command --apply
    # after lifting the lock). See apps/orchestrator/orphan_reaper.py.
    "reap_orphaned_containers": "apps.orchestrator.orphan_reaper.reap_orphaned_containers_task",
    # Re-enable a reactivated tenant's crons (delayed ~30s after container wake
    # so the gateway is ready). Enqueued by handle_checkout_completed on
    # SUSPENDED→ACTIVE — see issue #540.
    "resume_tenant_crons": "apps.orchestrator.tasks.resume_tenant_crons_task",
    # Per-tenant config/image updates (enqueued by apply-pending-configs)
    "apply_single_tenant_config": "apps.orchestrator.tasks.apply_single_tenant_config_task",
    "apply_single_tenant_image": "apps.orchestrator.tasks.apply_single_tenant_image_task",
    # Post-image-update cron restore (enqueued by apply_single_tenant_image_task)
    "restore_crons_after_image_update": "apps.orchestrator.tasks.restore_crons_after_image_update_task",
    # One-off broadcast (enqueued by broadcast-message)
    "broadcast_single_tenant": "apps.orchestrator.tasks.broadcast_single_tenant_task",
    # Cron dedup (enqueued by dedup-crons)
    "dedup_cron_jobs": "apps.orchestrator.tasks.dedup_cron_jobs_task",
    "remove_zombie_heartbeats": "apps.orchestrator.tasks.remove_zombie_heartbeats_task",
    # Retire one-shot ("at") crons whose fire time has passed. The container
    # deletes its own copy when the job fires but never tells Django, so without
    # this the row stays enabled=True and squats its (tenant, name) forever —
    # a user asking for the same reminder twice used to get a 409. Hourly.
    "expire_finished_at_crons": "apps.cron.tasks.expire_finished_at_crons_task",
    # Daily infra cost refresh from Azure billing
    "refresh_infra_costs": "apps.billing.tasks.refresh_infra_costs_task",
    # Monthly donation ledger — records each paying subscriber's revenue-%
    # donation for the just-closed month as a pending row (disbursed manually).
    "snapshot_donations_monthly": "apps.billing.tasks.snapshot_donations_monthly_task",
    # Free-model offer health — pricing + reachability probe; flips the promo
    # on transitions (bumps configs + notifies users).
    "model_health_check": "apps.billing.tasks.model_health_check_task",
    # Fleet USER.md refresh — bounds staleness of the `_Current local time: ..._`
    # line in workspace/USER.md so cron-fired turns see fresh time even when
    # no signal-driven refresh has fired for hours.
    "refresh_user_md_fleet": "apps.orchestrator.tasks.refresh_user_md_fleet_task",
    # Idle hibernation — scale-to-zero for inactive tenants
    "hibernate_idle_tenants": "apps.orchestrator.tasks.hibernate_idle_tenants_task",
    "deliver_buffered_messages": "apps.orchestrator.hibernation.deliver_buffered_messages_task",
    "resume_hibernated_crons": "apps.orchestrator.hibernation.resume_hibernated_crons_task",
    # Cron-aware wake — wake hibernated containers for scheduled crons
    "wake_for_cron": "apps.orchestrator.hibernation.wake_for_cron_task",
    "check_cron_wake_idle": "apps.orchestrator.hibernation.check_cron_wake_idle_task",
    # Cleanup delivered message buffers
    "cleanup_delivered_buffers": "apps.orchestrator.hibernation.cleanup_delivered_buffers_task",
    # Nightly extraction — goals/tasks/lessons from daily notes
    "nightly_extraction": "apps.orchestrator.tasks.nightly_extraction_task",
    # Fuel welcome cron — delayed after container restart
    "schedule_fuel_welcome": "apps.fuel.tasks.schedule_fuel_welcome_task",
    # Gravity (finance) welcome cron — delayed after finance toggle
    "schedule_finance_welcome": "apps.finance.tasks.schedule_finance_welcome_task",
    # Monthly FinanceSnapshot — point-in-time debt/savings/net-worth history.
    # Fires on the 1st of each month via QStash. Idempotent per (tenant, date).
    # The /api/v1/finance/snapshots/ endpoint reads these rows; without this
    # cron the endpoint always returns an empty list.
    "snapshot_finance_monthly": "apps.finance.tasks.create_monthly_snapshots_task",
    # Core (mindfulness) welcome cron + on-demand meditation render
    "schedule_core_welcome": "apps.core.tasks.schedule_core_welcome_task",
    "render_meditation": "apps.core.tasks.render_meditation_task",
    "compose_meditation": "apps.core.tasks.compose_meditation_task",
    "reap_meditations": "apps.core.tasks.reap_meditations",
    # Fuel session-scheduling cutover — derived from Workout.scheduled_at
    "regenerate_fuel_crons": "apps.orchestrator.tasks.regenerate_fuel_crons_task",
    "reconcile_fuel_crons": "apps.orchestrator.tasks.reconcile_fuel_crons_task",
    # Daily sweep — mark active workout plans whose duration has elapsed as
    # completed so "active" means "currently running" (single-active hygiene).
    "complete_elapsed_plans": "apps.orchestrator.tasks.complete_elapsed_plans_task",
    # Postgres-canonical cron cutover — derived view of CronJob rows
    "regenerate_tenant_crons": "apps.orchestrator.tasks.regenerate_tenant_crons_task",
    "reconcile_tenant_crons": "apps.orchestrator.tasks.reconcile_tenant_crons_task",
    # At-cron wake sweep — backstop wake scheduling for one-off kind:"at"
    # crons. The hibernation path only schedules wakes for tenants Django
    # is actively hibernating; this sweep covers out-of-band container
    # restarts between cron creation and fire time.
    "ensure_at_cron_wakes": "apps.orchestrator.tasks.ensure_at_cron_wakes_task",
    # Welcome-cron watchdog — daily fleet-wide reconcile for missing or
    # stale Fuel/Gravity welcome crons. Closes the gap when both the
    # deploy backfill and the live toggle path fail to deliver.
    "reconcile_welcomes": "apps.orchestrator.tasks.reconcile_welcomes_task",
    # Per-tenant message serialization queue — drains the next pending
    # warm-tenant message for (tenant, channel, channel_user_id) so the
    # OpenClaw claude-cli backend never sees overlapping turns on the
    # same live session.
    "drain_pending_messages_for_tenant": "apps.router.pending_queue.drain_pending_messages_for_tenant_task",
    # Per-minute reaper for the message queue. Republishes drain tasks
    # for PendingMessage rows whose original drain never ran. Safety net
    # for QStash publish failures, DLQ-bound drain attempts, and worker
    # deaths mid-claim. See pending_queue.reap_stuck_inbound_messages_task.
    "reap_stuck_inbound_messages": "apps.router.pending_queue.reap_stuck_inbound_messages_task",
    # Five-minute defense-in-depth sweep for iOS/AppChatMessage turns orphaned
    # in the narrow creation-before-enqueue crash window.
    "reap_stale_app_chat_messages": "apps.router.pending_queue.reap_stale_app_chat_messages_task",
    # Daily privacy sweep for the per-tenant message queue. Deletes terminal
    # PendingMessage rows (FAILED + residual DELIVERED) older than 14 days so
    # the transient forwarding queue stops accumulating (redacted) user text.
    # DELIVERED rows are hard-deleted on drain; this backstops the crash-window
    # residue and bounds FAILED-row retention. See
    # pending_queue.cleanup_stale_pending_messages_task.
    "cleanup_stale_pending_messages": "apps.router.pending_queue.cleanup_stale_pending_messages_task",
    # Atomic fleet-bump fan-out (rollout-atomic-bump endpoint). Per-tenant
    # version + config + image bump that survives gunicorn 300s budget
    # for any fleet size. Differs from apply_single_tenant_image (image-only,
    # config refreshed lazily by apply_pending_configs cron) in that this
    # bumps config AND image atomically — required when a release crosses
    # an OpenClaw config schema boundary (e.g. 4.x → 5.x).
    "bump_openclaw_atomic_per_tenant": "apps.orchestrator.tasks.bump_openclaw_atomic_per_tenant_task",
    # Weekly Gravity snapshot — writes PillarSnapshot rows for the
    # assistant's history/drill/compare tools. Skips hibernated tenants;
    # idempotent per ISO week. See apps.insights.snapshots.compute_gravity_snapshot.
    "snapshot_gravity_weekly": "apps.insights.tasks.snapshot_gravity_weekly_task",
    # Weekly Fuel / Core / Journal snapshots — companion to the Gravity task
    # above. Each pillar gated on its enablement flag (Journal always-on) +
    # status=ACTIVE + not-hibernated; idempotent per ISO week per pillar.
    # See apps.insights.snapshots.compute_{fuel,core,journal}_snapshot.
    "snapshot_pillars_weekly": "apps.insights.tasks.snapshot_pillars_weekly_task",
    # Phase 4 weekly reflection — hourly dispatcher fires Django-side
    # synthesis for each tenant whose local time is Sunday 09:00.
    # Bills to platform (record_usage is_system=True), not user quota.
    "weekly_gravity_reflection": "apps.insights.tasks.weekly_gravity_reflection_task",
    # PII junk sweep — daily ZERO-EGRESS deterministic hygiene pass over stored
    # bindings. Heals owner-visible journal text, denies the canonical key, then
    # deletes the junk binding (strict order). Replaces the retired cloud
    # ``pii_arbiter`` (which shipped span text to a cloud LLM). Residual
    # ambiguous cases go to the on-device review flow. See apps/pii/junk_sweep.py.
    "pii_junk_sweep": "apps.pii.junk_sweep.pii_junk_sweep_task",
    # RETIRED: ``pii_arbiter`` (apps/pii/arbiter.py) shipped PERSON/LOCATION span
    # text to Claude Haiku to prune false positives. That cloud egress is retired
    # in favor of pii_junk_sweep + on-device review; the schedule is removed via
    # RETIRED_CRON_PATHS in register_system_crons.py. The task path is left
    # mapped so any in-flight QStash message drains cleanly rather than 404-ing.
    "pii_arbiter": "apps.pii.arbiter.pii_arbiter_task",
    # User-facing scheduled automations — per-minute dispatcher that runs
    # every ACTIVE Automation whose next_run_at has elapsed. Without this the
    # automations CRUD surface is dead for scheduled fires (manual-run only).
    # See apps/automations/scheduler.py:run_due_automations. NOTE: also needs a
    # SYSTEM_CRONS entry in register_system_crons.py to create the QStash schedule.
    "run_due_automations": "apps.automations.tasks.run_due_automations_task",
    # Daily belt-and-braces reconcile of the system-cron schedules. Re-runs the
    # same register/update/deregister core the post-deploy register-system-crons
    # call uses, so any registration drift — e.g. a retired schedule the
    # post-deploy swap missed because it hit a stale revision (incident
    # 2026-07-09b) — self-heals within 24h. base_url comes from
    # settings.DJANGO_BASE_URL. See apps/cron/system_cron_registry.py and the
    # SYSTEM_CRONS entry in register_system_crons.py.
    "reconcile_system_crons": "apps.cron.system_cron_registry.reconcile_system_crons_task",
    # Portfolio-scoped deterministic watchtower; direct notification has no
    # tenant runtime, agent, gateway, Celery, or OpenClaw dependency.
    "steward_sweep": "apps.steward.sweep.run_steward_sweep",
    # Daily facts-only PM digest; refreshes eval/SLO intake before delivery.
    "steward_daily_digest": "apps.steward.digest.run_steward_daily_digest",
    # Read-only portfolio collectors; both are bounded and disabled when their
    # dedicated credentials are absent.
    "steward_collect_github": "apps.steward.tasks.steward_collect_github_task",
    "steward_collect_asc": "apps.steward.tasks.steward_collect_asc_task",
}


@csrf_exempt
@require_POST
def trigger_task(request, task_name):
    """
    Execute a registered task synchronously.

    QStash calls this endpoint on a schedule or via publish.
    We verify the signature, then execute the task directly.

    URL: /api/cron/trigger/<task_name>/
    """
    if not verify_qstash_signature(request):
        logger.warning("Unauthorized cron trigger attempt for task: %s", task_name)
        return JsonResponse({"error": "Invalid signature"}, status=401)

    # Signature verified — set RLS service role so tasks can access all tenants
    from apps.crypto import audit
    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)
    # Attribute any decrypt done by a QStash-dispatched task to system_cron
    # (silent). These are the service running scheduled/on-demand work, not a
    # human read. runtime_views deliberately stay on the ambient "system".
    audit.set_principal("system_cron")

    if task_name not in TASK_MAP:
        logger.warning("Unknown task requested: %s", task_name)
        return JsonResponse({"error": "Unknown task"}, status=404)

    task_path = TASK_MAP[task_name]
    execution_id = str(uuid.uuid4())[:8]

    # Parse arguments from request body (JSON)
    import json

    task_args = []
    task_kwargs = {}
    if request.body:
        try:
            body = json.loads(request.body)
            task_args = body.get("args", [])
            task_kwargs = body.get("kwargs", {})
        except (json.JSONDecodeError, AttributeError):
            pass

    # Coerce malformed shapes (args not a list, kwargs not a dict) into the
    # empty defaults so the signature check below produces a clean
    # "missing required argument" error rather than an opaque TypeError
    # from unpacking. Examples: ``{"args": null}``, ``{"args": 42}``,
    # ``{"kwargs": "string"}`` — all real shapes QStash can deliver if a
    # publisher serializes weirdly (see #557 + the publish_json vs
    # publish-as-string MCP gotcha).
    if not isinstance(task_args, list):
        task_args = []
    if not isinstance(task_kwargs, dict):
        task_kwargs = {}

    # Validate args against the task signature before we attempt to execute.
    # QStash retries 5xx three times; a bad-args message can never succeed,
    # so we return 400 instead — parks the message in DLQ on first delivery
    # and stops the retry storm. See issue #557.
    try:
        arg_error = _validate_task_signature(task_path, task_args, task_kwargs)
    except (ImportError, AttributeError) as exc:
        logger.error("[%s] Cannot resolve task %s: %s", execution_id, task_name, exc)
        return JsonResponse(
            {
                "status": "error",
                "task_name": task_name,
                "execution_id": execution_id,
                "error": f"Task resolution failed: {exc}",
            },
            status=500,
        )

    if arg_error:
        logger.warning(
            "[%s] Bad args for %s: %s (args=%s kwargs=%s)",
            execution_id,
            task_name,
            arg_error,
            task_args,
            list(task_kwargs.keys()),
        )
        return JsonResponse(
            {
                "status": "error",
                "task_name": task_name,
                "execution_id": execution_id,
                "error": f"Invalid task arguments: {arg_error}",
            },
            status=400,
        )

    try:
        logger.info("[%s] QStash executing task %s -> %s", execution_id, task_name, task_path)
        result = execute_task_sync(task_path, *task_args, **task_kwargs)
        logger.info("[%s] Task %s completed successfully", execution_id, task_name)

        return JsonResponse(
            {
                "status": "completed",
                "task_name": task_name,
                "execution_id": execution_id,
                "result": str(result) if result else None,
            }
        )
    except Exception as e:
        logger.error("[%s] Task %s failed: %s", execution_id, task_name, e)
        logger.error(traceback.format_exc())
        return JsonResponse(
            {
                "status": "error",
                "task_name": task_name,
                "execution_id": execution_id,
                "error": str(e),
            },
            status=500,
        )


@csrf_exempt
@require_POST
def trigger_task_debug(request, task_name):
    """
    Debug endpoint that skips signature verification.
    Only available when DEBUG=True.

    URL: /api/cron/trigger-debug/<task_name>/
    """
    if not settings.DEBUG:
        return JsonResponse({"error": "Debug endpoint disabled"}, status=403)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    if task_name not in TASK_MAP:
        return JsonResponse({"error": "Unknown task"}, status=404)

    task_path = TASK_MAP[task_name]
    execution_id = str(uuid.uuid4())[:8]

    import json

    task_args = []
    task_kwargs = {}
    if request.body:
        try:
            body = json.loads(request.body)
            task_args = body.get("args", [])
            task_kwargs = body.get("kwargs", {})
        except (json.JSONDecodeError, AttributeError):
            pass

    if not isinstance(task_args, list):
        task_args = []
    if not isinstance(task_kwargs, dict):
        task_kwargs = {}

    # Same boundary-validation contract as ``trigger_task`` — see #557.
    try:
        arg_error = _validate_task_signature(task_path, task_args, task_kwargs)
    except (ImportError, AttributeError) as exc:
        logger.error("[%s] DEBUG cannot resolve task %s: %s", execution_id, task_name, exc)
        return JsonResponse(
            {
                "status": "error",
                "task_name": task_name,
                "execution_id": execution_id,
                "error": f"Task resolution failed: {exc}",
            },
            status=500,
        )

    if arg_error:
        logger.warning(
            "[%s] DEBUG bad args for %s: %s (args=%s kwargs=%s)",
            execution_id,
            task_name,
            arg_error,
            task_args,
            list(task_kwargs.keys()),
        )
        return JsonResponse(
            {
                "status": "error",
                "task_name": task_name,
                "execution_id": execution_id,
                "error": f"Invalid task arguments: {arg_error}",
            },
            status=400,
        )

    try:
        logger.info("[%s] DEBUG executing task %s -> %s", execution_id, task_name, task_path)
        result = execute_task_sync(task_path, *task_args, **task_kwargs)
        logger.info("[%s] DEBUG task %s completed", execution_id, task_name)

        return JsonResponse(
            {
                "status": "completed",
                "task_name": task_name,
                "execution_id": execution_id,
                "result": str(result) if result else None,
            }
        )
    except Exception as e:
        logger.error("[%s] DEBUG task %s failed: %s", execution_id, task_name, e)
        return JsonResponse(
            {
                "status": "error",
                "task_name": task_name,
                "execution_id": execution_id,
                "error": str(e),
                "traceback": traceback.format_exc(),
            },
            status=500,
        )


def list_tasks(request):
    """
    List all available tasks. Only available when DEBUG=True.

    URL: /api/cron/tasks/
    """
    if not settings.DEBUG:
        return JsonResponse({"error": "Endpoint disabled"}, status=403)

    return JsonResponse({"tasks": list(TASK_MAP.keys()), "count": len(TASK_MAP)})


@csrf_exempt
@require_POST
def apply_pending_configs(request):
    """Apply queued config updates for idle active tenants.

    URL: /api/cron/apply-pending-configs/
    """
    if not verify_qstash_signature(request):
        logger.warning("Unauthorized apply-pending-configs cron attempt")
        return JsonResponse({"error": "Invalid signature"}, status=401)

    cutoff = timezone.now() - timedelta(minutes=15)
    query = Tenant.objects.filter(
        pending_config_version__gt=models.F("config_version"),
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        hibernated_at__isnull=True,
    )
    query = query.filter(
        models.Q(last_message_at__isnull=True) | models.Q(last_message_at__lt=cutoff),
    )
    evaluated = query.count()

    # Two disjoint work sets, each self-contained per tenant:
    #   Image set — tenants on a stale image. apply_single_tenant_image_task
    #     updates the container image and THEN writes the tenant's config
    #     against the new image's schema (image before config — see the
    #     incident note on that task). These tenants are EXCLUDED from the
    #     immediate config write below: writing their new config while the OLD
    #     image is still the active revision is exactly what broke a tenant on
    #     2026-07-03 (old image rejected a config referencing a
    #     new-image-only plugin dir and fell back to openclaw.json.last-good).
    #   Config set — config-pending tenants NOT getting an image update. A plain
    #     file-share write (model swap, cron-prompt edit) the running image
    #     already understands.
    from apps.cron.publish import publish_batch

    config_tasks: list[tuple[str, tuple, dict]] = []
    image_tasks: list[tuple[str, tuple, dict]] = []

    # 1. Image updates for tenants on a stale image. Same cron_wake_at guard
    # as hibernate_idle_tenants_task — pushing a new image triggers a revision
    # update that terminates the running container, which would kill an
    # about-to-fire scheduled cron in the wake-for-cron window.
    # Wake_hibernated_tenant already refreshes to current OPENCLAW_IMAGE_TAG at
    # wake time, so genuinely-stale cron-woken tenants don't exist in practice;
    # skipping them here is just defense-in-depth for the rare edge case where a
    # fleet bump lands between wake and apply-pending.
    desired_tag = getattr(settings, "OPENCLAW_IMAGE_TAG", "latest")
    image_count = 0
    image_skipped_imminent_cron = 0
    if desired_tag and desired_tag != "latest":
        stale_image_tenants = (
            Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
                container_id__gt="",
                hibernated_at__isnull=True,
            )
            .exclude(
                container_image_tag=desired_tag,
            )
            .filter(
                models.Q(last_message_at__isnull=True) | models.Q(last_message_at__lt=cutoff),
            )
            .filter(
                models.Q(cron_wake_at__isnull=True) | models.Q(cron_wake_at__lt=cutoff),
            )
        )

        from apps.orchestrator.hibernation import _cron_active_or_imminent

        for tenant in stale_image_tenants:
            # Mirror the safeguard in hibernate_idle_tenants_task: image
            # bump triggers a revision update that SIGTERMs the container,
            # so an in-flight or imminent user cron would get interrupted.
            # cron_wake_at above only protects tenants we just woke for
            # a cron; long-running awake tenants need the forward-looking
            # + in-flight check.
            defer_reason = _cron_active_or_imminent(tenant)
            if defer_reason:
                image_skipped_imminent_cron += 1
                logger.info(
                    "apply_pending_configs: deferring image bump for tenant %s (%s)",
                    str(tenant.id)[:8],
                    defer_reason,
                )
                continue
            image_tasks.append(("apply_single_tenant_image", (str(tenant.id), desired_tag), {}))
            image_count += 1

    # Tenants receiving an image update this cycle. Their config is written by
    # the image task AFTER the restart, so they must NOT also get an immediate
    # config write (which would land on the still-running old image) or a cron
    # seed (the post-image reseed handles that).
    image_tenant_ids = {t_id for _, (t_id, _), _ in image_tasks}

    # 2. Config updates for idle config-pending tenants NOT getting an image
    # update this cycle (image-update tenants are covered by their image task's
    # own post-restart config write).
    config_count = 0
    for tenant in query:
        if str(tenant.id) in image_tenant_ids:
            continue
        config_tasks.append(("apply_single_tenant_config", (str(tenant.id),), {}))
        config_count += 1

    # 3. Re-seed cron jobs for entitled, active (non-hibernated) tenants that
    #    are NOT about to get an image update. Image updates restart the
    #    container (wiping SQLite), so seeding now is wasted work — the
    #    post-image reseed in apply_single_tenant_image_task handles them.
    active_tenants_with_containers = (
        Tenant.entitled_active()
        .filter(
            hibernated_at__isnull=True,
        )
        .values_list("id", flat=True)
    )

    cron_seed_count = 0
    for tenant_id in active_tenants_with_containers:
        if str(tenant_id) in image_tenant_ids:
            continue  # post-image reseed will handle this tenant
        config_tasks.append(("seed_cron_jobs", (str(tenant_id),), {}))
        cron_seed_count += 1

    # Publish both sets immediately. They are disjoint per tenant, and each
    # image task orders its own image-then-config write internally, so no
    # cross-batch delay is needed. (The previous 30s delay on the image batch
    # assumed config had to land BEFORE the restart — the inverted assumption
    # that caused the 2026-07-03 incident.)
    enqueued = 0
    try:
        if image_tasks:
            enqueued += publish_batch(image_tasks)
    except Exception:
        logger.exception("Batch publish failed for image tasks")

    try:
        if config_tasks:
            enqueued += publish_batch(config_tasks)
    except Exception:
        logger.exception("Batch publish failed for config tasks")

    all_tasks = config_tasks + image_tasks
    success = enqueued == len(all_tasks) if all_tasks else True

    return JsonResponse(
        {
            "config_enqueued": config_count if success else 0,
            "config_failed": 0 if success else config_count,
            "evaluated": evaluated,
            "image_enqueued": image_count if success else 0,
            "image_failed": 0 if success else image_count,
            "image_skipped_imminent_cron": image_skipped_imminent_cron,
            "cron_seed_enqueued": cron_seed_count if success else 0,
            "cron_seed_failed": 0 if success else cron_seed_count,
            "batch_total": len(all_tasks),
            "batch_enqueued": enqueued,
        }
    )


@csrf_exempt
@require_POST
def force_reseed_crons(request):
    """DEPRECATED endpoint kept as a thin shim.

    The legacy implementation lived here from the gateway-canonical era and
    accumulated bugs (broken ``cron.list`` unwrap, hardcoded 5-name system
    list that missed half the seed). Force-reseed is now handled by
    refreshing each tenant's postgres CronJob rows from seed — the signal
    handler pushes the new state to OpenClaw via the reconciler.

    URL: /api/cron/force-reseed-crons/
    """
    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if provided and deploy_secret and provided == deploy_secret:
        pass  # CI deploy auth
    elif not verify_qstash_signature(request):
        logger.warning("Unauthorized force-reseed-crons attempt")
        return JsonResponse({"error": "Invalid signature"}, status=401)

    from apps.orchestrator.services import refresh_system_cron_rows_from_seed

    tenants = Tenant.entitled_active().select_related("user")

    results = []
    for tenant in tenants:
        tid = str(tenant.id)[:8]
        entry: dict = {"tenant": tid, "created": 0, "updated": 0, "preserved_custom": 0, "errors": []}
        try:
            summary = refresh_system_cron_rows_from_seed(tenant)
            entry["created"] = summary["created"]
            entry["updated"] = summary["updated"]
            entry["preserved_custom"] = summary["preserved_custom"]
        except Exception as exc:
            entry["errors"].append(str(exc)[:120])
        results.append(entry)

    total_touched = sum(r["created"] + r["updated"] for r in results)
    total_errors = sum(len(r["errors"]) for r in results)
    return JsonResponse(
        {
            "tenants": len(results),
            "total_touched": total_touched,
            "total_errors": total_errors,
            "details": results,
        }
    )


@csrf_exempt
@require_POST
def restart_tenant_container(request):
    """Restart a tenant's OpenClaw container. QStash-verified."""
    if not verify_qstash_signature(request):
        return JsonResponse({"error": "Invalid signature"}, status=401)

    body = {}
    if request.body:
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, TypeError):
            body = {}

    tenant_id = request.POST.get("tenant_id") or body.get("tenant_id")
    if not tenant_id:
        return JsonResponse({"error": "tenant_id required"}, status=400)

    try:
        tenant = Tenant.objects.get(id=tenant_id, status=Tenant.Status.ACTIVE)
    except Tenant.DoesNotExist:
        return JsonResponse({"error": "Tenant not found"}, status=404)

    if not tenant.container_id:
        return JsonResponse({"error": "No container"}, status=400)

    restart_container_app(tenant.container_id)
    return JsonResponse({"restarted": True, "container": tenant.container_id})


def _unentitled_active_tenants():
    """Return queryset of active tenants without entitlement.

    Entitled = paid (Stripe subscription) OR on a valid (unexpired) trial.
    The inverse is "active but no current entitlement" — these are the
    accounts the daily sweep should suspend.

    Note: this does NOT filter on ``is_trial=True``. The earlier query did,
    which silently let through any tenant whose ``is_trial`` was flipped
    to False at some prior point without their status moving to SUSPENDED.
    Production had 17 such ghost tenants accumulating LLM cost since their
    trials ended 2026-04-15.
    """
    now = timezone.now()
    return Tenant.objects.filter(status=Tenant.Status.ACTIVE).exclude(
        models.Q(stripe_subscription_id__gt="")
        | models.Q(is_trial=True, trial_ends_at__gt=now)
        # Budget-exempt tenants (canary, internal accounts) live outside the
        # billing lifecycle — they never carry a real subscription, so without
        # this they read as "unentitled" and the sweep suspends them. This is
        # what suspended the canary on 2026-06-10 when its (stale) subscription
        # id was cleared.
        | models.Q(is_budget_exempt=True),
    )


def _suspend_unentitled_tenant(tenant):
    """Disable crons, flip status to SUSPENDED, hibernate container.

    Order matters: disable while gateway still reachable, then mark
    suspended (the visible state change for the user), then hibernate
    (which stops Azure costs). Reused by ``expire_trials`` and the
    ``enforce_entitlement`` management command.
    """
    from apps.cron.suspension import suspend_tenant_crons
    from apps.orchestrator.azure_client import hibernate_container_app

    crons_disabled = 0
    hibernated = False

    if tenant.container_fqdn:
        try:
            cron_result = suspend_tenant_crons(tenant)
            crons_disabled = cron_result.get("disabled", 0)
        except Exception:
            logger.exception("enforce_entitlement: failed to suspend crons for tenant %s", tenant.id)

    tenant.is_trial = False
    tenant.status = Tenant.Status.SUSPENDED
    tenant.save(update_fields=["is_trial", "status", "updated_at"])

    if tenant.container_id:
        try:
            hibernate_container_app(tenant.container_id)
            hibernated = True
        except Exception:
            logger.exception(
                "enforce_entitlement: failed to hibernate container %s for tenant %s",
                tenant.container_id,
                tenant.id,
            )

    return {"crons_disabled": crons_disabled, "hibernated": hibernated}


@csrf_exempt
@require_POST
def expire_trials(request):
    """Suspend any active tenant that lacks entitlement.

    Daily QStash cron. Catches both:
      - Trials that have reached their end date with no paid conversion.
      - Ghost tenants (``is_trial=False, status='active', no stripe_sub``)
        that slipped past prior sweeps because the previous query filtered
        on ``is_trial=True``.

    Disables cron jobs (so they can be re-enabled on subscription, not
    deleted) and hibernates the container.

    URL: /api/v1/cron/expire-trials/ (kept for QStash schedule continuity)
    """
    if not verify_qstash_signature(request):
        logger.warning("Unauthorized expire-trials cron attempt")
        return JsonResponse({"error": "Invalid signature"}, status=401)

    updated = 0
    crons_disabled = 0
    hibernated = 0
    already_hibernated = 0

    for tenant in _unentitled_active_tenants():
        if tenant.hibernated_at is not None:
            already_hibernated += 1
        result = _suspend_unentitled_tenant(tenant)
        updated += 1
        crons_disabled += result["crons_disabled"]
        if result["hibernated"]:
            hibernated += 1

    logger.info(
        "expire_trials: suspended %d tenants (%d already hibernated, %d crons disabled, %d new hibernations)",
        updated,
        already_hibernated,
        crons_disabled,
        hibernated,
    )

    return JsonResponse(
        {
            "updated": updated,
            "already_hibernated": already_hibernated,
            "crons_disabled": crons_disabled,
            "hibernated": hibernated,
        }
    )


@csrf_exempt
def bump_all_pending_configs(request):
    """Mark all active tenants as needing a config update.

    Called by CI after deploy to ensure new workspace files propagate
    to all tenant file shares on their next idle cycle.

    Auth: X-Deploy-Secret header must match DEPLOY_SECRET setting.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    if not deploy_secret:
        logger.error("DEPLOY_SECRET not configured — bump_all_pending_configs rejected")
        return JsonResponse({"error": "Not configured"}, status=503)

    provided = request.headers.get("X-Deploy-Secret", "")
    if not provided or provided != deploy_secret:
        logger.warning("Unauthorized bump_all_pending_configs attempt")
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from django.db.models import F as DbF
    from django.db.models import Q

    has_channel = (
        Q(user__telegram_chat_id__isnull=False) | Q(user__line_user_id__isnull=False) | Q(device_tokens__isnull=False)
    )
    grace_cutoff = timezone.now() - timedelta(days=1)

    # Reset no-channel tenants created >1 day ago to version 0
    # (new tenants get a 24h grace period to register a channel)
    no_channel_reset = (
        Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
            created_at__lt=grace_cutoff,
        )
        .exclude(has_channel)
        .exclude(
            config_version=0,
            pending_config_version=0,
        )
        .update(config_version=0, pending_config_version=0)
    )

    if no_channel_reset:
        logger.info("bump_all: reset %d no-channel tenant(s) to version 0", no_channel_reset)

    # Only bump tenants that have a delivery channel (or were created <1 day ago)
    count = (
        Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
        )
        .filter(has_channel | Q(created_at__gte=grace_cutoff))
        .update(pending_config_version=DbF("config_version") + 1)
    )

    logger.info("bump_all_pending_configs: marked %d tenant(s) for config update", count)
    return JsonResponse({"queued": count})


@csrf_exempt
def backfill_welcomes(request):
    """Retry missing Fuel/Gravity welcome schedules for active tenants.

    Called by CI after deploy for the rare case where both the activation-flow
    stamp and a prior schedule-time stamp failed transiently. The schedulers
    stamp immediately after a successful cron creation and skip stamped
    tenants, so re-running cannot re-welcome an already-recorded activation.
    Pre-stamp enabled tenants are retired separately by the one-time
    ``stamp_grandfathered_welcomes`` management command.

    Auth: X-Deploy-Secret header must match DEPLOY_SECRET setting.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    if not deploy_secret:
        logger.error("DEPLOY_SECRET not configured — backfill_welcomes rejected")
        return JsonResponse({"error": "Not configured"}, status=503)

    provided = request.headers.get("X-Deploy-Secret", "")
    if not provided or provided != deploy_secret:
        logger.warning("Unauthorized backfill_welcomes attempt")
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from collections import Counter

    from apps.orchestrator.welcome_scheduler import WelcomeStatus

    tenants = list(Tenant.objects.select_related("user").filter(status=Tenant.Status.ACTIVE).exclude(container_id=""))
    per_feature: dict[str, Counter] = {"fuel": Counter(), "finance": Counter()}

    for tenant in tenants:
        if tenant.fuel_enabled:
            from apps.fuel.views import _schedule_fuel_welcome

            _tally(_schedule_fuel_welcome, tenant, per_feature["fuel"], "fuel")
        if tenant.finance_active:
            from apps.finance.views import _schedule_finance_welcome

            _tally(_schedule_finance_welcome, tenant, per_feature["finance"], "finance")

    response = {
        "tenants_walked": len(tenants),
        "fuel": dict(per_feature["fuel"]),
        "finance": dict(per_feature["finance"]),
    }
    logger.info("backfill_welcomes: %s", response)
    # WelcomeStatus is referenced for symmetry — surfacing the enum in
    # logs makes it easier to grep deploy output for outcome categories.
    response["statuses"] = [s.value for s in WelcomeStatus]
    return JsonResponse(response)


def _tally(helper, tenant, counts, feature: str) -> None:
    """Invoke a welcome scheduler and tally its outcome.

    Distinguishes scheduled / replaced_stale / skipped_pending /
    skipped_already_delivered / failed. Raises are caught and counted as
    "failed" so a single tenant's gateway hiccup doesn't abort the
    whole backfill loop.
    """
    try:
        status = helper(tenant)
    except Exception:
        counts["failed"] += 1
        logger.warning("backfill_welcomes: %s failed for %s", feature, str(tenant.id)[:8], exc_info=True)
        return
    key = getattr(status, "value", str(status))
    counts[key] += 1


@csrf_exempt
def register_system_crons(request):
    """Register system QStash cron schedules from CI after deploy.

    Idempotent — existing schedules are left alone.
    Auth: X-Deploy-Secret header must match DEPLOY_SECRET setting.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    if not deploy_secret:
        logger.error("DEPLOY_SECRET not configured")
        return JsonResponse({"error": "Not configured"}, status=503)

    provided = request.headers.get("X-Deploy-Secret", "")
    if not provided or provided != deploy_secret:
        logger.warning("Unauthorized register_system_crons attempt")
        return JsonResponse({"error": "Unauthorized"}, status=401)

    import json as _json

    try:
        body = _json.loads(request.body) if request.body else {}
    except Exception:
        body = {}

    base_url = body.get("base_url", "").rstrip("/")
    if not base_url:
        base_url = getattr(settings, "DJANGO_BASE_URL", "").rstrip("/")
    if not base_url:
        return JsonResponse({"error": "base_url required"}, status=400)

    # Register/update/deregister core lives in apps/cron/system_cron_registry.py
    # so the daily QStash-signed ``reconcile_system_crons`` task can run the exact
    # same logic (belt-and-braces for incident 2026-07-09b). This view keeps its
    # X-Deploy-Secret auth and just delegates.
    from apps.cron.system_cron_registry import (
        SystemCronConfigError,
        sync_system_crons,
    )

    try:
        result = sync_system_crons(base_url)
    except SystemCronConfigError:
        logger.error("QSTASH_TOKEN not configured")
        return JsonResponse({"error": "QSTASH_TOKEN not configured"}, status=503)

    return JsonResponse(result)


@csrf_exempt
def run_update_cron_prompts(request):
    """Refresh system cron rows from seed for all active tenants.

    Refreshes postgres CronJob rows; the post_save signal triggers
    ``regenerate_tenant_crons`` which pushes drift to each tenant's
    OpenClaw runtime.

    Auth: X-Deploy-Secret header.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    from apps.orchestrator.services import refresh_system_cron_rows_from_seed

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
    ).exclude(container_id="")

    results = []
    for tenant in tenants:
        try:
            result = refresh_system_cron_rows_from_seed(tenant)
            results.append(
                {
                    "tenant": str(tenant.id)[:8],
                    "created": result["created"],
                    "updated": result["updated"],
                    "preserved_custom": result["preserved_custom"],
                }
            )
        except Exception as e:
            results.append({"tenant": str(tenant.id)[:8], "status": "error", "error": str(e)})

    total_touched = sum(r.get("created", 0) + r.get("updated", 0) for r in results)
    logger.info("run_update_cron_prompts: %d tenants, %d rows touched", len(results), total_touched)
    return JsonResponse({"results": results, "total_touched": total_touched})


@csrf_exempt
def run_backfill_lesson_embeddings(request):
    """Backfill embeddings for approved lessons missing them.

    Auth: X-Deploy-Secret header.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    from apps.lessons.models import Lesson
    from apps.lessons.services import process_approved_lesson

    lessons = Lesson.objects.filter(status="approved", embedding__isnull=True)
    total = lessons.count()
    processed = 0
    errors = 0

    for lesson in lessons:
        try:
            process_approved_lesson(lesson)
            processed += 1
        except Exception as e:
            errors += 1
            logger.error("backfill_lesson_embeddings: lesson %s failed: %s", lesson.id, e)

    logger.info("backfill_lesson_embeddings: %d/%d processed, %d errors", processed, total, errors)
    return JsonResponse({"total": total, "processed": processed, "errors": errors})


@csrf_exempt
def verify_gateway_tools(request):
    """Verify that the OpenClaw gateway cron tool is available.

    Picks one active tenant and calls cron.list via the gateway.
    Returns 200 if the tool responds, 500 if not.
    Auth: X-Deploy-Secret header.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.models import Tenant

    tenant = (
        Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_fqdn__isnull=False,
        )
        .exclude(container_fqdn="")
        .first()
    )

    if not tenant:
        return JsonResponse({"ok": True, "skipped": True, "reason": "no active tenants with containers"})

    try:
        from apps.cron.gateway_client import invoke_gateway_tool

        result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        logger.info("verify_gateway_tools: cron.list succeeded for tenant %s", str(tenant.id)[:8])
        return JsonResponse({"ok": True, "tenant": str(tenant.id)[:8], "cron_tool": "available"})
    except Exception as exc:
        logger.error("verify_gateway_tools: cron.list FAILED for tenant %s: %s", str(tenant.id)[:8], exc)
        return JsonResponse(
            {"ok": False, "tenant": str(tenant.id)[:8], "error": str(exc)[:300]},
            status=500,
        )


@csrf_exempt
def run_rewrite_lessons_actionable(request):
    """Rewrite approved lessons to be actionable advice via LLM.

    Auth: X-Deploy-Secret header.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("rewrite_lessons_actionable", stdout=out)
    output = out.getvalue()
    # Log only metadata — `rewrite_lessons_actionable` prints
    # "[lesson_id] BEFORE: {lesson.text}" / "AFTER: {rewritten}" lines that
    # contain lesson text derived from user daily notes. Container logs ship
    # to a shared workspace; keep tenant content out of them.
    logger.info(
        "rewrite_lessons_actionable: completed (%d bytes, %d lines output)",
        len(output),
        output.count("\n"),
    )
    return JsonResponse({"ok": True, "output_bytes": len(output)})


@csrf_exempt
def run_reseed_lessons(request):
    """Delete journal-sourced lessons and re-extract from all daily notes.

    Auth: X-Deploy-Secret header.
    URL: /api/cron/run-reseed-lessons/
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("reseed_lessons", stdout=out)
    output = out.getvalue()
    # `reseed_lessons` prints per-tenant progress including lesson counts
    # plus interleaved extraction details that can echo daily-note text.
    # Same shared-workspace concern as rewrite_lessons_actionable above.
    logger.info(
        "reseed_lessons: completed (%d bytes, %d lines output)",
        len(output),
        output.count("\n"),
    )
    return JsonResponse({"ok": True, "output_bytes": len(output)})


@csrf_exempt
def scrub_thread_titles(request):
    """Scrub persisted chat-thread titles without logging title content.

    Auth: X-Deploy-Secret header.
    URL: /api/cron/scrub-thread-titles/?apply=1&tenant_id=<uuid>
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    import re
    from io import StringIO

    from django.core.management import call_command

    dry_run = request.GET.get("apply") != "1"
    tenant_id = request.GET.get("tenant_id") or None
    out = StringIO()
    call_command(
        "scrub_chat_thread_titles",
        dry_run=dry_run,
        tenant_id=tenant_id,
        stdout=out,
    )
    output = out.getvalue()
    summary = re.search(
        r"Scanned (?P<scanned>\d+); (?:would change|changed) (?P<changed>\d+); "
        r"errors (?P<errors>\d+) across (?P<tenants>\d+) tenant\(s\)",
        output,
    )
    counts = {key: int(value) for key, value in summary.groupdict().items()}
    logger.info(
        "scrub_chat_thread_titles: completed (dry_run=%s, tenant_id=%s, scanned=%d, changed=%d, errors=%d, tenants=%d)",
        dry_run,
        tenant_id or "all",
        counts["scanned"],
        counts["changed"],
        counts["errors"],
        counts["tenants"],
    )
    return JsonResponse(
        {
            "ok": True,
            "dry_run": dry_run,
            "tenant_id": tenant_id,
            **counts,
            "output_bytes": len(output),
        }
    )


def _parse_internal_ops_body(request, allowed_fields: set[str]):
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"error": "Invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return None, JsonResponse({"error": "JSON body must be an object"}, status=400)

    unknown = sorted(set(body) - allowed_fields)
    if unknown:
        return None, JsonResponse(
            {
                "error": "Unknown fields",
                "fields": unknown,
            },
            status=400,
        )

    missing = sorted(allowed_fields - set(body))
    if missing:
        return None, JsonResponse(
            {
                "error": "Missing required fields",
                "fields": missing,
            },
            status=400,
        )
    return body, None


@csrf_exempt
def repair_fuel_rows(request):
    """Retire unmanaged-prefix CronJob rows for one tenant.

    Auth: X-Deploy-Secret header.
    URL: /api/cron/repair-fuel-rows/
    Body: {"tenant_id": "<uuid>", "confirm": false}
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    body, error = _parse_internal_ops_body(request, {"tenant_id", "confirm"})
    if error:
        return error
    if not isinstance(body["confirm"], bool):
        return JsonResponse({"error": "confirm must be a boolean"}, status=400)

    from django.core.management.base import CommandError

    from apps.cron.management.commands.repair_fuel_cron_rows import (
        repair_fuel_cron_rows,
    )

    try:
        result = repair_fuel_cron_rows(
            tenant_id=body["tenant_id"],
            confirm=body["confirm"],
        )
    except CommandError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    logger.info(
        "repair_fuel_rows: completed (tenant_id=%s, confirm=%s, matched=%d, retired=%d, already_retired=%d)",
        body["tenant_id"],
        body["confirm"],
        result["matched"],
        result["retired"],
        result["already_retired"],
    )
    return JsonResponse(
        {
            "ok": True,
            "tenant_id": str(body["tenant_id"]),
            "confirm": body["confirm"],
            **result,
        }
    )


@csrf_exempt
def retire_quarantined_rows(request):
    """Remove share-observed cron jobs by exact gateway ID.

    Auth: X-Deploy-Secret header.
    URL: /api/cron/retire-quarantined/
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    fields = {"tenant_id", "name", "bucket", "limit", "confirm"}
    body, error = _parse_internal_ops_body(request, fields)
    if error:
        return error
    if not isinstance(body["name"], str) or not body["name"].strip():
        return JsonResponse({"error": "name must be a non-empty string"}, status=400)
    if not isinstance(body["bucket"], str) or body["bucket"] not in {"duplicate", "expired"}:
        return JsonResponse({"error": "bucket must be duplicate or expired"}, status=400)
    if isinstance(body["limit"], bool) or not isinstance(body["limit"], int):
        return JsonResponse({"error": "limit must be an integer"}, status=400)
    if not isinstance(body["confirm"], bool):
        return JsonResponse({"error": "confirm must be a boolean"}, status=400)

    from django.core.management.base import CommandError

    from apps.cron.management.commands.retire_quarantined import (
        retire_quarantined,
    )

    try:
        result = retire_quarantined(
            tenant_id=body["tenant_id"],
            name=body["name"],
            bucket=body["bucket"],
            limit=body["limit"],
            confirm=body["confirm"],
        )
    except CommandError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    logger.info(
        "retire_quarantined_rows: completed (tenant_id=%s, name=%s, bucket=%s, confirm=%s, "
        "matched=%d, removed=%d, failed=%d, remaining=%d)",
        body["tenant_id"],
        body["name"],
        body["bucket"],
        body["confirm"],
        result["matched"],
        result["removed"],
        result["failed"],
        result["remaining"],
    )
    return JsonResponse(
        {
            "ok": True,
            "tenant_id": str(body["tenant_id"]),
            "name": body["name"],
            "bucket": body["bucket"],
            "confirm": body["confirm"],
            **{key: result[key] for key in ("matched", "removed", "failed", "remaining", "removed_ids")},
        }
    )


@csrf_exempt
def delete_registry_cron(request):
    """Delete one managed registry row and its bound gateway job.

    Auth: X-Deploy-Secret header.
    URL: /api/cron/delete-registry-cron/
    Body: {"tenant_id": "<uuid>", "name": "cron name"}
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    body, error = _parse_internal_ops_body(request, {"tenant_id", "name"})
    if error:
        return error
    if not isinstance(body["name"], str) or not body["name"].strip():
        return JsonResponse({"error": "name must be a non-empty string"}, status=400)

    try:
        tenant = Tenant.objects.get(pk=body["tenant_id"])
    except (Tenant.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"error": "Cron job not found"}, status=404)

    from apps.cron import postgres_canonical as pg
    from apps.cron.gateway_client import GatewayError, cron_remove
    from apps.cron.models import CronJob

    row = CronJob.objects.filter(
        tenant=tenant,
        name=body["name"],
        managed=True,
    ).first()
    if not row:
        logger.info(
            "delete_registry_cron: no match (tenant_id=%s, name=%s)",
            body["tenant_id"],
            body["name"],
        )
        return JsonResponse({"error": "Cron job not found"}, status=404)

    deleted = {
        "id": str(row.id),
        "name": row.name,
        "gateway_job_id": row.gateway_job_id or None,
    }
    gateway_job_id = row.gateway_job_id
    payload, code = pg.delete_job(tenant, row.name)
    if code != 204:
        return JsonResponse(payload, status=code)

    gateway_removal_attempted = bool(gateway_job_id)
    gateway_removal_succeeded = False
    if gateway_removal_attempted:
        try:
            cron_remove(tenant, job_id=gateway_job_id)
        except GatewayError:
            logger.warning(
                "delete_registry_cron: bound gateway removal failed (tenant_id=%s, name=%s)",
                body["tenant_id"],
                body["name"],
            )
        else:
            gateway_removal_succeeded = True

    logger.info(
        "delete_registry_cron: completed (tenant_id=%s, name=%s, gateway_removal_attempted=%s, "
        "gateway_removal_succeeded=%s)",
        body["tenant_id"],
        body["name"],
        gateway_removal_attempted,
        gateway_removal_succeeded,
    )
    return JsonResponse(
        {
            "ok": True,
            "tenant_id": str(tenant.id),
            "deleted": deleted,
            "gateway_removal_attempted": gateway_removal_attempted,
            "gateway_removal_succeeded": gateway_removal_succeeded,
        }
    )


@csrf_exempt
@require_POST
def broadcast_message(request):
    """Send a one-off message from each tenant's agent to their user.

    URL: /api/cron/broadcast-message/
    Body: {"message": "prompt text for the agent"}

    The agent receives the prompt and uses nbhd_send_to_user to deliver it.
    Each tenant is processed via QStash to avoid blocking the web worker.
    """
    if not verify_qstash_signature(request):
        deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
        provided = request.headers.get("X-Deploy-Secret", "")
        if not (provided and deploy_secret and provided == deploy_secret):
            return JsonResponse({"error": "Invalid signature"}, status=401)

    import json as _json

    try:
        body = _json.loads(request.body)
    except (ValueError, _json.JSONDecodeError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    message = body.get("message", "").strip()
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)

    # Idempotency key: caller can supply one, or we generate from message hash.
    # QStash deduplicates messages with the same key within ~5 minutes.
    import hashlib

    broadcast_key = body.get("idempotency_key") or hashlib.sha256(message.encode()).hexdigest()[:16]

    from apps.cron.publish import publish_batch

    tenants = Tenant.entitled_active().filter(container_fqdn__gt="")

    batch_tasks = []
    for tenant in tenants:
        # Per-tenant deduplication key prevents double-delivery if endpoint is
        # called multiple times with the same message (e.g. QStash retries).
        tenant_key = f"{broadcast_key}-{str(tenant.id)[:8]}"
        batch_tasks.append(
            (
                "broadcast_single_tenant",
                (str(tenant.id), message),
                {},
                tenant_key,
            )
        )

    try:
        enqueued = publish_batch(batch_tasks)
    except Exception:
        logger.exception("Batch publish failed for broadcast")
        enqueued = 0

    failed = len(batch_tasks) - enqueued
    return JsonResponse({"enqueued": enqueued, "failed": failed})


@csrf_exempt
@require_POST
def dedup_crons(request):
    """Remove duplicate cron jobs from tenant containers.

    URL: /api/cron/dedup-crons/
    Without ?tenant=UUID: fans out per-tenant via QStash (async).
    With ?tenant=UUID: runs synchronously for that tenant (immediate result).
    """
    if not verify_qstash_signature(request):
        deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
        provided = request.headers.get("X-Deploy-Secret", "")
        if not (provided and deploy_secret and provided == deploy_secret):
            return JsonResponse({"error": "Invalid signature"}, status=401)

    # Single-tenant sync mode — immediate feedback for debugging
    tenant_id = request.GET.get("tenant") or request.POST.get("tenant_id")
    if tenant_id:
        from apps.orchestrator.services import dedup_tenant_cron_jobs

        tenant = Tenant.objects.filter(
            id=tenant_id,
            status=Tenant.Status.ACTIVE,
            container_fqdn__gt="",
        ).first()
        if not tenant:
            return JsonResponse({"error": f"Tenant {tenant_id} not found or inactive"}, status=404)

        result = dedup_tenant_cron_jobs(tenant)
        return JsonResponse(
            {
                "tenant": str(tenant.id),
                "kept": result["kept"],
                "deleted": result["deleted"],
                "errors": result["errors"],
                "duplicates_found": len(result["duplicates"]),
            }
        )

    # Fan out to all active tenants via QStash
    from apps.cron.publish import publish_batch

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        container_fqdn__gt="",
    )

    batch_tasks = [("dedup_cron_jobs", (str(tenant.id),), {}) for tenant in tenants]

    try:
        enqueued = publish_batch(batch_tasks)
    except Exception:
        logger.exception("Batch publish failed for dedup")
        enqueued = 0

    failed = len(batch_tasks) - enqueued
    return JsonResponse({"enqueued": enqueued, "failed": failed})


# Cooldown: don't send duplicate alerts within this window
_HEALTH_ALERT_COOLDOWN_SECONDS = 30 * 60  # 30 minutes
# Shorter backoff when the gateway is slow/cold (read timeout or 5xx during a
# cold start): long enough to stop the every-5-min storm, short enough to retry
# and actually deliver once the gateway warms.
_HEALTH_ALERT_TIMEOUT_COOLDOWN_SECONDS = 10 * 60  # 10 minutes


def _send_alert_to_personal_openclaw(message: str) -> str:
    """Send a health alert to MJ's personal OpenClaw agent.

    Routes through the Cloudflare tunnel to the personal gateway. The agent
    receives the alert and can propose fixes without acting.

    Returns a delivery status:
      - "delivered"    — the gateway accepted it (HTTP 200). Full cooldown.
      - "undeliverable" — a config/auth problem that retrying won't fix
                          (missing env, a 3xx CF-Access login redirect, or a 4xx).
                          Full cooldown so we don't POST a doomed request every 5 minutes.
      - "timeout"      — we connected but the gateway was slow/cold: a client-side
                          read timeout, or a 5xx (incl. Cloudflare 52x) from a
                          cold/waking origin. The personal gateway is itself an
                          idle-hibernating Container App behind Cloudflare, so a
                          real-outage alert often wakes a cold gateway. The caller
                          starts a SHORT backoff — stops the every-tick storm but
                          retries soon enough to land the alert once it warms.
      - "transient"    — a fast connect-side failure (DNS/TCP/connect timeout) or
                          an odd error; the caller leaves the cooldown unset so the
                          next tick retries immediately against a hopefully-up gateway.
    """
    import httpx

    gateway_url = getattr(settings, "ADMIN_OPENCLAW_GATEWAY_URL", "").strip()
    gateway_token = getattr(settings, "ADMIN_OPENCLAW_GATEWAY_TOKEN", "").strip()
    cf_client_id = getattr(settings, "CF_ACCESS_CLIENT_ID", "").strip()
    cf_client_secret = getattr(settings, "CF_ACCESS_CLIENT_SECRET", "").strip()

    if not gateway_url or not gateway_token:
        logger.warning("ADMIN_OPENCLAW_GATEWAY_URL or TOKEN not configured")
        return "undeliverable"

    headers = {
        "Authorization": f"Bearer {gateway_token}",
        "Content-Type": "application/json",
    }
    if cf_client_id and cf_client_secret:
        headers["CF-Access-Client-Id"] = cf_client_id
        headers["CF-Access-Client-Secret"] = cf_client_secret

    url = f"{gateway_url.rstrip('/')}/v1/chat/completions"

    try:
        resp = httpx.post(
            url,
            headers=headers,
            json={
                "model": "openclaw",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are receiving an automated NBHD United platform health alert. "
                            "Review the issue, propose fixes, but do NOT take any action. "
                            "Summarize what happened and what MJ should consider doing."
                        ),
                    },
                    {"role": "user", "content": message},
                ],
            },
            # httpx does NOT follow redirects by default — a 302 is returned as-is
            # (which is what we want: a CF-Access login redirect must read as a
            # failure, not be chased to an HTML login page).
            # Split timeout: a connect/DNS/TCP failure fast-fails in ~10s
            # (-> transient) so it's cleanly separated from "reached the gateway,
            # the LLM round-trip is just slow" (read=75 covers a cold start +
            # generation). The body is tiny, so write/pool never bind.
            timeout=httpx.Timeout(connect=10.0, read=75.0, write=10.0, pool=10.0),
        )
    # Exception order is LOAD-BEARING: ReadTimeout and ConnectTimeout both subclass
    # httpx.TimeoutException, so the specific types MUST precede the broad
    # `except httpx.TimeoutException`; the bare `except Exception` MUST stay last
    # (a plain Exception -> transient, which test_network_error_is_transient pins).
    except httpx.ReadTimeout:
        # Connected fine; the gateway (cold start + LLM round-trip) didn't answer
        # within the read window. It's warming now — back off briefly and retry.
        logger.warning("Personal OpenClaw alert read-timed-out (gateway slow/cold) — backing off")
        return "timeout"
    except httpx.ConnectTimeout:
        logger.warning("Personal OpenClaw alert connect-timed-out (gateway unreachable) — transient")
        return "transient"
    except httpx.ConnectError:
        logger.warning("Personal OpenClaw alert connection error (gateway unreachable) — transient")
        return "transient"
    except httpx.TimeoutException:
        # Write/pool timeout — unusual; treat as a quick transient retry.
        logger.warning("Personal OpenClaw alert timed out (write/pool) — transient")
        return "transient"
    except Exception:
        logger.exception("Failed to send alert to personal OpenClaw (transient)")
        return "transient"

    if resp.status_code == 200:
        logger.info("Health alert delivered to personal OpenClaw")
        return "delivered"
    if resp.status_code in (301, 302, 303, 307, 308):
        # A 3xx to the gateway means Cloudflare Access bounced us to its login page:
        # the CF-Access service token (CF_ACCESS_CLIENT_ID/SECRET) is missing/stale,
        # or the CF-Access app has no Service-Auth policy admitting it. Retrying every
        # tick won't help — fix the token/policy in Cloudflare Zero Trust.
        logger.error(
            "Personal OpenClaw alert redirected (HTTP %d) — Cloudflare Access rejected "
            "the service token; alert NOT delivered. Check CF_ACCESS_CLIENT_ID/SECRET "
            "and the CF-Access Service-Auth policy for the admin gateway.",
            resp.status_code,
        )
        return "undeliverable"
    if 400 <= resp.status_code < 500:
        logger.error("Personal OpenClaw alert rejected (HTTP %d): %s", resp.status_code, resp.text[:200])
        return "undeliverable"
    if resp.status_code >= 500:
        # 5xx (incl. Cloudflare 520-526) usually means the gateway origin is
        # cold/waking behind CF — operationally the same as a slow read, so back
        # off ~10 min rather than re-POSTing every 5-min tick (the storm fix for
        # the real-outage path, where a cold gateway answers 5xx before read=75).
        logger.warning(
            "Personal OpenClaw returned %d (gateway slow/cold) — backing off: %s", resp.status_code, resp.text[:200]
        )
        return "timeout"
    logger.warning("Personal OpenClaw returned %d (transient): %s", resp.status_code, resp.text[:200])
    return "transient"


@csrf_exempt
@require_POST
def run_health_check(request):
    """Run health checks on all active tenants and alert on failures.

    URL: /api/cron/run-health-check/
    Auth: QStash signature or X-Deploy-Secret header.

    Sends alerts to MJ's personal OpenClaw agent (via Cloudflare tunnel)
    when any tenant has a gateway failure. Config drift is informational
    only. Alerts are rate-limited to one per 30 minutes.
    """
    if not verify_qstash_signature(request):
        deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
        provided = request.headers.get("X-Deploy-Secret", "")
        if not (provided and deploy_secret and provided == deploy_secret):
            return JsonResponse({"error": "Unauthorized"}, status=401)

    from django.core.cache import cache

    from apps.orchestrator.services import check_all_tenants_health

    results = check_all_tenants_health()
    unhealthy = [r for r in results if not r["healthy"]]

    summary = {
        "total": len(results),
        "healthy": len(results) - len(unhealthy),
        "unhealthy": len(unhealthy),
    }

    if unhealthy:
        # Rate-limit alerts — skip if we already alerted recently
        cache_key = "health_alert_sent"
        already_alerted = cache.get(cache_key)

        if not already_alerted:
            lines = [f"NBHD Health Alert — {len(unhealthy)}/{len(results)} tenant(s) unhealthy:"]
            for r in unhealthy[:10]:
                name = r.get("display_name", "?")
                container = r.get("container", "?")
                checks = r.get("checks", {})
                failed = [k for k, v in checks.items() if not v.get("ok")]
                detail = ", ".join(failed) if failed else r.get("error", "unknown")
                lines.append(f"  - {name} ({container}): {detail}")
            if len(unhealthy) > 10:
                lines.append(f"  ... and {len(unhealthy) - 10} more")

            status = _send_alert_to_personal_openclaw("\n".join(lines))
            # Cooldown policy:
            #   delivered / undeliverable -> full 30-min cooldown (it landed, or
            #     retrying won't fix it: missing config / CF-Access 302 / 4xx).
            #   timeout -> short 10-min backoff: the gateway was slow/cold (read
            #     timeout or 5xx). Don't hammer every 5-min tick, but retry soon so
            #     the alert lands once it warms. THIS is the storm fix.
            #   transient -> no cooldown: a fast connect-side failure / odd error;
            #     the next tick retries immediately against a hopefully-up gateway.
            if status in ("delivered", "undeliverable"):
                cache.set(cache_key, True, _HEALTH_ALERT_COOLDOWN_SECONDS)
            elif status == "timeout":
                cache.set(cache_key, True, _HEALTH_ALERT_TIMEOUT_COOLDOWN_SECONDS)
            summary["alerted"] = status == "delivered"
            summary["alert_status"] = status
        else:
            logger.info("Health check: %d unhealthy, alert suppressed (cooldown)", len(unhealthy))
            summary["alerted"] = False
            summary["cooldown"] = True

        summary["details"] = unhealthy

    return JsonResponse(summary)


@csrf_exempt
def admin_health_status(request):
    """On-demand tenant health query for admin / personal agent.

    URL: /api/v1/cron/admin-health/
    Auth: X-Deploy-Secret header.
    Methods: GET or POST (both return the same data).

    Unlike run-health-check, this endpoint does NOT send alerts. It's a pull
    mechanism for MJ's personal OpenClaw to query status on demand
    (e.g. "how's NBHD?").

    Returns JSON summary + per-tenant details so the agent can format
    a natural-language response.
    """
    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not deploy_secret or not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.orchestrator.services import check_all_tenants_health

    results = check_all_tenants_health()
    unhealthy = [r for r in results if not r["healthy"]]
    with_drift = [r for r in results if r.get("config_drift")]
    # Hibernated tenants are healthy=True (asleep on purpose, not a fault) so they
    # are excluded from `unhealthy`; break them out so the on-demand answer can say
    # "N healthy (M asleep)" instead of conflating asleep with serving.
    hibernated = [r for r in results if r.get("hibernated")]

    return JsonResponse(
        {
            "total": len(results),
            "healthy": len(results) - len(unhealthy),
            "unhealthy": len(unhealthy),
            "hibernated": len(hibernated),
            "config_drift": len(with_drift),
            "results": results,
        }
    )


@csrf_exempt
@require_POST
def rollout_byo_image_bump(request):
    """One-shot: bump every active tenant to the current OpenClaw image.

    URL: /api/cron/rollout-byo-image-bump/
    Auth: X-Deploy-Secret header.

    Wraps the ``bump_all_tenant_images`` management command. Intended to
    be called manually (or by a workflow_dispatch CI run) after PR #434
    ships, then never again — the routine ``apply_pending_configs`` cron
    handles future image rollouts via the per-message bump path.

    POST body (optional):
      ``{"include_hibernated": true}`` to also bump hibernated tenants.

    Returns JSON: ``{"succeeded": N, "failed": N, "skipped_idempotent": N}``.
    """
    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    if not deploy_secret:
        return JsonResponse({"error": "DEPLOY_SECRET not configured"}, status=503)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not provided or provided != deploy_secret:
        logger.warning("Unauthorized rollout_byo_image_bump attempt")
        return JsonResponse({"error": "Unauthorized"}, status=401)

    body = {}
    if request.body:
        try:
            body = json.loads(request.body)
        except Exception:
            body = {}
    include_hibernated = bool(body.get("include_hibernated", False))

    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    err = StringIO()
    args = []
    if include_hibernated:
        args.append("--include-hibernated")

    try:
        call_command("bump_all_tenant_images", *args, stdout=out, stderr=err)
        return JsonResponse(
            {
                "ok": True,
                "include_hibernated": include_hibernated,
                "stdout_tail": out.getvalue()[-2000:],
            }
        )
    except Exception as exc:
        logger.exception("rollout_byo_image_bump failed")
        return JsonResponse(
            {
                "ok": False,
                "error": str(exc),
                "stdout_tail": out.getvalue()[-2000:],
                "stderr_tail": err.getvalue()[-2000:],
            },
            status=500,
        )


@csrf_exempt
@require_POST
def rollout_byo_persona_refresh(request):
    """One-shot: re-render workspace/AGENTS.md to every active tenant.

    URL: /api/cron/rollout-byo-persona-refresh/
    Auth: X-Deploy-Secret header.

    Wraps the ``refresh_persona_agents_md`` management command. Same
    one-shot semantics as ``rollout_byo_image_bump`` — manual op, not a
    deploy-time hook.

    POST body (optional):
      ``{"include_hibernated": true}``.
    """
    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    if not deploy_secret:
        return JsonResponse({"error": "DEPLOY_SECRET not configured"}, status=503)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not provided or provided != deploy_secret:
        logger.warning("Unauthorized rollout_byo_persona_refresh attempt")
        return JsonResponse({"error": "Unauthorized"}, status=401)

    body = {}
    if request.body:
        try:
            body = json.loads(request.body)
        except Exception:
            body = {}
    include_hibernated = bool(body.get("include_hibernated", False))

    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    err = StringIO()
    args = []
    if include_hibernated:
        args.append("--include-hibernated")

    try:
        call_command("refresh_persona_agents_md", *args, stdout=out, stderr=err)
        return JsonResponse(
            {
                "ok": True,
                "include_hibernated": include_hibernated,
                "stdout_tail": out.getvalue()[-2000:],
            }
        )
    except Exception as exc:
        logger.exception("rollout_byo_persona_refresh failed")
        return JsonResponse(
            {
                "ok": False,
                "error": str(exc),
                "stdout_tail": out.getvalue()[-2000:],
                "stderr_tail": err.getvalue()[-2000:],
            },
            status=500,
        )


# Per-tenant bump tasks for atomic fleet rollout. The endpoint below fans
# out one task per eligible tenant via QStash so each runs in its own
# Django worker invocation, well within the 300s gunicorn budget. Sized
# to handle arbitrary fleet sizes (sequential `bump_openclaw_version --all`
# inside a request handler would time out for fleets >10 tenants).
_ATOMIC_BUMP_LOCK_KEY = "rollout_atomic_bump:in_flight"
# This lock only guards the *enqueue* (the eligibility query + publish_batch
# fan-out below), which completes in milliseconds — it is released in the
# finally block as soon as the tasks are queued, NOT held for the duration of
# the rollout. Double-bumps of the same tenant are prevented at the task layer
# instead: bump_openclaw_atomic_per_tenant re-checks version+image_tag at
# execution time (apps/orchestrator/tasks.py) and bump_openclaw_version_for_tenant
# snapshots-before-write / restores-on-failure, so a repeated write is idempotent.
# A 5-minute TTL is ample headroom for the enqueue path while still expiring a
# lock orphaned by a worker death mid-fan-out.
_ATOMIC_BUMP_LOCK_TTL_SECONDS = 60 * 5


@csrf_exempt
@require_POST
def rollout_atomic_bump(request):
    """Atomic fleet bump: fan out per-tenant config + image + version updates.

    URL: /api/cron/rollout-atomic-bump/
    Auth: X-Deploy-Secret header.

    Unlike ``rollout-byo-image-bump`` (which only updates the image and
    relies on the ``apply_pending_configs`` cron to lazily refresh
    configs), this endpoint enqueues a per-tenant QStash task that
    atomically updates the version field, openclaw.json on the file
    share, and the container image — required when a release crosses an
    OpenClaw config schema boundary.

    POST body (all optional):
        {
            "oc_version": "2026.5.7",   // default: settings.OPENCLAW_CURRENT_VERSION
            "image_tag":  "<sha>",       // default: settings.OPENCLAW_IMAGE_TAG
            "tenant_id":  "<uuid>",      // optional canary mode — bump only this tenant
            "dry_run":    false
        }

    Returns:
        {"queued": N, "oc_version": "...", "image_tag": "...",
         "tenant_ids": [...], "dry_run": bool}

    Each per-tenant task uses ``bump_openclaw_version_for_tenant`` (see
    apps/orchestrator/services.py) which provides:
      - File-share snapshot before write + best-effort restore on
        image-push failure (closes the boot-failure window for partial
        failures across schema crossings).
      - DB rollback of ``tenant.openclaw_version`` on any exception.
      - Idempotency on the per-tenant level (version+image_tag check).

    Concurrency-locked via Django cache so two operators can't kick off
    simultaneous fleet rollouts.
    """
    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    if not deploy_secret:
        return JsonResponse({"error": "DEPLOY_SECRET not configured"}, status=503)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not provided or provided != deploy_secret:
        logger.warning("Unauthorized rollout_atomic_bump attempt")
        return JsonResponse({"error": "Unauthorized"}, status=401)

    body: dict = {}
    if request.body:
        try:
            body = json.loads(request.body)
        except Exception:
            body = {}

    from apps.orchestrator.tool_policy import OPENCLAW_CURRENT_VERSION

    oc_version = str(body.get("oc_version") or OPENCLAW_CURRENT_VERSION).strip()
    image_tag = str(body.get("image_tag") or getattr(settings, "OPENCLAW_IMAGE_TAG", "") or "").strip()
    tenant_filter = str(body.get("tenant_id") or "").strip()
    dry_run = bool(body.get("dry_run", False))

    if not image_tag or image_tag == "latest":
        return JsonResponse(
            {
                "error": (
                    "Refusing to roll out the 'latest' tag — pass image_tag explicitly "
                    "or set OPENCLAW_IMAGE_TAG. The endpoint can't compute idempotence otherwise."
                )
            },
            status=400,
        )

    # Concurrency lock — Django cache. Best-effort; expires automatically.
    from django.core.cache import cache

    if not dry_run and cache.add(_ATOMIC_BUMP_LOCK_KEY, "1", timeout=_ATOMIC_BUMP_LOCK_TTL_SECONDS):
        lock_acquired = True
    elif dry_run:
        lock_acquired = False  # Don't acquire lock for dry-run
    else:
        return JsonResponse(
            {"error": "Another atomic bump is in flight; try again in a few minutes"},
            status=409,
        )

    try:
        eligible = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
        )
        if tenant_filter:
            eligible = eligible.filter(id=tenant_filter)
        # Idempotency: skip tenants already at target on BOTH version + image_tag.
        # A version-only match isn't enough (a prior partial failure could leave
        # version=target but image_tag stale).
        eligible = eligible.exclude(openclaw_version=oc_version, container_image_tag=image_tag)

        tenant_ids = [str(t.id) for t in eligible]

        if dry_run or not tenant_ids:
            return JsonResponse(
                {
                    "queued": 0,
                    "oc_version": oc_version,
                    "image_tag": image_tag,
                    "tenant_ids": tenant_ids,
                    "dry_run": dry_run,
                }
            )

        from apps.cron.publish import publish_batch

        tasks = [("bump_openclaw_atomic_per_tenant", (tid, oc_version, image_tag), {}) for tid in tenant_ids]
        queued = publish_batch(tasks)
        logger.info(
            "rollout_atomic_bump queued %d task(s) -> oc_version=%s image_tag=%s",
            queued,
            oc_version,
            image_tag,
        )
        return JsonResponse(
            {
                "queued": queued,
                "oc_version": oc_version,
                "image_tag": image_tag,
                "tenant_ids": tenant_ids,
                "dry_run": False,
            }
        )
    finally:
        if lock_acquired:
            cache.delete(_ATOMIC_BUMP_LOCK_KEY)


def atomic_bump_status(request):
    """Report current openclaw_version + container_image_tag per active tenant.

    URL: /api/cron/atomic-bump-status/
    Auth: X-Deploy-Secret header.

    Read-only post-rollout audit endpoint. The caller filters the response
    against their target values to find tenants where the rollout didn't
    take (drift signal). Server-side filtering would need to know the
    target — which the caller already has.

    Returns:
        {
            "tenants": [{tenant_id, oc_version, image_tag, hibernated}, ...],
            "count":   N
        }
    """
    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    if not deploy_secret:
        return JsonResponse({"error": "DEPLOY_SECRET not configured"}, status=503)
    provided = request.headers.get("X-Deploy-Secret", "")
    if not provided or provided != deploy_secret:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    rows = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
    ).values("id", "openclaw_version", "container_image_tag", "hibernated_at")

    tenants = [
        {
            "tenant_id": str(row["id"]),
            "oc_version": row["openclaw_version"] or "",
            "image_tag": row["container_image_tag"] or "",
            "hibernated": row["hibernated_at"] is not None,
        }
        for row in rows
    ]
    return JsonResponse({"tenants": tenants, "count": len(tenants)})
