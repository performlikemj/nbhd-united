"""Tenant context middleware."""
import threading

from django.utils.deprecation import MiddlewareMixin

_tenant_context = threading.local()


def get_current_tenant_id():
    return getattr(_tenant_context, "tenant_id", None)


class TenantContextMiddleware(MiddlewareMixin):
    """Set tenant context from the authenticated user."""

    def process_request(self, request):
        if hasattr(request, "user") and request.user.is_authenticated:
            _tenant_context.tenant_id = getattr(request.user, "tenant_id", None)
        else:
            _tenant_context.tenant_id = None

    def process_response(self, request, response):
        _tenant_context.tenant_id = None
        return response
