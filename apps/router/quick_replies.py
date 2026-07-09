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
        logger.warning(
            "quick_reply_marker_malformed",
            extra={
                "tenant_id": str(tenant_id) if tenant_id is not None else None,
                "channel": channel,
                "label_count": len(labels),
                "sample": last_line[:200],
            },
        )
        return remainder, None
    return remainder, labels
