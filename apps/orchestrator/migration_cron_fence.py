"""One tenant-row guard for cron mutations during a migration cutover.

The SQL guard is also called by the CronJob trigger (including bulk/raw writes).
Transport writers hold its tenant lock until their external operation finishes,
so activating the fence drains writers admitted before pre-staging.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from django.db import connection, transaction
from django.http import JsonResponse
from rest_framework.exceptions import APIException

_rejected = ContextVar("migration_cron_rejected", default=None)


class AssistantUpdating(APIException):
    status_code = 409
    default_detail = "Your assistant is updating. Please try changing your reminders again in one minute."
    default_code = "assistant_updating"


def _translate_error(execute, sql, params, many, context):
    try:
        return execute(sql, params, many, context)
    except Exception as exc:
        cause = exc.__cause__ or exc
        if getattr(cause, "sqlstate", None) == "P0094":
            rejected = _rejected.get()
            if rejected is not None:
                rejected.append(True)
            raise AssistantUpdating() from None
        raise


@contextmanager
def cron_mutation(tenant_id):
    with connection.execute_wrapper(_translate_error), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT nbhd_migration_cron_guard(%s)", [str(tenant_id)])
            active = cursor.fetchone()[0]
        yield active


def guard_transport(fn):
    @wraps(fn)
    def guarded(tenant, *args, **kwargs):
        # cron.list/status are observations. All other cron tools can mutate.
        if fn.__name__ == "invoke_gateway_tool":
            tool = args[0] if args else kwargs.get("tool", "")
            if not tool.startswith("cron.") or tool in {"cron.list", "cron.status"}:
                return fn(tenant, *args, **kwargs)
        with cron_mutation(tenant.id) as active:
            if active and fn.__name__ == "write_tenant_crons_file":
                from apps.cron.share_cron_sync import build_signed_crons_doc

                from .migration_signed_file import publish_signed_file

                data, count = build_signed_crons_doc(tenant)
                publish_signed_file(tenant, data)
                return count
            return fn(tenant, *args, **kwargs)

    return guarded


class CronFenceMiddleware:
    """Preserve retryable JSON even where legacy handlers catch all exceptions.

    DB writes fail before changing a row; only that exact database error marks
    the request. Unrelated requests/responses take their original path.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rejected = []
        token = _rejected.set(rejected)
        try:
            with connection.execute_wrapper(_translate_error):
                response = self.get_response(request)
            if rejected:
                body = {"error": "assistant_updating", "retry_after": 60}
                if "/integrations/runtime/" in request.path:
                    body["detail"] = str(AssistantUpdating.default_detail)
                return JsonResponse(body, status=409)
            return response
        finally:
            _rejected.reset(token)
