"""
QStash webhook handlers for executing scheduled and on-demand tasks.

QStash sends HTTP POST requests on a schedule (or on-demand via publish),
which this endpoint executes synchronously. This eliminates the need for
Celery workers polling Redis continuously.
"""
import json
import logging
import traceback
import uuid
from importlib import import_module

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.orchestrator.azure_client import restart_container_app, update_container_image
from apps.orchestrator.services import update_tenant_config
from apps.tenants.models import Tenant
from apps.cron.qstash_verify import verify_qstash_signature


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


# Map of URL-safe task names to task module paths.
# Tasks are executed synchronously — no Celery queue involved.
TASK_MAP = {
    # Tenant maintenance (scheduled via QStash cron)
    "reset_daily_counters": "apps.tenants.tasks.reset_daily_counters_task",
    "reset_monthly_counters": "apps.tenants.tasks.reset_monthly_counters_task",
    "cleanup_expired_telegram_tokens": "apps.tenants.tasks.cleanup_expired_telegram_tokens",
    "refresh_expiring_integrations": "apps.integrations.tasks.refresh_expiring_integrations_task",
    # Journal memory sync (on-demand via signal or QStash publish)
    "sync_documents_to_workspace": "apps.journal.tasks.sync_documents_to_workspace",
    # Provisioning (on-demand via QStash publish)
    "provision_tenant": "apps.orchestrator.tasks.provision_tenant_task",
    "deprovision_tenant": "apps.orchestrator.tasks.deprovision_tenant_task",
    "update_tenant_config": "apps.orchestrator.tasks.update_tenant_config_task",
    "seed_cron_jobs": "apps.orchestrator.tasks.seed_cron_jobs_task",
    "repair_stale_tenant_provisioning": "apps.orchestrator.tasks.repair_stale_tenant_provisioning_task",
    # Media cleanup (daily)
    "cleanup_inbound_media": "apps.router.tasks.cleanup_inbound_media_task",
    # Force reseed cron jobs for all tenants (one-off)
    "force_reseed_crons": "apps.orchestrator.tasks.force_reseed_crons_task",
    # Hibernate suspended containers (one-off cleanup)
    "hibernate_suspended": "apps.orchestrator.tasks.hibernate_suspended_task",
    # Per-tenant config/image updates (enqueued by apply-pending-configs)
    "apply_single_tenant_config": "apps.orchestrator.tasks.apply_single_tenant_config_task",
    "apply_single_tenant_image": "apps.orchestrator.tasks.apply_single_tenant_image_task",
    # One-off broadcast (enqueued by broadcast-message)
    "broadcast_single_tenant": "apps.orchestrator.tasks.broadcast_single_tenant_task",
    # Cron dedup (enqueued by dedup-crons)
    "dedup_cron_jobs": "apps.orchestrator.tasks.dedup_cron_jobs_task",
    # Daily infra cost refresh from Azure billing
    "refresh_infra_costs": "apps.billing.tasks.refresh_infra_costs_task",
    # Idle hibernation — scale-to-zero for inactive tenants
    "hibernate_idle_tenants": "apps.orchestrator.tasks.hibernate_idle_tenants_task",
    "deliver_buffered_messages": "apps.orchestrator.hibernation.deliver_buffered_messages_task",
    "resume_hibernated_crons": "apps.orchestrator.hibernation.resume_hibernated_crons_task",
    # Cleanup delivered message buffers
    "cleanup_delivered_buffers": "apps.orchestrator.hibernation.cleanup_delivered_buffers_task",
    # Nightly extraction — goals/tasks/lessons from daily notes
    "nightly_extraction": "apps.orchestrator.tasks.nightly_extraction_task",
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
    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

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

    try:
        logger.info("[%s] QStash executing task %s -> %s", execution_id, task_name, task_path)
        result = execute_task_sync(task_path, *task_args, **task_kwargs)
        logger.info("[%s] Task %s completed successfully", execution_id, task_name)

        return JsonResponse({
            "status": "completed",
            "task_name": task_name,
            "execution_id": execution_id,
            "result": str(result) if result else None,
        })
    except Exception as e:
        logger.error("[%s] Task %s failed: %s", execution_id, task_name, e)
        logger.error(traceback.format_exc())
        return JsonResponse({
            "status": "error",
            "task_name": task_name,
            "execution_id": execution_id,
            "error": str(e),
        }, status=500)


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

    try:
        logger.info("[%s] DEBUG executing task %s -> %s", execution_id, task_name, task_path)
        result = execute_task_sync(task_path, *task_args, **task_kwargs)
        logger.info("[%s] DEBUG task %s completed", execution_id, task_name)

        return JsonResponse({
            "status": "completed",
            "task_name": task_name,
            "execution_id": execution_id,
            "result": str(result) if result else None,
        })
    except Exception as e:
        logger.error("[%s] DEBUG task %s failed: %s", execution_id, task_name, e)
        return JsonResponse({
            "status": "error",
            "task_name": task_name,
            "execution_id": execution_id,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }, status=500)


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

    # Collect all tasks and publish in a single QStash batch call.
    # This replaces 3 serial loops (~51 HTTP calls, ~25s blocking) with
    # one batch HTTP call (~200ms).
    from apps.cron.publish import publish_batch

    batch_tasks: list[tuple[str, tuple, dict]] = []

    # 1. Config updates for idle tenants with pending changes
    config_count = 0
    for tenant in query:
        batch_tasks.append(("apply_single_tenant_config", (str(tenant.id),), {}))
        config_count += 1

    # 2. Image updates for tenants on stale image
    desired_tag = getattr(settings, "OPENCLAW_IMAGE_TAG", "latest")
    image_count = 0
    if desired_tag and desired_tag != "latest":
        stale_image_tenants = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
            hibernated_at__isnull=True,
        ).exclude(
            container_image_tag=desired_tag,
        ).filter(
            models.Q(last_message_at__isnull=True) | models.Q(last_message_at__lt=cutoff),
        )

        for tenant in stale_image_tenants:
            batch_tasks.append(("apply_single_tenant_image", (str(tenant.id), desired_tag), {}))
            image_count += 1

    # 3. Re-seed cron jobs for all active (non-hibernated) tenants
    active_tenants_with_containers = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        hibernated_at__isnull=True,
    ).values_list("id", flat=True)

    cron_seed_count = 0
    for tenant_id in active_tenants_with_containers:
        batch_tasks.append(("seed_cron_jobs", (str(tenant_id),), {}))
        cron_seed_count += 1

    # Single batch publish — one HTTP call instead of ~51 serial ones.
    # Batch is all-or-nothing: either all tasks are enqueued or none are.
    try:
        enqueued = publish_batch(batch_tasks)
    except Exception:
        logger.exception("Batch publish failed for apply-pending-configs")
        enqueued = 0

    success = enqueued == len(batch_tasks) if batch_tasks else True

    return JsonResponse({
        "config_enqueued": config_count if success else 0,
        "config_failed": 0 if success else config_count,
        "evaluated": evaluated,
        "image_enqueued": image_count if success else 0,
        "image_failed": 0 if success else image_count,
        "cron_seed_enqueued": cron_seed_count if success else 0,
        "cron_seed_failed": 0 if success else cron_seed_count,
        "batch_total": len(batch_tasks),
        "batch_enqueued": enqueued,
    })


@csrf_exempt
@require_POST
def force_reseed_crons(request):
    """Force delete-and-recreate cron jobs for all active tenants.

    URL: /api/cron/force-reseed-crons/
    Use when cron job definitions have changed and need to be pushed to all containers.
    """
    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    provided = request.headers.get("X-Deploy-Secret", "")
    if provided and deploy_secret and provided == deploy_secret:
        pass  # CI deploy auth
    elif not verify_qstash_signature(request):
        logger.warning("Unauthorized force-reseed-crons attempt")
        return JsonResponse({"error": "Invalid signature"}, status=401)

    from apps.orchestrator.config_generator import build_cron_seed_jobs
    from apps.cron.gateway_client import invoke_gateway_tool, GatewayError

    # Only touch system-managed jobs — preserve user-created crons
    SYSTEM_JOB_NAMES = {"Morning Briefing", "Evening Check-in", "Week Ahead Review", "Background Tasks"}

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
    ).select_related("user")

    results = []
    for tenant in tenants:
        tid = str(tenant.id)[:8]
        entry = {"tenant": tid, "deleted": 0, "created": 0, "user_jobs_preserved": 0, "errors": []}

        if not tenant.container_fqdn:
            entry["errors"].append("no FQDN")
            results.append(entry)
            continue

        # List existing jobs
        try:
            result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
            existing = result.get("jobs", []) if isinstance(result, dict) else result if isinstance(result, list) else []
        except GatewayError as e:
            entry["errors"].append(f"list: {str(e)[:100]}")
            results.append(entry)
            continue

        # Delete only system-managed jobs
        for job in existing:
            if job.get("name") not in SYSTEM_JOB_NAMES:
                entry["user_jobs_preserved"] += 1
                continue
            job_id = job.get("id") or job.get("jobId")
            if not job_id:
                continue
            try:
                invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
                entry["deleted"] += 1
            except GatewayError as e:
                entry["errors"].append(f"delete {job_id}: {str(e)[:80]}")

        # Create new system jobs
        for job in build_cron_seed_jobs(tenant):
            try:
                invoke_gateway_tool(tenant, "cron.add", {"job": job})
                entry["created"] += 1
            except GatewayError as e:
                entry["errors"].append(f"add {job.get('name', '?')}: {str(e)[:80]}")

        # Safety net: dedup in case partial deletion left orphaned jobs
        try:
            from apps.orchestrator.services import dedup_tenant_cron_jobs
            dedup_result = dedup_tenant_cron_jobs(tenant)
            if dedup_result.get("deleted", 0) > 0:
                entry["deduped"] = dedup_result["deleted"]
        except Exception:
            pass  # Non-critical — don't fail the reseed

        results.append(entry)

    total_created = sum(r["created"] for r in results)
    total_errors = sum(len(r["errors"]) for r in results)
    return JsonResponse({
        "tenants": len(results),
        "total_created": total_created,
        "total_errors": total_errors,
        "details": results,
    })


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


@csrf_exempt
@require_POST
def expire_trials(request):
    """Suspend trials that have reached their end date and are unpaid.

    Disables all cron jobs (not deleted — so they can be re-enabled on
    subscription) and hibernates the container to stop resource costs.

    URL: /api/v1/cron/expire-trials/
    """
    if not verify_qstash_signature(request):
        logger.warning("Unauthorized expire-trials cron attempt")
        return JsonResponse({"error": "Invalid signature"}, status=401)

    from apps.cron.suspension import suspend_tenant_crons
    from apps.orchestrator.azure_client import hibernate_container_app

    now = timezone.now()
    query = Tenant.objects.filter(
        is_trial=True,
        trial_ends_at__lte=now,
    ).filter(
        models.Q(stripe_subscription_id__isnull=True) | models.Q(stripe_subscription_id=""),
    )

    updated = 0
    crons_disabled = 0
    hibernated = 0
    for tenant in query:
        # 1. Disable all cron jobs (before hibernating — gateway must be reachable)
        if tenant.container_fqdn:
            try:
                cron_result = suspend_tenant_crons(tenant)
                crons_disabled += cron_result.get("disabled", 0)
            except Exception:
                logger.exception(
                    "expire_trials: failed to suspend crons for tenant %s", tenant.id
                )

        # 2. Mark as suspended
        tenant.is_trial = False
        tenant.status = Tenant.Status.SUSPENDED
        tenant.save(update_fields=["is_trial", "status", "updated_at"])
        updated += 1

        # 3. Hibernate container (deactivate revision to stop Azure costs)
        if tenant.container_id:
            try:
                hibernate_container_app(tenant.container_id)
                hibernated += 1
            except Exception:
                logger.exception(
                    "expire_trials: failed to hibernate container %s for tenant %s",
                    tenant.container_id, tenant.id,
                )

    return JsonResponse({
        "updated": updated,
        "crons_disabled": crons_disabled,
        "hibernated": hibernated,
    })


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

    from django.db.models import F as DbF, Q

    has_channel = Q(user__telegram_chat_id__isnull=False) | Q(user__line_user_id__isnull=False)
    grace_cutoff = timezone.now() - timedelta(days=1)

    # Reset no-channel tenants created >1 day ago to version 0
    # (new tenants get a 24h grace period to link Telegram/LINE)
    no_channel_reset = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        created_at__lt=grace_cutoff,
    ).exclude(has_channel).exclude(
        config_version=0, pending_config_version=0,
    ).update(config_version=0, pending_config_version=0)

    if no_channel_reset:
        logger.info("bump_all: reset %d no-channel tenant(s) to version 0", no_channel_reset)

    # Only bump tenants that have a delivery channel (or were created <1 day ago)
    count = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
    ).filter(
        has_channel | Q(created_at__gte=grace_cutoff)
    ).update(pending_config_version=DbF("config_version") + 1)

    logger.info("bump_all_pending_configs: marked %d tenant(s) for config update", count)
    return JsonResponse({"queued": count})


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

    from apps.cron.management.commands.register_system_crons import SYSTEM_CRONS

    qstash_token = getattr(settings, "QSTASH_TOKEN", "")
    if not qstash_token:
        return JsonResponse({"error": "QSTASH_TOKEN not configured"}, status=503)

    import httpx

    headers = {
        "Authorization": f"Bearer {qstash_token}",
        "Content-Type": "application/json",
    }

    resp = httpx.get("https://qstash.upstash.io/v2/schedules", headers=headers)
    resp.raise_for_status()
    existing_destinations = {s["destination"] for s in resp.json()}

    registered = []
    skipped = []
    failed = []

    for name, cron_expr, path in SYSTEM_CRONS:
        destination = f"{base_url}{path}"
        if destination in existing_destinations:
            skipped.append(name)
            continue
        create_resp = httpx.post(
            f"https://qstash.upstash.io/v2/schedules/{destination}",
            headers={**headers, "Upstash-Cron": cron_expr},
        )
        if create_resp.status_code in (200, 201):
            registered.append(name)
            logger.info("Registered QStash cron: %s → %s", name, cron_expr)
        else:
            failed.append(name)
            logger.error("Failed to register QStash cron %s: %s %s", name, create_resp.status_code, create_resp.text)

    return JsonResponse({"registered": registered, "skipped": skipped, "failed": failed})


@csrf_exempt
def resync_cron_timezones(request):
    """Delete and recreate system crons for all active tenants using each
    tenant's configured timezone.

    Fixes tenants whose system crons were seeded in UTC before they set
    their timezone.

    Auth: X-Deploy-Secret header must match DEPLOY_SECRET setting.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
    if not deploy_secret:
        logger.error("DEPLOY_SECRET not configured — resync_cron_timezones rejected")
        return JsonResponse({"error": "Not configured"}, status=503)

    provided = request.headers.get("X-Deploy-Secret", "")
    if not provided or provided != deploy_secret:
        logger.warning("Unauthorized resync_cron_timezones attempt")
        return JsonResponse({"error": "Unauthorized"}, status=401)

    from apps.orchestrator.config_generator import build_cron_seed_jobs
    from apps.cron.gateway_client import invoke_gateway_tool, GatewayError

    SYSTEM_JOB_NAMES = {"Morning Briefing", "Evening Check-in", "Week Ahead Review", "Background Tasks"}

    # All tenants with a running container, not just ACTIVE — trial/pending
    # tenants still have containers with potentially wrong UTC crons.
    tenants = Tenant.objects.filter(
        container_id__gt="",
    ).exclude(
        status=Tenant.Status.DEPROVISIONING,
    ).select_related("user")

    results = []
    for tenant in tenants:
        tid = str(tenant.id)
        user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
        if not tenant.container_fqdn:
            results.append({"tenant": tid[:8], "status": "skipped", "reason": "no fqdn"})
            continue
        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
            existing = list_result.get("jobs", []) if isinstance(list_result, dict) else (list_result if isinstance(list_result, list) else [])

            deleted = 0
            for job in existing:
                if job.get("name") not in SYSTEM_JOB_NAMES:
                    continue
                job_id = job.get("jobId") or job.get("id") or job.get("name")
                try:
                    invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
                    deleted += 1
                except GatewayError:
                    pass

            created = 0
            for job in build_cron_seed_jobs(tenant):
                try:
                    invoke_gateway_tool(tenant, "cron.add", {"job": job})
                    created += 1
                except GatewayError as e:
                    logger.error("resync_cron_timezones: add %s for %s failed: %s", job.get("name"), tid[:8], e)

            logger.info("resync_cron_timezones: tenant %s tz=%s deleted=%d created=%d", tid[:8], user_tz, deleted, created)
            results.append({"tenant": tid[:8], "tz": user_tz, "deleted": deleted, "created": created})
        except GatewayError as e:
            logger.error("resync_cron_timezones: tenant %s failed: %s", tid[:8], e)
            results.append({"tenant": tid[:8], "status": "error", "error": str(e)})

    return JsonResponse({"results": results, "total": len(results)})


@csrf_exempt
def run_update_cron_prompts(request):
    """Run update_system_cron_prompts for all active tenants.

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

    from apps.orchestrator.services import update_system_cron_prompts

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
    ).exclude(container_id="")

    results = []
    for tenant in tenants:
        try:
            result = update_system_cron_prompts(tenant)
            results.append({"tenant": str(tenant.id)[:8], "updated": result["updated"], "errors": result["errors"]})
        except Exception as e:
            results.append({"tenant": str(tenant.id)[:8], "status": "error", "error": str(e)})

    total_updated = sum(r.get("updated", 0) for r in results)
    logger.info("run_update_cron_prompts: %d tenants, %d prompts updated", len(results), total_updated)
    return JsonResponse({"results": results, "total_updated": total_updated})


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

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        container_fqdn__gt="",
    )

    batch_tasks = []
    for tenant in tenants:
        # Per-tenant deduplication key prevents double-delivery if endpoint is
        # called multiple times with the same message (e.g. QStash retries).
        tenant_key = f"{broadcast_key}-{str(tenant.id)[:8]}"
        batch_tasks.append((
            "broadcast_single_tenant",
            (str(tenant.id), message),
            {},
            tenant_key,
        ))

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
        return JsonResponse({
            "tenant": str(tenant.id),
            "kept": result["kept"],
            "deleted": result["deleted"],
            "errors": result["errors"],
            "duplicates_found": len(result["duplicates"]),
        })

    # Fan out to all active tenants via QStash
    from apps.cron.publish import publish_batch

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        container_fqdn__gt="",
    )

    batch_tasks = [
        ("dedup_cron_jobs", (str(tenant.id),), {})
        for tenant in tenants
    ]

    try:
        enqueued = publish_batch(batch_tasks)
    except Exception:
        logger.exception("Batch publish failed for dedup")
        enqueued = 0

    return JsonResponse({"enqueued": enqueued, "failed": 0})
