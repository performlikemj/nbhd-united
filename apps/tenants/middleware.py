"""Tenant context middleware and RLS helpers."""
import logging
import threading
import zoneinfo

from django.db import connection
from django.utils import timezone as dj_timezone
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)

_tenant_context = threading.local()


def get_current_tenant():
    return getattr(_tenant_context, "tenant", None)


def set_rls_context(*, tenant_id=None, user_id=None, service_role=False):
    """Set Postgres session variables for RLS policies.

    Variables are transaction-scoped (auto-reset when the connection is
    returned to the pool), so no manual RESET is needed.
    """
    with connection.cursor() as cursor:
        if tenant_id:
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)]
            )
        if user_id:
            cursor.execute(
                "SELECT set_config('app.user_id', %s, true)", [str(user_id)]
            )
        if service_role:
            cursor.execute(
                "SELECT set_config('app.service_role', 'true', true)"
            )


class TenantContextMiddleware(MiddlewareMixin):
    """Set tenant context from the authenticated user.

    For authenticated requests, sets both the thread-local tenant context
    (used by application code) and Postgres session variables (enforced
    by RLS policies).
    """

    def process_request(self, request):
        if hasattr(request, "user") and request.user.is_authenticated:
            _tenant_context.tenant = getattr(request.user, "tenant", None)
            tenant = _tenant_context.tenant
            if tenant:
                set_rls_context(tenant_id=tenant.id, user_id=request.user.id)
        else:
            _tenant_context.tenant = None

    def process_response(self, request, response):
        _tenant_context.tenant = None
        return response


class UserTimezoneMiddleware(MiddlewareMixin):
    """Activate authenticated user's timezone for the request lifecycle."""

    def process_request(self, request):
        if hasattr(request, "user") and request.user.is_authenticated:
            user_tz = getattr(request.user, "timezone", "UTC") or "UTC"
            try:
                dj_timezone.activate(zoneinfo.ZoneInfo(user_tz))
            except (KeyError, zoneinfo.ZoneInfoNotFoundError):
                dj_timezone.deactivate()
        else:
            dj_timezone.deactivate()

    def process_response(self, request, response):
        dj_timezone.deactivate()
        return response
