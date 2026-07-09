"""Shared parser for the generic quick-reply button marker.

The agent ends a reply with a marker on its OWN final line to offer up to 3
tappable choices instead of making the user type a short answer:

    [[quick-replies: Label A | Label B | Label C]]

This is deliberately distinct from LINE's ``[[button:label|data]]`` marker
(``apps.router.line_flex.extract_quick_reply_buttons``): that one carries a
callback payload and can appear anywhere in the text; this one carries plain
labels (the tap re-sends the label as an ordinary chat message — no callback
data) and is only recognized as the LAST line of the reply, so ordinary prose
that happens to reference the shape elsewhere passes through untouched.

Used by every outbound-reply chokepoint (iOS app, Telegram queue drain,
Telegram poller, LINE) so the marker is ALWAYS stripped before a user sees
it — only the iOS app path turns the parsed labels into stored buttons
(``AppChatMessage.quick_replies``); Telegram/LINE discard the labels and keep
only the stripped text.

Labels are parsed (and length-validated) in PII-PLACEHOLDER space, before the
reply is ever rehydrated — see :func:`rehydrate_quick_replies`, the shared
helper the two owner-facing read seams (``chat_views._serialize_message`` and
``chat_history._app_rows``) both call so a stored ``[PERSON_1]`` label always
resolves to the same real name at both seams, and never ships raw.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MAX_QUICK_REPLIES = 3
MAX_LABEL_LEN = 24

# The whole trimmed final line must match — no leading/trailing prose on that
# line — so a marker embedded mid-sentence is left as ordinary text.
_QUICK_REPLY_MARKER_RE = re.compile(r"^\[\[quick-replies:\s*(.+)\]\]$", re.IGNORECASE)


def extract_quick_replies(
    text: str,
    *,
    tenant_id=None,
    channel: str = "",
) -> tuple[str, list[str] | None]:
    """Parse + strip a trailing ``[[quick-replies: A | B | C]]`` marker.

    Returns ``(text_without_marker, labels)``. ``labels`` is ``None`` when no
    marker is present (the marker must be the LAST line, after trimming
    trailing whitespace — a marker anywhere else in the text is left alone
    and ``text`` is returned unchanged).

    A marker that IS shaped correctly but fails validation (not 1-3 labels,
    or any label empty / over ``MAX_LABEL_LEN`` chars after trimming) is
    still stripped — never shown to a user raw — but yields ``labels=None``
    and logs a telemetry warning (agent misuse signal) instead of raising.
    """
    if not text:
        return text, None

    stripped = text.rstrip()
    if not stripped:
        return text, None

    lines = stripped.split("\n")
    last_line = lines[-1].strip()
    match = _QUICK_REPLY_MARKER_RE.match(last_line)
    if not match:
        return text, None

    remainder = "\n".join(lines[:-1]).rstrip()
    labels = [part.strip() for part in match.group(1).split("|")]
    valid = 1 <= len(labels) <= MAX_QUICK_REPLIES and all(0 < len(label) <= MAX_LABEL_LEN for label in labels)
    if not valid:
        _log_malformed(
            tenant_id=tenant_id, channel=channel, label_count=len(labels), sample=last_line, reason="parse_validation"
        )
        return remainder, None
    return remainder, labels


def _log_malformed(*, tenant_id, channel: str, label_count: int, sample: str, reason: str) -> None:
    logger.warning(
        "quick_reply_marker_malformed",
        extra={
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            "channel": channel,
            "label_count": label_count,
            "sample": sample[:200],
            "reason": reason,
        },
    )


def rehydrate_quick_replies(
    labels: list[str] | None,
    entity_map: dict | None,
    *,
    tenant_id=None,
    channel: str = "",
) -> list[str] | None:
    """Rehydrate quick-reply labels from PII-placeholder space to real values.

    Labels are parsed (and length-validated) in placeholder space at write
    time, before ``reply_text`` is ever rehydrated — a label like
    ``"[PERSON_1]"`` is short pre-rehydration, but the real name it stands for
    can push it over ``MAX_LABEL_LEN`` once resolved. Call this from BOTH
    owner-facing read seams (``chat_views._serialize_message`` and
    ``chat_history._app_rows``) so they can't drift on how a stored label
    resolves.

    Re-validates length AFTER rehydration: if ANY label now exceeds the cap,
    the WHOLE set is dropped (returns ``None``) and a
    ``quick_reply_marker_malformed`` warning is logged with
    ``reason="rehydration_overflow"`` — never truncate, since the displayed
    label and the tap-sent text must stay identical, and iOS drops rather
    than truncates by contract.

    Fails open on an unexpected rehydration error: serves the labels
    unchanged (still placeholder-space, but not a security leak — just a
    stale label) rather than dropping the row, mirroring how ``reply_text``
    rehydration fails open at the same two read seams.
    """
    if not labels:
        return None
    if not entity_map:
        rehydrated = list(labels)
    else:
        try:
            from apps.pii.redactor import rehydrate_text

            rehydrated = [rehydrate_text(label, entity_map) for label in labels]
        except Exception:
            logger.exception("quick_replies: label rehydrate failed (non-fatal, serving placeholder labels)")
            return list(labels)

    if any(len(label) > MAX_LABEL_LEN for label in rehydrated):
        _log_malformed(
            tenant_id=tenant_id,
            channel=channel,
            label_count=len(rehydrated),
            sample=" | ".join(rehydrated),
            reason="rehydration_overflow",
        )
        return None
    return rehydrated


# The literal opener this streaming heuristic watches for. Checked against at
# least 3 characters so a bare "[[" — which could be the start of ANY marker
# ([[chart:, [[button:, [[insight:...) — isn't mistaken for this one; the
# third character alone ("q") already disambiguates.
_STREAMING_MARKER_OPENER = "[[quick-replies:"
_STREAMING_MIN_MATCH = 3


def strip_streaming_quick_reply_marker(text: str) -> str:
    """Hide an in-progress ``[[quick-replies: ...`` marker from the LIVE
    streaming ``partial_text`` bubble — whether it's mid-typing (unclosed) or
    already complete. ``partial_text`` is cumulative text-so-far written on
    every model-call step (see ``ChatProgressEventView``), so this runs on
    every write; a marker that hasn't started yet (no trailing ``[[``) is a
    no-op. Purely cosmetic — real parsing/validation only ever happens on the
    terminal reply via :func:`extract_quick_replies`.
    """
    if not text:
        return text
    idx = text.rfind("[[")
    if idx == -1:
        return text
    tail = text[idx:]
    check_len = min(len(tail), len(_STREAMING_MARKER_OPENER))
    if check_len < _STREAMING_MIN_MATCH:
        return text
    if tail[:check_len].lower() != _STREAMING_MARKER_OPENER[:check_len].lower():
        return text
    cut = idx - 1 if idx > 0 and text[idx - 1] == "\n" else idx
    return text[:cut]
