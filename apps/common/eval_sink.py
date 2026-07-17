"""Authoritative real-transport suppression for eval-sink tenants."""

from __future__ import annotations

import logging

from django.core.exceptions import ObjectDoesNotExist

logger = logging.getLogger(__name__)


def suppresses_real_transport(tenant) -> bool:
    """Return whether ``tenant`` is explicitly marked as an eval sink."""
    return getattr(tenant, "is_eval_sink", False) is True


def blocks_real_transport_for_identifier(transport: str, identifier) -> bool:
    """Fail closed when a Telegram/LINE identifier belongs to an eval sink.

    Unknown identifiers deliberately pass through: fleet/ops destinations are
    not necessarily backed by a user row. Both user identifier columns are
    unique, so the ownership check is one indexed query.
    """
    from apps.tenants.models import User

    if transport == "telegram":
        lookup = {"telegram_chat_id": identifier}
    elif transport == "line":
        lookup = {"line_user_id": identifier}
    else:
        raise ValueError(f"unsupported transport: {transport}")

    try:
        owner = User.objects.select_related("tenant").filter(**lookup).first()
    except Exception:
        # Fail closed: an unresolved owner must never permit real transport.
        logger.exception(
            "eval-sink transport block: owner lookup failed transport=%s",
            transport,
        )
        return True

    if owner is None:
        return False

    try:
        tenant = owner.tenant
    except ObjectDoesNotExist:
        # Auth/ops users can own a provider identifier without owning a tenant.
        return False
    if not suppresses_real_transport(tenant):
        return False

    logger.error(
        "eval-sink transport block: tenant=%s transport=%s",
        tenant.id,
        transport,
    )
    return True
