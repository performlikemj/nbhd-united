"""Views for nightly extraction endpoint.

Called by QStash-signed cron per tenant. Auth via QStash signature verification
(same pattern as other tenant cron endpoints in apps/cron/views.py).
"""

from __future__ import annotations

import logging

from django.http import JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from django.conf import settings

from apps.cron.qstash_verify import verify_qstash_signature
from apps.tenants.models import Tenant

from .extraction import run_extraction_for_tenant

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class NightlyExtractionView(View):
    """POST /api/v1/journal/extract/

    Called by a per-tenant QStash cron job. The request body must include
    {"tenant_id": "<uuid>"} and be signed by QStash.

    Returns 200 always (so QStash doesn't retry on app-level failures).
    """

    def post(self, request):
        # Auth: accept DEPLOY_SECRET (manual/CI) or QStash signature
        deploy_secret = getattr(settings, "DEPLOY_SECRET", None)
        provided = request.headers.get("X-Deploy-Secret", "")
        if provided and deploy_secret and provided == deploy_secret:
            pass  # Manual/CI auth
        elif not verify_qstash_signature(request):
            logger.warning("Unauthorized nightly-extract attempt")
            return JsonResponse({"error": "Unauthorized"}, status=401)

        import json as _json
        try:
            body = _json.loads(request.body or b"{}")
            tenant_id = body.get("tenant_id")
        except Exception:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        if not tenant_id:
            return JsonResponse({"error": "tenant_id required"}, status=400)

        try:
            tenant = Tenant.objects.select_related("user").get(id=tenant_id)
        except Tenant.DoesNotExist:
            logger.warning("nightly-extract: unknown tenant %s", tenant_id)
            return JsonResponse({"ok": True, "skipped": "unknown_tenant"})

        try:
            result = run_extraction_for_tenant(tenant)
        except Exception:
            logger.exception("nightly-extract: unhandled error for tenant %s", str(tenant_id)[:8])
            # Soft-fail — return 200 so QStash doesn't retry indefinitely
            return JsonResponse({"ok": True, "error": "extraction_failed"})

        return JsonResponse({"ok": True, **result})
