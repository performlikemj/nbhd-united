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
    )
    query = query.filter(
        models.Q(last_message_at__isnull=True) | models.Q(last_message_at__lt=cutoff),
    )
    evaluated = query.count()

    updated = 0
    failed = 0
    for tenant in query:
        try:
            update_tenant_config(str(tenant.id))
        except Exception:
            logger.exception("Auto apply config failed for tenant %s", tenant.id)
            failed += 1
            continue

        now = timezone.now()
        Tenant.objects.filter(id=tenant.id).update(
            config_version=models.F("pending_config_version"),
            config_refreshed_at=now,
        )
        updated += 1

    desired_tag = getattr(settings, "OPENCLAW_IMAGE_TAG", "latest")
    image_updated = 0
    image_failed = 0
    if desired_tag and desired_tag != "latest":
        stale_image_tenants = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
        ).exclude(
            container_image_tag=desired_tag,
        ).filter(
            models.Q(last_message_at__isnull=True) | models.Q(last_message_at__lt=cutoff),
        )
        desired_image = f"{settings.AZURE_ACR_SERVER}/nbhd-openclaw:{desired_tag}"

        for tenant in stale_image_tenants:
            try:
                update_container_image(tenant.container_id, desired_image)
                Tenant.objects.filter(id=tenant.id).update(
                    container_image_tag=desired_tag,
                )
                image_updated += 1
            except Exception:
                logger.exception("Auto image update failed for tenant %s", tenant.id)
                image_failed += 1

    # Re-seed cron jobs for active tenants that have none.
    cron_seeded = 0
    cron_seed_failed = 0
    from apps.orchestrator.services import seed_cron_jobs

    active_tenants_with_containers = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
    ).select_related("user")

    for tenant in active_tenants_with_containers:
        try:
            result = seed_cron_jobs(tenant)
            if result.get("created", 0) > 0:
                cron_seeded += 1
                logger.info(
                    "Re-seeded %d cron jobs for tenant %s",
                    result["created"],
                    tenant.id,
                )
        except Exception:
            cron_seed_failed += 1
            logger.exception("Cron re-seed failed for tenant %s", tenant.id)

    return JsonResponse({
        "updated": updated,
        "failed": failed,
        "evaluated": evaluated,
        "image_updated": image_updated,
        "image_failed": image_failed,
        "cron_seeded": cron_seeded,
        "cron_seed_failed": cron_seed_failed,
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

    URL: /api/v1/cron/expire-trials/
    """
    if not verify_qstash_signature(request):
        logger.warning("Unauthorized expire-trials cron attempt")
        return JsonResponse({"error": "Invalid signature"}, status=401)

    now = timezone.now()
    query = Tenant.objects.filter(
        is_trial=True,
        trial_ends_at__lte=now,
    ).filter(
        models.Q(stripe_subscription_id__isnull=True) | models.Q(stripe_subscription_id=""),
    )

    updated = 0
    for tenant in query:  # noqa: PERF401
        tenant.is_trial = False
        tenant.status = Tenant.Status.SUSPENDED
        tenant.save(update_fields=["is_trial", "status", "updated_at"])
        updated += 1

    return JsonResponse({"updated": updated})
