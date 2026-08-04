"""Shared storage bound for assistant-authored chat history text."""

from __future__ import annotations

import logging
import re

from apps.pii.entity_registry import get_name

logger = logging.getLogger(__name__)

DEFAULT_REPLY_TEXT_MAX_CHARS = 16_000
REPLY_TEXT_TRUNCATION_SUFFIX = "\n\n… [message truncated]"

# Final owner-facing guard.  Accept the annotated model-context form as well as
# the historical bare token, plus markdown-escaped brackets observed in journal
# replies. LOCATION is retained as the deployed synonym for the requested PLACE
# class.
_OUTBOUND_ENTITY_PLACEHOLDER_RE = re.compile(r"\\?\[(PERSON|ORG|PLACE|LOCATION)_(\d+)(?:\|[^\]]*)?\\?\]")

_NEUTRAL_ENTITY_LABELS = {
    "PERSON": "a redacted person",
    "ORG": "a redacted organization",
    "PLACE": "a redacted place",
    "LOCATION": "a redacted place",
}


def guard_outbound_placeholders(
    text: str | None,
    entity_map: dict | None,
    *,
    tenant_id=None,
    channel: str,
) -> str:
    """Resolve or neutralize every entity placeholder before user delivery.

    The regex fast path runs before registry access and performs no database
    work. Every catch logs one structured warning because a placeholder reaching
    this final seam indicates an upstream response-integrity bug, even when the
    tenant registry can safely restore it.
    """
    value = text or ""
    if "[" not in value:
        return value

    matches = list(_OUTBOUND_ENTITY_PLACEHOLDER_RE.finditer(value))
    if not matches:
        return value

    registry = entity_map or {}
    resolved_count = 0
    neutralized_count = 0

    def _replace(match: re.Match) -> str:
        nonlocal resolved_count, neutralized_count
        entity_type, number = match.group(1), match.group(2)
        name = get_name(registry.get(f"[{entity_type}_{number}]"))
        if name:
            resolved_count += 1
            return name
        neutralized_count += 1
        return _NEUTRAL_ENTITY_LABELS[entity_type]

    guarded = _OUTBOUND_ENTITY_PLACEHOLDER_RE.sub(_replace, value)
    logger.warning(
        "raw_outbound_placeholder_guard",
        extra={
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            "channel": channel,
            "placeholder_count": len(matches),
            "resolved_count": resolved_count,
            "neutralized_count": neutralized_count,
        },
    )
    return guarded


def finalize_outbound_text(
    text: str | None,
    entity_map: dict | None,
    *,
    tenant_id=None,
    channel: str,
) -> str:
    """Run ordinary rehydration, then the loud residual-token guard.

    Placeholder-space at rest is intentional, so successfully restored tokens
    are not guard catches. Only a token that remains after the normal restoration
    seam reaches :func:`guard_outbound_placeholders` and emits warning telemetry.
    """
    value = text or ""
    if "[" not in value:
        return value
    if entity_map:
        from apps.pii.redactor import rehydrate_text

        value = rehydrate_text(value, entity_map)
    return guard_outbound_placeholders(
        value,
        entity_map,
        tenant_id=tenant_id,
        channel=channel,
    )


def clamp_reply_text(text: str | None, *, max_chars: int = DEFAULT_REPLY_TEXT_MAX_CHARS) -> str:
    """Return ``text`` capped to ``max_chars``, including a visible suffix.

    Callers must extract or strip assistant control markers before clamping so
    trailing structured metadata is not lost or persisted as a partial marker.
    """
    value = text or ""
    if len(value) <= max_chars:
        return value
    if max_chars < len(REPLY_TEXT_TRUNCATION_SUFFIX):
        raise ValueError("max_chars must fit the reply truncation suffix")
    prefix_chars = max_chars - len(REPLY_TEXT_TRUNCATION_SUFFIX)
    return value[:prefix_chars] + REPLY_TEXT_TRUNCATION_SUFFIX
