"""Console-authenticated sautai account-link endpoints (Phase 0.5).

A subscriber pastes a one-time connect key (minted in sautai) into the NBHD
console; Django exchanges it SERVER-SIDE via sautai's ``/link/resolve/`` (platform
secret, never delivered to the browser) and stores the resulting ``sautai_user_id``
on the tenant's ``Provider.SAUTAI`` Integration row. From then on the meal-plan
M2M calls address sautai by that id, so the user's real dietary profile applies.

The raw connect key is NEVER stored — one-time exchange, burn after resolve.
Disconnect clears the link (the email auto-create fallback resumes).
See docs/sautai-phase05-contract.md (addendum v2) and apps/integrations/sautai_client.py.
"""

from __future__ import annotations

import logging

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Integration

logger = logging.getLogger(__name__)


def _sautai_integration(tenant) -> Integration | None:
    return Integration.objects.filter(tenant=tenant, provider=Integration.Provider.SAUTAI).first()


def _status_payload(integration: Integration | None) -> dict:
    linked = bool(integration and integration.sautai_user_id)
    return {
        "linked": linked,
        "email": (integration.provider_email if linked else "") or "",
        "linked_at": (integration.linked_at.isoformat() if (linked and integration.linked_at) else None),
    }


class SautaiLinkView(APIView):
    """GET link status · POST connect (paste key) · DELETE disconnect."""

    permission_classes = [IsAuthenticated]

    def _tenant(self, request):
        return getattr(request.user, "tenant", None)

    def get(self, request):
        tenant = self._tenant(request)
        if tenant is None:
            return Response({"detail": "No tenant provisioned."}, status=400)
        return Response(_status_payload(_sautai_integration(tenant)))

    def post(self, request):
        tenant = self._tenant(request)
        if tenant is None:
            return Response({"detail": "No tenant provisioned."}, status=400)

        connect_key = str(request.data.get("connect_key") or "").strip()
        if not connect_key:
            return Response({"error": "invalid_request", "detail": "connect_key is required."}, status=400)

        from .sautai_client import resolve_sautai_link_key

        # Server-side exchange with the platform secret; the raw key never leaves
        # Django and is never persisted.
        result = resolve_sautai_link_key(connect_key, nbhd_tenant_id=str(tenant.id))
        outcome = result.get("outcome")

        if outcome == "invalid_key":
            return Response(
                {
                    "error": "invalid_key",
                    "detail": (
                        "That connect key is invalid, expired, or already used. "
                        "Generate a fresh one in sautai and try again."
                    ),
                },
                status=400,
            )
        if outcome == "not_configured":
            return Response(
                {"error": "sautai_not_configured", "detail": "The sautai integration is not configured."},
                status=503,
            )
        if outcome == "retryable":
            return Response(
                {"error": "sautai_busy", "detail": "sautai is busy just now. Please try again."},
                status=503,
            )
        if outcome != "ok":
            return Response(
                {"error": "sautai_unavailable", "detail": "Couldn't reach sautai just now. Please try again."},
                status=502,
            )

        # Integration writes use the service role, matching the OAuth callback's
        # proven write path (the tenant is the authenticated user's own; no
        # cross-tenant reach).
        from apps.tenants.middleware import set_rls_context

        set_rls_context(tenant_id=tenant.id, service_role=True)

        integration, _created = Integration.objects.get_or_create(
            tenant=tenant,
            provider=Integration.Provider.SAUTAI,
            defaults={"status": Integration.Status.ACTIVE},
        )
        integration.sautai_user_id = result["sautai_user_id"]
        integration.linked_at = timezone.now()
        integration.status = Integration.Status.ACTIVE
        update_fields = ["sautai_user_id", "linked_at", "status", "updated_at"]
        email = str(result.get("email") or "").strip()
        if email:
            integration.provider_email = email[:255]
            update_fields.append("provider_email")
        integration.save(update_fields=update_fields)

        logger.info("sautai link connected for tenant %s (sautai_user_id set)", str(tenant.id)[:8])
        return Response({"status": "connected", **_status_payload(integration)})

    def delete(self, request):
        tenant = self._tenant(request)
        if tenant is None:
            return Response({"detail": "No tenant provisioned."}, status=400)

        integration = _sautai_integration(tenant)
        if integration and integration.sautai_user_id:
            from .sautai_client import clear_sautai_link

            clear_sautai_link(integration)
            logger.info("sautai link disconnected for tenant %s", str(tenant.id)[:8])
        return Response({"status": "disconnected", "linked": False, "email": ""})
