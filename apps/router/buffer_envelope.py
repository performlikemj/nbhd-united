"""Minimal reconstructable envelope for hibernation-buffered messages.

Phase 0 — ``docs/encryption-at-rest-directive.md`` §7. ``BufferedMessage`` must
NOT store the raw pre-redaction provider webhook (Telegram update / LINE event):
those are the highest-sensitivity rows in the system — they hold the real name,
username, phone, and chat title the subscriber typed, in the clear, while the
tenant is hibernated. Instead we store:

  - ``user_text``: the extracted user message, REDACTED at write time through
    the same seam the live poller / line_webhook use (``redact_user_message``),
    so a buffered message and a live message have the same at-rest posture.
  - ``payload``: a minimal, schema-versioned envelope carrying ONLY the routing
    / media metadata the drain (``apps.orchestrator.hibernation``) needs to
    reconstruct the forward — never the raw PII fields the provider inlines.

What the drain actually consumes from ``payload`` (traced against the drain):

  - LINE: nothing but voice detection (``_buffered_row_is_voice``). The forward
    content is built from ``user_text``; the recipient is ``tenant.user
    .line_user_id``. So the LINE envelope needs only ``is_voice``.
  - Telegram: the legacy drain re-POSTed the whole update to the container's
    ``/telegram-webhook``. The converged drain instead POSTs the redacted
    ``user_text`` to ``/v1/chat/completions`` and relays the rehydrated reply
    (same mechanism as the live poller's ``_drain_telegram_batch``), so it needs
    only the ``chat_id`` (recipient / OpenClaw ``user`` param) plus voice/image
    flags and any media file-id references.

Backward compat: rows written before this change hold the raw webhook and carry
no ``schema`` marker. ``envelope_is_minimal`` / ``envelope_is_voice`` shape-sniff
so the drain handles BOTH shapes. Legacy rows still contain raw PII; they age
out via the delete-on-forward + TTL sweepers (PR #1082) — they are NOT migrated.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Bump when the envelope shape changes incompatibly. Presence of this exact
# value in ``payload["schema"]`` is the marker that a row is a minimal envelope
# (vs a legacy raw webhook).
SCHEMA = "min-v1"


def build_buffer_envelope(channel: str, payload: dict | None) -> dict:
    """Extract the minimal, non-PII envelope the drain needs from a raw webhook.

    ``payload`` is the raw provider webhook (Telegram update or single LINE
    event) — it is read here and DISCARDED; only the returned envelope is
    persisted, so no raw PII field ever reaches the row.
    """
    payload = payload or {}
    if channel == "telegram":
        return _telegram_envelope(payload)
    if channel == "line":
        return _line_envelope(payload)
    # Unknown channel: keep only the marker (never the raw fields).
    return {"schema": SCHEMA, "channel": channel}


def _telegram_envelope(update: dict) -> dict:
    msg = update.get("message") or update.get("edited_message") or {}
    if not isinstance(msg, dict):
        msg = {}
    chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
    env: dict = {
        "schema": SCHEMA,
        "channel": "telegram",
        "is_voice": bool(msg.get("voice")),
        "is_image": bool(msg.get("photo")),
    }
    chat_id = chat.get("id")
    if chat_id is not None:
        env["chat_id"] = chat_id
    media = _telegram_media_refs(msg)
    if media:
        env["media"] = media
    return env


def _telegram_media_refs(msg: dict) -> dict:
    """Opaque file-id references (NOT bytes, NOT PII) so a wake path could
    re-fetch the media. We keep the references but never the content."""
    media: dict = {}
    photo = msg.get("photo")
    if isinstance(photo, list) and photo:
        # Telegram sends multiple resolutions; the last is the largest.
        largest = photo[-1]
        if isinstance(largest, dict) and largest.get("file_id"):
            media["photo_file_id"] = largest["file_id"]
    for key in ("voice", "audio", "document", "video", "sticker"):
        obj = msg.get(key)
        if isinstance(obj, dict) and obj.get("file_id"):
            media[f"{key}_file_id"] = obj["file_id"]
    return media


def _line_envelope(event: dict) -> dict:
    message = event.get("message") if isinstance(event, dict) else None
    message = message if isinstance(message, dict) else {}
    mtype = (message.get("type") or "").lower()
    return {
        "schema": SCHEMA,
        "channel": "line",
        "is_voice": mtype in {"audio", "voice"},
    }


def envelope_is_minimal(payload) -> bool:
    """True when ``payload`` is a minimal envelope (this change), False for a
    legacy raw webhook (pre-change row)."""
    return isinstance(payload, dict) and payload.get("schema") == SCHEMA


def envelope_is_voice(payload) -> bool:
    """Voice detection that works for BOTH the minimal envelope and a legacy
    raw webhook (backward compat)."""
    if envelope_is_minimal(payload):
        return bool(payload.get("is_voice"))
    return _legacy_raw_is_voice(payload)


def envelope_media(payload) -> dict:
    """Media file-id references from a minimal envelope; empty for legacy rows
    (the legacy drain re-POSTed the raw update, so its media rode the payload)."""
    if envelope_is_minimal(payload) and isinstance(payload.get("media"), dict):
        return payload["media"]
    return {}


def envelope_telegram_chat_id(payload):
    """Telegram ``chat_id`` from either envelope shape, or ``None``."""
    if not isinstance(payload, dict):
        return None
    if envelope_is_minimal(payload):
        return payload.get("chat_id")
    # Legacy raw Telegram update.
    msg = payload.get("message") or payload.get("edited_message") or {}
    if isinstance(msg, dict):
        chat = msg.get("chat")
        if isinstance(chat, dict):
            return chat.get("id")
    return None


def _legacy_raw_is_voice(payload) -> bool:
    """Pre-change voice sniff over a raw webhook. Kept verbatim so legacy rows
    still in the queue behave exactly as before."""
    payload = payload or {}
    if not isinstance(payload, dict):
        return False
    events = payload.get("events")
    if isinstance(events, list) and events:
        first = events[0]
        if isinstance(first, dict):
            message = first.get("message") or {}
            mtype = (message.get("type") or "").lower() if isinstance(message, dict) else ""
            if mtype in {"audio", "voice"}:
                return True
    tg_message = payload.get("message") or payload.get("edited_message") or {}
    if isinstance(tg_message, dict) and tg_message.get("voice"):
        return True
    return False


def redact_for_buffer(tenant, text: str) -> str:
    """Redact user text at BUFFER-WRITE time so the raw name/email/phone the
    subscriber typed never rests in ``BufferedMessage``.

    Mirrors the live poller / line_webhook seam: ``redact_user_message`` runs
    NER in-process (the PII ML stack lives in the Django image, not the tenant
    container) so it works for a *hibernated* tenant, mints/reuses placeholders
    against the tenant map, and is the same minting authority a live message
    goes through.

    Fail SAFE, never store raw: ``redact_user_message`` fail-OPENs (returns the
    raw text on internal NER/DB error), so we ALSO run reuse-only masking
    (``redact_known_entities`` — dict lookups, no model, no mint) as a second
    pass to catch any already-known entity a failed mint pass left raw, and fall
    back to reuse-only if the mint pass raises outright. If even reuse-only
    fails, we drop the text rather than persist raw PII.
    """
    if not text or not text.strip():
        return text or ""
    try:
        from apps.pii.redactor import redact_known_entities, redact_user_message

        redacted = redact_user_message(text, tenant)
        return redact_known_entities(tenant, redacted)
    except Exception:
        logger.exception("buffer redaction failed — falling back to reuse-only masking")
        try:
            from apps.pii.redactor import redact_known_entities

            return redact_known_entities(tenant, text)
        except Exception:
            logger.exception("reuse-only buffer redaction also failed — dropping text to avoid storing raw")
            return ""
