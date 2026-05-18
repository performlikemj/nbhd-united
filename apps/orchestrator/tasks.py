"""Tasks for async provisioning/deprovisioning (executed via QStash)."""

import time
from datetime import UTC

import httpx

from .services import (
    bump_openclaw_version_for_tenant,
    deprovision_tenant,
    provision_tenant,
    repair_stale_tenant_provisioning,
    seed_cron_jobs,
    update_tenant_config,
)


def provision_tenant_task(tenant_id: str) -> None:
    """Provision an OpenClaw instance for a tenant."""
    provision_tenant(tenant_id)


def deprovision_tenant_task(tenant_id: str) -> None:
    """Deprovision a tenant's OpenClaw instance."""
    deprovision_tenant(tenant_id)


def update_tenant_config_task(tenant_id: str) -> None:
    """Update an active tenant's OpenClaw container config."""
    update_tenant_config(tenant_id)


def bump_openclaw_atomic_per_tenant_task(tenant_id: str, target_version: str, image_tag: str) -> None:
    """Atomic per-tenant OC version bump (config + image + DB) for fleet rollout.

    QStash fan-out target for the rollout-atomic-bump endpoint. Each task
    runs in <30s wall-time so it stays well within the gunicorn 300s
    request budget — unlike a sequential management command call which
    would time out for any fleet >10 tenants.

    Idempotent: skips if tenant is already at target_version (the most
    likely cause is a duplicate QStash delivery from retry).
    """
    from django.conf import settings

    from apps.tenants.models import Tenant

    try:
        tenant = Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        return  # Tenant deprovisioned between endpoint and task — no-op.

    if tenant.openclaw_version == target_version and tenant.container_image_tag == image_tag:
        return  # Already at target; QStash retry or stale enqueue.

    if tenant.status != Tenant.Status.ACTIVE or not tenant.container_id:
        return  # Suspended / deprovisioning — out of scope.

    registry = getattr(settings, "AZURE_ACR_SERVER", "nbhdunited.azurecr.io")
    bump_openclaw_version_for_tenant(tenant, target_version, image_tag, registry)


def seed_cron_jobs_task(tenant_id: str) -> None:
    """Seed cron job definitions into a tenant's running OpenClaw container."""
    seed_cron_jobs(tenant_id)


def repair_stale_tenant_provisioning_task(limit: int = 50) -> dict:
    """Repair stale tenant provisioning states in a safe, idempotent sweep."""
    return repair_stale_tenant_provisioning(limit=limit, dry_run=False)


def hibernate_suspended_task() -> dict:
    """Hibernate all suspended tenant containers (deactivate revisions)."""
    from apps.orchestrator.azure_client import hibernate_container_app
    from apps.tenants.models import Tenant

    tenants = Tenant.objects.filter(
        status=Tenant.Status.SUSPENDED,
    ).exclude(container_id="")

    hibernated = 0
    failed = 0
    for tenant in tenants:
        try:
            hibernate_container_app(tenant.container_id)
            hibernated += 1
        except Exception:
            failed += 1

    return {"hibernated": hibernated, "failed": failed, "total": tenants.count()}


def resume_tenant_crons_task(tenant_id: str) -> dict:
    """Re-enable a reactivated tenant's crons after the container has woken.

    Enqueued (delayed ~30s) by ``handle_checkout_completed`` so the freshly
    woken container's gateway has time to boot before we hit
    ``cron.list`` / ``cron.update``. QStash retries handle any residual
    cold-start. Running this synchronously from the Stripe webhook was the
    original shape of issue #540 — the gateway is reliably not ready
    inside the webhook's ~10s budget.

    Idempotent: ``resume_tenant_crons`` only flips crons where
    ``enabled=False`` to ``True``, so duplicate QStash deliveries
    (signature retries, reactivation re-fire) are no-ops on the second
    call.
    """
    import logging

    from apps.cron.suspension import resume_tenant_crons
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant:
        return {"enabled": 0, "already_enabled": 0, "errors": 0, "job_names": []}
    if not tenant.container_fqdn:
        return {"enabled": 0, "already_enabled": 0, "errors": 0, "job_names": []}

    result = resume_tenant_crons(tenant)
    logger.info(
        "resume_tenant_crons_task: tenant %s — enabled=%d already_enabled=%d errors=%d",
        str(tenant_id)[:8],
        result.get("enabled", 0),
        result.get("already_enabled", 0),
        result.get("errors", 0),
    )
    return result


def apply_single_tenant_config_task(tenant_id: str, _is_followup_retry: bool = False) -> None:
    """Apply pending config for a single tenant (enqueued by apply-pending-configs).

    Two-stage flow:
      1. Regenerate ``openclaw.json`` and write it to the file share.
      2. Advance ``config_version`` and stamp ``applied_model``. The
         file write IS the apply — the running OpenClaw process picks
         the new config up on its next restart. Image swaps already
         restart; config-only changes (model swap, cron prompt edits)
         take effect on the next container warmup. Hibernated tenants
         pick up the file at wake.

    Why no live hot-reload: OpenClaw 2026.5.7 only exposes config-mutation
    actions (``config.apply`` / ``config.patch`` / ``restart``) through
    its agent-scoped ``/tools/invoke`` registry, which strips the
    ``gateway`` tool when ``tools.deny`` contains it (the policy pipeline
    is shared between agent context and HTTP). The legacy
    ``gateway.reload`` action does not exist in 2026.5.7 — calls 404'd
    fleet-wide and would have raised ``Unknown action: reload`` even if
    the registry passed them through. See issue #541.

    The ``_is_followup_retry`` arg is kept for compatibility with any
    in-flight QStash deliveries enqueued by the previous reload-loop
    code path; it is accepted but no longer drives any retry behavior.
    """
    import logging

    from django.db import models as db_models
    from django.utils import timezone as tz

    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant:
        return

    if tenant.config_version >= tenant.pending_config_version:
        return

    try:
        update_tenant_config(tenant_id)
    except Exception:
        logger.exception("apply_single_tenant_config failed for %s", tenant_id)
        return

    tenant.refresh_from_db()

    Tenant.objects.filter(id=tenant_id).update(
        config_version=db_models.F("pending_config_version"),
        config_refreshed_at=tz.now(),
        applied_model=tenant.preferred_model,
        applied_model_at=tz.now(),
    )
    if tenant.hibernated_at:
        logger.info(
            "Config written for hibernated tenant %s — wake will pick it up",
            str(tenant_id)[:8],
        )
    else:
        logger.info(
            "Config written for tenant %s — next container restart picks it up",
            str(tenant_id)[:8],
        )


def apply_single_tenant_image_task(tenant_id: str, desired_tag: str) -> None:
    """Update a single tenant's container image (enqueued by apply-pending-configs).

    Three-phase flow to prevent cron loss on restart:
      1. Snapshot current crons from the running container → PostgreSQL
      2. Update the container image (triggers restart, wipes SQLite)
      3. Schedule a delayed restore from the snapshot
    """
    import logging

    from django.conf import settings as django_settings
    from django.utils import timezone

    from apps.orchestrator.azure_client import update_container_image
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
    if not tenant or not tenant.container_id:
        return

    if tenant.hibernated_at:
        logger.info("Skipping image update for hibernated tenant %s", tenant_id[:8])
        return

    # Phase 1: Snapshot current cron state before the restart wipes SQLite.
    try:
        from apps.cron.gateway_client import invoke_gateway_tool
        from apps.orchestrator.services import _extract_cron_jobs

        result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        jobs = _extract_cron_jobs(result)
        if jobs is not None:
            Tenant.objects.filter(id=tenant_id).update(
                cron_jobs_snapshot={
                    "jobs": jobs,
                    "snapshot_at": timezone.now().isoformat(),
                    "trigger": "pre-image-update",
                    "image_tag": desired_tag,
                },
            )
            logger.info(
                "Pre-image cron snapshot saved for tenant %s (%d jobs)",
                tenant_id[:8],
                len(jobs),
            )
    except Exception:
        logger.warning(
            "Pre-image cron snapshot failed for tenant %s (proceeding — will fall back to seed)",
            tenant_id[:8],
            exc_info=True,
        )

    # Phase 2: Update the container image.
    desired_image = f"{django_settings.AZURE_ACR_SERVER}/nbhd-openclaw:{desired_tag}"
    try:
        update_container_image(tenant.container_id, desired_image)
        Tenant.objects.filter(id=tenant_id).update(
            container_image_tag=desired_tag,
        )
    except Exception:
        logger.exception("apply_single_tenant_image failed for %s", tenant_id)
        return

    # Phase 3: Schedule post-restart cron restore (90s for container startup).
    from apps.cron.publish import publish_task as publish_qstash_task

    try:
        publish_qstash_task(
            "restore_crons_after_image_update",
            tenant_id,
            delay_seconds=90,
            idempotency_key=f"post-image-restore-{tenant_id}-{desired_tag}",
        )
        logger.info("Scheduled post-image cron restore for tenant %s (90s delay)", tenant_id[:8])
    except Exception:
        logger.warning(
            "Failed to schedule post-image cron restore for %s (non-fatal)",
            tenant_id[:8],
            exc_info=True,
        )


# Cap on per-restore missed-fire triggers. A 24h gap on hourly crons would
# otherwise stampede the gateway; users want the latest of each cron, not
# every back-fire, so we dedupe to "latest missed fire per name" and then
# cap total. 5 covers multiple distinct crons (morning briefing + heartbeat
# + personal question + …) without flooding.
_MAX_MISSED_FIRES_PER_RESTORE = 5


def _compute_missed_cron_fires(
    snapshot_jobs: list[dict],
    snapshot_at,
    now,
) -> dict:
    """For each enabled cron-kind job, return {name: latest_missed_fire_dt}.

    A fire is "missed" if its scheduled time falls in (snapshot_at, now].
    We keep only the latest per cron — users want the most recent morning
    briefing, not 24 of them. Skips ``at`` / ``every`` kinds: ``at`` is
    one-shot (the kind-"at" wake sweep covers it separately — see PR #513)
    and ``every`` lacks the fixed-time semantics that make "missed fire"
    meaningful. Skips agent-managed prefixes (``_sync:``, ``_fuel:``) —
    those are one-shots whose schedule expressions encode specific dates;
    re-firing them after the originating session is over would be wrong.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        from croniter import croniter
    except ImportError:
        return {}

    missed: dict[str, datetime] = {}

    for job in snapshot_jobs:
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        name = job.get("name", "")
        if not name or name.startswith(("_sync:", "_fuel:")):
            continue
        schedule = job.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("kind") != "cron":
            continue
        expr = schedule.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            continue
        tz_name = schedule.get("tz") or "UTC"
        try:
            zone = ZoneInfo(tz_name)
        except Exception:
            continue
        try:
            anchor = snapshot_at.astimezone(zone)
            ceiling = now.astimezone(zone)
            cron = croniter(expr, anchor)
            latest = None
            # croniter has no built-in "latest fire before X"; iterate
            # forward and remember the last fire that lands inside the
            # window. Bounded by the window's actual fire density.
            while True:
                next_dt = cron.get_next(datetime)
                if next_dt > ceiling:
                    break
                if next_dt > anchor:
                    latest = next_dt
        except Exception:
            continue
        if latest is not None:
            missed[name] = latest

    return missed


def _fire_missed_crons_after_restore(*, tenant, snapshot: dict, snapshot_jobs: list[dict]) -> int:
    """Re-fire cron-kind jobs that should have run between snapshot and now.

    OpenClaw 5.7's startup catch-up (``planStartupCatchup`` in
    ``server-cron-*.js``) only fires missed crons when ``lastRunAtMs`` is
    set on the job (via ``allowCronMissedRunByLastRun``). The pre-image
    snapshot strips the entire ``state`` block (see ``_STRIP_FIELDS`` in
    ``restore_crons_after_image_update_task``) so the restored job has no
    ``lastRunAtMs`` — catch-up falls through and the missed fire is
    silently dropped. We compute "what should have fired" from each cron
    expression + ``snapshot_at`` and call ``cron.run`` for the latest one
    per cron, capped to avoid gateway flood.

    See ``project_openclaw_cron_payload_shape.md`` and the 2026-05-11
    evening-check-in incident (cron registered, container awake 59 min,
    but the agent turn never fired because lastRunAtMs was missing).

    Returns the number of missed fires actually triggered.
    """
    import logging
    from datetime import datetime

    from django.utils import timezone as django_tz

    logger = logging.getLogger(__name__)
    tid = str(tenant.id)[:8]

    snapshot_at_str = snapshot.get("snapshot_at")
    if not snapshot_at_str or not snapshot_jobs:
        return 0

    try:
        snapshot_at = datetime.fromisoformat(snapshot_at_str)
    except (ValueError, TypeError):
        logger.warning("Missed-cron catch-up: invalid snapshot_at %r for tenant %s", snapshot_at_str, tid)
        return 0

    if snapshot_at.tzinfo is None:
        # Legacy snapshots may have stored naive ISO strings; interpret as
        # UTC since that's what ``timezone.now().isoformat()`` produces today.

        snapshot_at = snapshot_at.replace(tzinfo=UTC)

    now = django_tz.now()
    if snapshot_at >= now:
        return 0

    missed = _compute_missed_cron_fires(snapshot_jobs, snapshot_at, now)
    if not missed:
        return 0

    # Map name → fresh gateway job id (restore generated new UUIDs).
    # Local re-import keeps test mocks of ``apps.cron.gateway_client.invoke_gateway_tool``
    # working (the patch hits the source module; module-level aliases bind at import time
    # and would shadow the mock).
    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
    from apps.orchestrator.services import _extract_cron_jobs

    try:
        result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        current_jobs = _extract_cron_jobs(result) or []
    except GatewayError:
        logger.warning(
            "Missed-cron catch-up: cron.list failed for tenant %s — skipping",
            tid,
            exc_info=True,
        )
        return 0

    name_to_id: dict[str, str] = {}
    for j in current_jobs:
        if not isinstance(j, dict):
            continue
        name = j.get("name", "")
        job_id = j.get("id") or j.get("jobId", "")
        if name and job_id and name not in name_to_id:
            name_to_id[name] = job_id

    fired = 0
    for name, fire_time in list(missed.items())[:_MAX_MISSED_FIRES_PER_RESTORE]:
        job_id = name_to_id.get(name)
        if not job_id:
            logger.warning(
                "Missed-cron catch-up: '%s' not in gateway after restore (tenant %s) — skipping",
                name,
                tid,
            )
            continue
        try:
            invoke_gateway_tool(tenant, "cron.run", {"jobId": job_id})
            fired += 1
            logger.info(
                "Missed-cron catch-up: fired '%s' for tenant %s (was due %s)",
                name,
                tid,
                fire_time.isoformat(),
            )
        except GatewayError:
            logger.warning(
                "Missed-cron catch-up: cron.run failed for '%s' on tenant %s",
                name,
                tid,
                exc_info=True,
            )

    if len(missed) > _MAX_MISSED_FIRES_PER_RESTORE:
        logger.info(
            "Missed-cron catch-up: capped at %d of %d for tenant %s (older missed fires dropped)",
            _MAX_MISSED_FIRES_PER_RESTORE,
            len(missed),
            tid,
        )

    return fired


def restore_crons_after_image_update_task(tenant_id: str) -> None:
    """Restore cron jobs from snapshot after a container image update.

    Called ~90s after apply_single_tenant_image_task to give the container
    time to start. Restores every job from the pre-image snapshot with full
    fidelity. Falls back to seed_cron_jobs if the snapshot is missing.
    """
    import logging

    # Deferred import (re-importing names also available at module level) so
    # tests that patch ``apps.cron.gateway_client.invoke_gateway_tool`` /
    # ``GatewayError`` keep working — the local rebind happens at call time
    # and picks up the patched object.
    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool  # noqa: F811
    from apps.orchestrator.config_generator import build_cron_seed_jobs
    from apps.orchestrator.services import _extract_cron_jobs, seed_cron_jobs
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
    if not tenant or not tenant.container_id:
        return

    snapshot = getattr(tenant, "cron_jobs_snapshot", None)
    if not snapshot or not isinstance(snapshot, dict) or not snapshot.get("jobs"):
        logger.warning(
            "No cron snapshot for tenant %s — falling back to seed_cron_jobs",
            tenant_id[:8],
        )
        seed_cron_jobs(tenant)
        return

    snapshot_jobs = snapshot["jobs"]

    # Check what's already on the container (may not be empty if the container
    # restored some jobs from its own persistence layer).
    try:
        result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        existing_jobs = _extract_cron_jobs(result) or []
    except GatewayError:
        logger.warning(
            "cron.list failed for tenant %s — container may still be starting, falling back to seed",
            tenant_id[:8],
            exc_info=True,
        )
        seed_cron_jobs(tenant)
        return

    existing_names = {j.get("name", "").lower() for j in existing_jobs if isinstance(j, dict)}

    # System crons live in postgres (CronJob rows) for postgres-canonical
    # tenants. We rely on the signal-driven ``regenerate_tenant_crons`` task
    # (enqueued at the end of this function) to push them to the gateway
    # with current seed shape — restoring them from the snapshot here would
    # re-propagate whatever stale payload OC had at snapshot time (the
    # 2026-05-13 canary incident: snapshot captured stale ``anthropic-cli/...``
    # model on Morning Briefing and we kept pushing it back across image swaps).
    system_cron_names = {j["name"].lower() for j in build_cron_seed_jobs(tenant) if j.get("name")}

    # Gateway-internal fields that cron.add rejects
    _STRIP_FIELDS = {"id", "jobId", "createdAt", "state", "createdAtMs", "updatedAtMs", "nextRunAtMs", "runningAtMs"}

    # Deduplicate within snapshot (dirty snapshots may have duplicate names).
    # Skip system crons — postgres is canonical for those.
    seen_names: set[str] = set()
    jobs_to_restore: list[dict] = []
    for job in snapshot_jobs:
        if not isinstance(job, dict) or not job.get("name"):
            continue
        lower_name = job["name"].lower()
        if lower_name in system_cron_names:
            continue
        if lower_name in seen_names or lower_name in existing_names:
            continue
        seen_names.add(lower_name)
        jobs_to_restore.append(job)

    restored = 0
    errors = 0
    for job in jobs_to_restore:
        clean_job = {k: v for k, v in job.items() if k not in _STRIP_FIELDS}
        try:
            invoke_gateway_tool(tenant, "cron.add", {"job": clean_job})
            restored += 1
        except GatewayError as exc:
            logger.warning(
                "Failed to restore cron '%s' for tenant %s: %s",
                job.get("name"),
                tenant_id[:8],
                exc,
            )
            errors += 1

    # System cron convergence: explicitly enqueue a reconcile so the new
    # container picks up the postgres-canonical state right away. Without
    # this, system crons would fire from whatever OC loaded from jobs.json
    # on the share until the next signal-driven sweep — a 22:00 UTC
    # morning briefing could fire stale during the post-boot window.
    # Idempotency key dedup'd with the signal-driven reconcile that fires
    # at the next CronJob.save.
    if getattr(tenant, "postgres_cron_canonical", False):
        try:
            from apps.cron.publish import publish_task

            publish_task(
                "regenerate_tenant_crons",
                tenant_id,
                idempotency_key=f"regen-cron-post-image-{tenant_id}",
                delay_seconds=5,
            )
        except Exception:
            logger.warning(
                "Failed to enqueue post-image reconcile for %s (non-fatal)",
                tenant_id[:8],
                exc_info=True,
            )

    # Re-fire any cron-kind jobs that should have run while the container
    # was down between snapshot capture and now. The pre-image snapshot
    # strips ``state`` (including ``lastRunAtMs``), so OpenClaw 5.7's
    # startup catch-up — gated by ``lastRunAtMs`` via ``allowCronMissedRunByLastRun``
    # in ``planStartupCatchup`` — silently skips the missed fire. Closing the
    # gap manually here is the smallest correct fix. See
    # ``project_openclaw_cron_payload_shape.md`` and the 2026-05-11 evening
    # check-in incident: cron registered in runtime, container awake 59 min,
    # but the agent turn never fired because the missed window was dropped.
    missed_fired = _fire_missed_crons_after_restore(
        tenant=tenant,
        snapshot=snapshot,
        snapshot_jobs=snapshot_jobs,
    )

    # If the tenant is on the new per-session Fuel flow, snapshot didn't
    # capture _fuel:* (the restore step deliberately filters them by prefix),
    # so we regenerate them here from Postgres truth.
    profile = getattr(tenant, "fuel_profile", None)
    if profile and profile.use_session_scheduling:
        try:
            from apps.orchestrator.fuel_cron import regenerate_fuel_crons

            regenerate_fuel_crons(tenant)
        except Exception:
            logger.warning(
                "Post-image Fuel cron regen failed for %s (non-fatal)",
                tenant_id[:8],
                exc_info=True,
            )

    logger.info(
        "Post-image cron restore for tenant %s: %d restored, %d errors, %d already present, "
        "%d missed-fires triggered (snapshot had %d)",
        tenant_id[:8],
        restored,
        errors,
        len(existing_names),
        missed_fired,
        len(snapshot_jobs),
    )


def broadcast_single_tenant_task(tenant_id: str, message: str) -> None:
    """Send a one-off agent-driven message to a single tenant's user.

    Posts directly to the container's /v1/chat/completions endpoint —
    the same path used by the Telegram poller and LINE webhook.
    The agent processes the prompt and messages the user via
    nbhd_send_to_user.
    """
    import logging

    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
    if not tenant or not tenant.container_fqdn:
        return

    if not tenant.has_entitlement or tenant.status != Tenant.Status.ACTIVE:
        logger.info("Broadcast skipped for tenant %s (no entitlement)", tenant_id[:8])
        return

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    from apps.cron.gateway_client import get_gateway_token_for_tenant

    gateway_token = get_gateway_token_for_tenant(tenant)

    user_tz = getattr(tenant.user, "timezone", None) or "UTC"

    try:
        resp = httpx.post(
            url,
            json={
                "model": "openclaw",
                "messages": [{"role": "user", "content": message}],
            },
            headers={
                "Authorization": f"Bearer {gateway_token}",
                "X-User-Timezone": user_tz,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        logger.info("Broadcast sent to tenant %s", tenant_id[:8])
    except httpx.TimeoutException:
        logger.error("Broadcast timeout for tenant %s", tenant_id[:8])
    except httpx.HTTPStatusError as e:
        logger.error(
            "Broadcast failed for tenant %s: %s %s", tenant_id[:8], e.response.status_code, e.response.text[:200]
        )
    except Exception as e:
        logger.error("Broadcast failed for tenant %s: %s", tenant_id[:8], e)


def hibernate_idle_tenants_task() -> dict:
    """Find active tenants idle >2h and hibernate their containers.

    Excludes tenants with a recent ``cron_wake_at`` so this hourly sweep
    doesn't race with ``wake_for_cron_task`` and kill a container right
    when its scheduled cron is about to fire. Without this guard, a
    morning briefing scheduled at the top of the hour was silently killed
    by the same-minute hibernate sweep (canary 2026-05-11 07:00 JST
    incident — wake_for_cron at :56, hibernate-idle at :00 → ManuallyStopped
    at :00:47, briefing never delivered). Cron-wake re-hibernation belongs
    to ``check_cron_wake_idle_task``, which knows about upcoming crons and
    decides correctly. If ``cron_wake_at`` ever gets stuck (QStash drop,
    etc.), the same 2h cutoff still lets us reclaim the container — we
    just defer to the cron-aware path while it's fresh.
    """
    import logging
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from apps.orchestrator.hibernation import hibernate_idle_tenant
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    from django.db import transaction

    cutoff = timezone.now() - timedelta(hours=2)

    hibernated = 0
    failed = 0
    skipped_cron_wake = 0
    skipped_imminent_cron = 0
    with transaction.atomic():
        idle_tenants = (
            Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
                container_id__gt="",
                hibernated_at__isnull=True,
            )
            .filter(Q(last_message_at__lt=cutoff) | Q(last_message_at__isnull=True, provisioned_at__lt=cutoff))
            .filter(Q(cron_wake_at__isnull=True) | Q(cron_wake_at__lt=cutoff))
            .select_for_update(skip_locked=True)
        )

        from apps.orchestrator.hibernation import _cron_active_or_imminent

        for tenant in idle_tenants:
            # Re-check last_message_at + cron_wake_at to avoid TOCTOU race
            # with wake_for_cron_task (which sets cron_wake_at) and inbound
            # user messages.
            tenant.refresh_from_db(fields=["last_message_at", "hibernated_at", "cron_wake_at"])
            if tenant.hibernated_at:
                continue
            if tenant.last_message_at and tenant.last_message_at >= cutoff:
                continue
            if tenant.cron_wake_at and tenant.cron_wake_at >= cutoff:
                # A cron wake landed between the queryset and the refresh
                # — let check_cron_wake_idle handle it.
                skipped_cron_wake += 1
                continue

            # Forward-looking guard: ``cron_wake_at`` only catches tenants
            # we just woke for a cron. Long-running awake tenants miss
            # that signal entirely, so the in-flight + lookahead check
            # below covers the canary 2026-05-12 12:00 case (continuously
            # awake since the morning deploy, killed mid-evening-check-in).
            defer_reason = _cron_active_or_imminent(tenant)
            if defer_reason:
                skipped_imminent_cron += 1
                logger.info(
                    "hibernate_idle_tenants: deferring tenant %s (%s)",
                    str(tenant.id)[:8],
                    defer_reason,
                )
                continue

            if hibernate_idle_tenant(tenant):
                hibernated += 1
            else:
                failed += 1

    logger.info(
        "hibernate_idle_tenants: hibernated=%d failed=%d skipped_cron_wake=%d skipped_imminent_cron=%d",
        hibernated,
        failed,
        skipped_cron_wake,
        skipped_imminent_cron,
    )
    return {
        "hibernated": hibernated,
        "failed": failed,
        "skipped_cron_wake": skipped_cron_wake,
        "skipped_imminent_cron": skipped_imminent_cron,
    }


def nightly_extraction_task() -> dict:
    """Run nightly extraction for all active tenants.

    Iterates every active tenant and calls run_extraction_for_tenant().
    Each tenant is handled independently — one failure doesn't block the rest.
    """
    import logging

    from apps.journal.extraction import run_extraction_for_tenant
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
    ).select_related("user")

    results = []
    for tenant in tenants:
        try:
            result = run_extraction_for_tenant(tenant)
        except Exception:
            logger.exception("nightly_extraction: failed for tenant %s", str(tenant.id)[:8])
            result = {"skipped": "error"}
        results.append({"tenant": str(tenant.id)[:8], **result})

    extracted = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    logger.info("nightly_extraction: total=%d extracted=%d skipped=%d", len(results), extracted, skipped)
    return {"total": len(results), "extracted": extracted, "skipped": skipped}


def nightly_task_propagation_task() -> dict:
    """Propagate checked-off tasks from documents to the tasks document.

    Iterates every active tenant and calls propagate_completions_for_tenant().
    Each tenant is handled independently — one failure doesn't block the rest.
    """
    import logging

    from apps.journal.propagation import propagate_completions_for_tenant
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
    ).select_related("user")

    results = []
    for tenant in tenants:
        try:
            result = propagate_completions_for_tenant(tenant)
        except Exception:
            logger.exception("nightly_task_propagation: failed for tenant %s", str(tenant.id)[:8])
            result = {"skipped": "error"}
        results.append({"tenant": str(tenant.id)[:8], **result})

    propagated = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    logger.info("nightly_task_propagation: total=%d propagated=%d skipped=%d", len(results), propagated, skipped)
    return {"total": len(results), "propagated": propagated, "skipped": skipped}


def dedup_cron_jobs_task(tenant_id: str) -> None:
    """Remove duplicate cron jobs from a tenant's container.

    Groups by name, keeps newest (by createdAt), deletes rest.
    """
    import logging

    from apps.orchestrator.services import dedup_tenant_cron_jobs
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant or not tenant.container_fqdn:
        return

    result = dedup_tenant_cron_jobs(tenant)
    logger.info(
        "dedup_task: tenant %s — kept %d, deleted %d, errors %d",
        tenant_id[:8],
        result["kept"],
        result["deleted"],
        result["errors"],
    )


def regenerate_fuel_crons_task(tenant_id: str) -> dict:
    """Reconcile a tenant's _fuel:* crons with the derived set (Postgres truth).

    Enqueued (debounced 30s) by ``apps/fuel/signals.py`` on Workout writes;
    also runnable on demand or from the hourly reconcile.
    """
    import logging

    from apps.orchestrator.fuel_cron import regenerate_fuel_crons
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
    if not tenant or not tenant.container_fqdn:
        return {"added": 0, "removed": 0, "unchanged": 0, "errors": 0}
    return regenerate_fuel_crons(tenant)


def reconcile_fuel_crons_task() -> dict:
    """Hourly fleet-wide reconcile — catches drift on tenants on the new flow."""
    import logging

    from apps.fuel.models import FuelProfile
    from apps.orchestrator.fuel_cron import regenerate_fuel_crons
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    profiles = FuelProfile.objects.filter(use_session_scheduling=True).select_related("tenant", "tenant__user")
    totals = {"tenants": 0, "added": 0, "removed": 0, "errors": 0}
    for profile in profiles:
        tenant: Tenant = profile.tenant
        if not tenant.container_fqdn:
            continue
        try:
            res = regenerate_fuel_crons(tenant)
            totals["tenants"] += 1
            totals["added"] += res["added"]
            totals["removed"] += res["removed"]
            totals["errors"] += res["errors"]
        except Exception:
            logger.exception("reconcile_fuel_crons: tenant %s failed", tenant.id)
            totals["errors"] += 1
    logger.info("reconcile_fuel_crons: %s", totals)
    return totals


def regenerate_tenant_crons_task(tenant_id: str) -> dict:
    """Reconcile a tenant's managed crons against the Postgres CronJob table.

    Enqueued (debounced 30s) by ``apps/cron/signals.py`` on CronJob writes
    and by the container-start hook; also runnable on demand or from the
    hourly fleet reconcile.

    No-op for tenants where ``postgres_cron_canonical=False``.
    """
    import logging

    from apps.orchestrator.cron_reconcile import regenerate_tenant_crons
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
    if not tenant or not tenant.container_fqdn:
        return {"added": 0, "removed": 0, "unchanged": 0, "errors": 0}
    return regenerate_tenant_crons(tenant)


def reconcile_tenant_crons_task() -> dict:
    """Hourly fleet-wide reconcile for all tenants on the Postgres-canonical flow.

    Catches drift between Postgres ``CronJob`` rows and the container's SQLite
    registry. Skips tenants whose ``postgres_cron_canonical`` flag is False.
    """
    import logging

    from apps.orchestrator.cron_reconcile import regenerate_tenant_crons
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenants = (
        Tenant.objects.filter(postgres_cron_canonical=True, status="active")
        .exclude(container_fqdn="")
        .select_related("user")
    )
    totals = {"tenants": 0, "added": 0, "removed": 0, "errors": 0}
    for tenant in tenants:
        try:
            res = regenerate_tenant_crons(tenant)
            totals["tenants"] += 1
            totals["added"] += res["added"]
            totals["removed"] += res["removed"]
            totals["errors"] += res["errors"]
        except Exception:
            logger.exception("reconcile_tenant_crons: tenant %s failed", tenant.id)
            totals["errors"] += 1
    logger.info("reconcile_tenant_crons: %s", totals)
    return totals


def reconcile_welcomes_task() -> dict:
    """Daily fleet-wide reconcile for missing or stale welcome crons.

    Watchdog backstop for the welcome flow. Walks active tenants and,
    for each enabled feature (Fuel, Gravity), re-invokes the scheduler.
    The schedulers are self-healing: they replace stale one-shots whose
    fire date already passed without successful self-removal, skip
    tenants whose welcome was confirmed delivered, and surface
    transport failures as a "failed" tally.

    The original Phase 1 design relied on the deploy-time backfill plus
    the live toggle path to deliver welcomes, with no recovery path
    when both failed (e.g. agent crashed mid-turn during the original
    fire). This task closes that gap — a tenant whose welcome got
    orphaned will be re-queued within 24h.
    """
    import logging
    from collections import Counter

    from apps.orchestrator.welcome_scheduler import WelcomeStatus
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenants = list(Tenant.objects.select_related("user").filter(status=Tenant.Status.ACTIVE).exclude(container_id=""))
    per_feature: dict[str, Counter] = {"fuel": Counter(), "finance": Counter()}

    for tenant in tenants:
        if tenant.fuel_enabled:
            from apps.fuel.views import _schedule_fuel_welcome

            _tally_welcome(_schedule_fuel_welcome, tenant, per_feature["fuel"], "fuel", logger)
        if tenant.finance_enabled:
            from apps.finance.views import _schedule_finance_welcome

            _tally_welcome(_schedule_finance_welcome, tenant, per_feature["finance"], "finance", logger)

    totals = {
        "tenants": len(tenants),
        "fuel": dict(per_feature["fuel"]),
        "finance": dict(per_feature["finance"]),
        "statuses": [s.value for s in WelcomeStatus],
    }
    logger.info("reconcile_welcomes: %s", totals)
    return totals


def _tally_welcome(helper, tenant, counts, feature: str, logger) -> None:
    try:
        status = helper(tenant)
    except Exception:
        counts["failed"] += 1
        logger.warning("reconcile_welcomes: %s failed for %s", feature, str(tenant.id)[:8], exc_info=True)
        return
    key = getattr(status, "value", str(status))
    counts[key] += 1


def remove_zombie_heartbeats_task() -> dict:
    """Remove Heartbeat Check-in cron jobs from tenants with heartbeat disabled."""
    import logging

    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        heartbeat_enabled=False,
    ).select_related("user")

    removed = 0
    skipped = 0
    errors = 0

    for tenant in tenants:
        tid = str(tenant.id)[:8]
        if not tenant.container_fqdn:
            skipped += 1
            continue

        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        except GatewayError:
            logger.warning("zombie_heartbeats: tenant %s — cannot list jobs", tid)
            errors += 1
            time.sleep(1)
            continue

        jobs = []
        if isinstance(list_result, dict):
            inner = list_result.get("details", list_result)
            if isinstance(inner, dict):
                jobs = inner.get("jobs", [])
            else:
                jobs = list_result.get("jobs", [])
        elif isinstance(list_result, list):
            jobs = list_result

        heartbeat = next(
            (j for j in jobs if isinstance(j, dict) and j.get("name") == "Heartbeat Check-in"),
            None,
        )
        if not heartbeat:
            skipped += 1
            time.sleep(0.5)
            continue

        job_id = heartbeat.get("id") or heartbeat.get("jobId", "Heartbeat Check-in")
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            removed += 1
            logger.info("zombie_heartbeats: tenant %s — removed heartbeat (id=%s)", tid, job_id)
        except GatewayError:
            logger.exception("zombie_heartbeats: tenant %s — failed to remove", tid)
            errors += 1

        time.sleep(1)

    return {"removed": removed, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# At-cron wake sweep
# ---------------------------------------------------------------------------


# Look-ahead window for proactive wake scheduling. An at-cron firing more than
# this far in the future doesn't need a wake task yet — the next sweep will
# pick it up. Bounding the window keeps QStash scheduled-task count proportional
# to imminent work rather than the tenant's full reminder queue.
_AT_WAKE_LOOKAHEAD_SECONDS = 2 * 60 * 60  # 2 hours


def ensure_at_cron_wakes_task() -> dict:
    """Backstop wake scheduling for one-off ``kind:"at"`` crons.

    Background: Django only schedules ``wake_for_cron`` tasks via
    ``hibernate_idle_tenants`` — i.e. when it cleanly hibernates a tenant.
    For an ``at`` cron created mid-conversation, that path doesn't run
    until the next hourly idle sweep, and even then only if the tenant is
    actually idle. If the container goes down out-of-band (Azure replica
    recycle, OOM, crash) between cron creation and fire time, the fire
    is missed because nothing wakes the tenant.

    This task runs every 5 minutes. For each active tenant on the
    postgres-cron-canonical flow it asks the gateway for current
    ``kind:"at"`` jobs, computes the fire time, and publishes a
    ``wake_for_cron`` task idempotency-keyed on ``(tenant, fire_time_ms)``
    so duplicates collapse. Hibernated tenants are skipped — the
    hibernation snapshot path already covers them.

    Worst-case window: 5 minutes between cron creation and wake
    registration. Acceptable for ``at`` reminders, which typically have
    horizons of minutes-to-hours; a sub-5-minute reminder is firing while
    the user is still in conversation and the container is awake anyway.
    """
    import logging
    import time as _time

    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
    from apps.cron.pending_at_views import _at_fires_at_ms
    from apps.cron.publish import publish_task
    from apps.orchestrator.hibernation import _CRON_WAKE_LEAD_SECONDS
    from apps.orchestrator.services import _extract_cron_jobs
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    now_ms = int(_time.time() * 1000)
    cutoff_ms = now_ms + _AT_WAKE_LOOKAHEAD_SECONDS * 1000
    totals = {"tenants": 0, "scheduled": 0, "errors": 0, "skipped": 0}

    tenants = (
        Tenant.objects.filter(status="active")
        .exclude(container_fqdn="")
        .exclude(hibernated_at__isnull=False)  # hibernated path is owned by hibernate_idle_tenants
        .select_related("user")
    )
    for tenant in tenants:
        totals["tenants"] += 1
        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {})
        except GatewayError:
            totals["skipped"] += 1
            continue

        # Iterate the raw gateway response — we don't need the dashboard
        # decoration from ``_extract_at_jobs``, just fire times.
        for job in _extract_cron_jobs(list_result) or []:
            if not isinstance(job, dict):
                continue
            schedule = job.get("schedule")
            if not isinstance(schedule, dict) or schedule.get("kind") != "at":
                continue
            if job.get("enabled") is False:
                continue
            fires_at_ms = _at_fires_at_ms(job)
            if not isinstance(fires_at_ms, int):
                continue
            if fires_at_ms <= now_ms or fires_at_ms > cutoff_ms:
                continue
            delay = max(60, (fires_at_ms - now_ms) // 1000 - _CRON_WAKE_LEAD_SECONDS)
            try:
                publish_task(
                    "wake_for_cron",
                    str(tenant.id),
                    delay_seconds=delay,
                    idempotency_key=f"wake-cron-{tenant.id}-{fires_at_ms}",
                )
                totals["scheduled"] += 1
            except Exception:
                logger.exception(
                    "ensure_at_cron_wakes: publish_task failed for tenant %s",
                    str(tenant.id)[:8],
                )
                totals["errors"] += 1

    logger.info("ensure_at_cron_wakes: %s", totals)
    return totals
