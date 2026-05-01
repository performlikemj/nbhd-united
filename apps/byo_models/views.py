"""DRF endpoints for managing BYO subscription credentials.

Endpoints (all under `/api/v1/tenants/byo-credentials/`):

- `GET  /` — list the current tenant's BYO credentials
- `POST /` — paste a credential (provider + mode + token)
- `DELETE /<uuid>/` — disconnect a credential

All endpoints are gated by `tenant.byo_models_enabled`. When the flag
is False, endpoints return 404 (intentional — feature is not advertised
for tenants without the flag).

Security notes:
- The token is never logged. View code never includes `request.body`,
  `request.data`, or the parsed token in any log call or response body.
- On exception, we re-raise a generic message; the original exception
  is logged at the ERROR level WITHOUT including request details.
- A defensive logging filter (`apps.byo_models.logging_filters.RedactBYOPasteBody`)
  scrubs any log records whose message includes the BYO paste path, as
  belt-and-suspenders against future code changes.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.byo_models.models import BYOCredential
from apps.byo_models.services import delete_credential, upsert_credential
from apps.orchestrator.azure_client import apply_byo_credentials_to_container

logger = logging.getLogger(__name__)


# Phase 1 only allows Anthropic CLI subscription mode. Phase 2 will add
# (openai, cli_subscription); Phase 3 will add api_key for both.
_PHASE_1_ALLOWED = {("anthropic", "cli_subscription")}

# Reasonable bounds for an Anthropic OAuth token from `claude setup-token`.
# Real tokens are ~250 chars; we accept a wide range to allow future shape
# changes without breaking this validator.
_TOKEN_MIN_LEN = 32
_TOKEN_MAX_LEN = 4096


def _serialize(cred: BYOCredential) -> dict:
    return {
        "id": str(cred.id),
        "provider": cred.provider,
        "mode": cred.mode,
        "status": cred.status,
        "last_verified_at": cred.last_verified_at.isoformat() if cred.last_verified_at else None,
        "last_error": cred.last_error[:200] if cred.last_error else "",
        "created_at": cred.created_at.isoformat(),
    }


class BYOCredentialListView(APIView):
    """List or paste a BYO credential."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if tenant is None or not tenant.byo_models_enabled:
            return Response(status=status.HTTP_404_NOT_FOUND)
        creds = BYOCredential.objects.filter(tenant=tenant).order_by("provider")
        return Response([_serialize(c) for c in creds])

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if tenant is None or not tenant.byo_models_enabled:
            return Response(status=status.HTTP_404_NOT_FOUND)

        # Pull values without ever holding the token in a named variable
        # that would show up in tracebacks.
        provider = str(request.data.get("provider", "")).strip()
        mode = str(request.data.get("mode", "")).strip()

        if (provider, mode) not in _PHASE_1_ALLOWED:
            return Response(
                {"error": "This provider/mode combination is not yet supported"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        token = request.data.get("token")
        if not isinstance(token, str) or not (_TOKEN_MIN_LEN <= len(token) <= _TOKEN_MAX_LEN):
            return Response(
                {"error": "Token format is invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cred = upsert_credential(tenant, provider, mode, token)
        except ValueError as exc:
            # secret_name_for raises ValueError if tenant has no key_vault_prefix
            logger.error(
                "BYO credential paste failed (config error) for tenant=%s provider=%s: %s",
                tenant.id,
                provider,
                exc,
            )
            return Response(
                {"error": "Tenant is not configured for credential storage"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            # Critical: do NOT include the token, body, or data in this log.
            logger.exception(
                "BYO credential paste failed for tenant=%s provider=%s",
                tenant.id,
                provider,
            )
            return Response(
                {"error": "Credential storage failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Reconcile the container's secret/env bindings + force a new
        # revision so KV-referenced secrets are re-fetched. Failure here
        # is non-fatal — the cred is stored; the next config bump or
        # natural restart will pick it up.
        try:
            apply_byo_credentials_to_container(tenant)
        except Exception:
            logger.exception(
                "apply_byo_credentials_to_container failed for tenant=%s after paste",
                tenant.id,
            )

        return Response(_serialize(cred), status=status.HTTP_201_CREATED)


class BYOCredentialDetailView(APIView):
    """Disconnect a BYO credential."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, cred_id):
        tenant = getattr(request.user, "tenant", None)
        if tenant is None or not tenant.byo_models_enabled:
            return Response(status=status.HTTP_404_NOT_FOUND)

        try:
            cred = BYOCredential.objects.get(id=cred_id, tenant=tenant)
        except BYOCredential.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        delete_credential(cred)

        try:
            apply_byo_credentials_to_container(tenant)
        except Exception:
            logger.exception(
                "apply_byo_credentials_to_container failed for tenant=%s after delete",
                tenant.id,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)
