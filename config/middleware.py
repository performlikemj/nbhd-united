"""Preview access gate middleware."""
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)

# Path prefixes that bypass the preview gate (they have their own auth).
EXEMPT_PREFIXES = (
    "/admin/",
    "/api/v1/billing/webhook/",
    "/api/v1/telegram/webhook/",
    "/api/cron/",
    "/stripe/",
    "/api/v1/integrations/callback/",
    "/api/v1/integrations/composio-callback/",
)


class PreviewAccessMiddleware(MiddlewareMixin):
    """Require a valid preview key on all requests unless exempted.

    Activated only when settings.PREVIEW_ACCESS_KEY is a non-empty string.
    Checks the ``X-Preview-Key`` header against the configured secret.
    """

    def process_request(self, request):
        required_key = getattr(settings, "PREVIEW_ACCESS_KEY", "")
        if not required_key:
            return None

        for prefix in EXEMPT_PREFIXES:
            if request.path.startswith(prefix):
                return None

        provided_key = request.headers.get("X-Preview-Key", "")
        if provided_key == required_key:
            return None

        return JsonResponse(
            {"detail": "Preview access required."},
            status=403,
        )
