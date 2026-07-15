"""Shared storage bound for assistant-authored chat history text."""

from __future__ import annotations

DEFAULT_REPLY_TEXT_MAX_CHARS = 16_000
REPLY_TEXT_TRUNCATION_SUFFIX = "\n\n… [message truncated]"


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
