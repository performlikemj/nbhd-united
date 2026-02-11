"""Scheduled integration maintenance tasks."""
from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from django.db.models import Q
from django.utils import timezone

from .models import Integration
from .services import (
    get_provider_client_credentials,
    load_tokens_from_key_vault,
    refresh_integration_tokens,
)

logger = logging.getLogger(__name__)

REFRESH_LEAD_MINUTES = 15


def refresh_expiring_integrations_task() -> dict[str, int]:
    """Refresh integrations that are close to expiring.

    Intended to be triggered by QStash on a recurring cadence.
    """
    threshold = timezone.now() + timedelta(minutes=REFRESH_LEAD_MINUTES)
    integrations = Integration.objects.select_related("tenant").filter(
        status=Integration.Status.ACTIVE,
    ).filter(
        Q(token_expires_at__isnull=True) | Q(token_expires_at__lte=threshold)
    )

    checked = refreshed = expired = errored = 0

    for integration in integrations:
        checked += 1
        client_id, client_secret = get_provider_client_credentials(integration.provider)
        if not client_id or not client_secret:
            integration.status = Integration.Status.ERROR
            integration.save(update_fields=["status", "updated_at"])
            errored += 1
            logger.warning(
                "Skipping refresh for %s/%s due to missing client credentials",
                integration.tenant_id,
                integration.provider,
            )
            continue

        raw_tokens = load_tokens_from_key_vault(integration.tenant, integration.provider)
        tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            integration.status = Integration.Status.EXPIRED
            integration.save(update_fields=["status", "updated_at"])
            expired += 1
            logger.warning(
                "Integration missing refresh token; marking expired for %s/%s",
                integration.tenant_id,
                integration.provider,
            )
            continue

        try:
            refresh_integration_tokens(
                tenant=integration.tenant,
                provider=integration.provider,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
            )
            refreshed += 1
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            integration.status = (
                Integration.Status.EXPIRED
                if status_code in (400, 401)
                else Integration.Status.ERROR
            )
            integration.save(update_fields=["status", "updated_at"])
            if integration.status == Integration.Status.EXPIRED:
                expired += 1
            else:
                errored += 1
            logger.warning(
                "Refresh failed for %s/%s with status %s",
                integration.tenant_id,
                integration.provider,
                status_code,
            )
        except Exception:
            integration.status = Integration.Status.ERROR
            integration.save(update_fields=["status", "updated_at"])
            errored += 1
            logger.exception(
                "Unexpected refresh failure for %s/%s",
                integration.tenant_id,
                integration.provider,
            )

    return {
        "checked": checked,
        "refreshed": refreshed,
        "expired": expired,
        "errored": errored,
    }
