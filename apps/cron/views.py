"""
QStash webhook handlers for executing scheduled and on-demand tasks.

QStash sends HTTP POST requests on a schedule (or on-demand via publish),
which this endpoint executes synchronously. This eliminates the need for
Celery workers polling Redis continuously.
"""
import logging
import traceback
import uuid
from importlib import import_module

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


logger = logging.getLogger(__name__)


def verify_qstash_signature(request):
    """
    Verify the request came from QStash using the official SDK.

    Uses the qstash Receiver class for proper JWT verification.
    See: https://upstash.com/docs/qstash/howto/signature
    """
    try:
        from qstash import Receiver
    except ImportError:
        logger.error("qstash package not installed — cannot verify signatures")
        return False

    signature = request.headers.get("Upstash-Signature")
    if not signature:
        logger.warning("QStash request missing Upstash-Signature header")
        return False

    current_key = getattr(settings, "QSTASH_CURRENT_SIGNING_KEY", None)
    next_key = getattr(settings, "QSTASH_NEXT_SIGNING_KEY", None)

    if not current_key:
        logger.error("QSTASH_CURRENT_SIGNING_KEY not configured")
        return False

    try:
        receiver = Receiver(
            current_signing_key=current_key,
            next_signing_key=next_key or current_key,
        )
        url = request.build_absolute_uri()
        body = request.body.decode("utf-8") if request.body else ""
        receiver.verify(signature=signature, body=body, url=url)
        return True
    except Exception as e:
        logger.warning("QStash signature verification failed: %s", e)
        return False


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
