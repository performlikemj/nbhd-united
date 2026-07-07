"""Logging filter that scrubs Telegram bot tokens out of log records.

Why this exists: every Telegram Bot API call is made over httpx with the bot
token embedded in the URL *path* — e.g.

    https://api.telegram.org/bot<id>:<secret>/getUpdates

httpx logs each outbound request at INFO via the ``httpx`` logger:

    HTTP Request: GET https://api.telegram.org/bot<id>:<secret>/getUpdates "HTTP/1.1 200 OK"

In production the root logger is INFO with a single stdout ``console`` handler,
so that line — token and all — flows straight into Azure Container Apps Log
Analytics. The token is a long-lived credential for the shared platform bot and
must never reach a log sink.

This filter is attached to the console handler (see
``config/settings/production.py``) so it scrubs *every* record on its way to
stdout, regardless of which logger emitted it — httpx today, and any future code
that happens to log a constructed Bot API URL or the raw token. It is a
belt-and-suspenders backstop in the same spirit as
``apps.byo_models.logging_filters.RedactBYOPasteBody``.

The numeric bot id is preserved (it identifies the single shared bot and is not
itself the secret); only the auth secret after the colon is replaced, so log
lines stay readable as URLs while the credential is gone.
"""

from __future__ import annotations

import logging
import re

# Telegram bot-token shape: a numeric bot id, a colon, then the auth secret
# (url-safe base64 — 35 chars in the classic format; the {30,} lower bound is
# loose so the pattern survives Telegram lengthening the token). Deliberately
# unanchored so it matches both the URL-path form (``…/bot<id>:<secret>/…`` —
# the leading ``bot`` is letters, so the match naturally starts at the digits)
# and a bare ``<id>:<secret>`` token. The 30+-char url-safe tail after the colon
# makes false positives against ordinary log content effectively impossible.
_TELEGRAM_TOKEN_RE = re.compile(r"(\d{6,}):[A-Za-z0-9_-]{30,}")
_REPLACEMENT = r"\1:[REDACTED]"


def scrub_telegram_token(text: str) -> str:
    """Return ``text`` with the secret half of any Telegram bot token replaced
    by ``[REDACTED]``, keeping the (non-secret) numeric bot id for readability."""
    return _TELEGRAM_TOKEN_RE.sub(_REPLACEMENT, text)


class RedactTelegramToken(logging.Filter):
    """Strip Telegram bot tokens from log records before a handler emits them.

    The token usually arrives inside ``record.args`` as an ``httpx.URL`` object
    (httpx logs ``request.url`` with ``%s``), so it is not a string until the
    record is formatted. We therefore render the message via
    ``record.getMessage()`` and, *only when* a token is present, collapse the
    record onto the redacted, fully-rendered text (``record.msg``) and clear
    ``record.args``. Records without a token are left untouched. Always returns
    True — this filter redacts, it never drops records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # A formatting error here must never suppress the log record.
            return True
        if _TELEGRAM_TOKEN_RE.search(message):
            record.msg = scrub_telegram_token(message)
            record.args = ()
        return True
