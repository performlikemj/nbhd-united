"""Tenant context middleware."""
import threading
import zoneinfo

from django.utils import timezone as dj_timezone
from django.utils.deprecation import MiddlewareMixin

_tenant_context = threading.local()


def get_current_tenant():
    return getattr(_tenant_context, "tenant", None)


class TenantContextMiddleware(MiddlewareMixin):
    """Set tenant context from the authenticated user."""

    def process_request(self, request):
        if hasattr(request, "user") and request.user.is_authenticated:
            _tenant_context.tenant = getattr(request.user, "tenant", None)
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
