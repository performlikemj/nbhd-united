"""Defensive logging filter that scrubs BYO paste-endpoint records.

This is belt-and-suspenders, not the primary defense. The primary
defense is in `apps.byo_models.views`: the view code never includes the
token, request body, or request data in any log call or response body.

The filter catches the case where some future code change (or a third-
party middleware) starts emitting log records that include the body,
preventing the token from ever reaching stdout/Container Apps log
analytics.

Wire by adding to LOGGING["filters"] and attaching to the "console"
handler in `config/settings/production.py`.
"""

from __future__ import annotations

import logging
import re

# Match anything that looks like a JSON object body (greedy across the
# message). Conservative: any `{...}` block gets redacted in records that
# have already been flagged as touching a BYO endpoint.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# Path prefix that triggers redaction.
_REDACT_PATH = "/api/v1/tenants/byo-credentials/"


class RedactBYOPasteBody(logging.Filter):
    """Strip JSON-looking content from log records targeting the BYO
    paste endpoint.

    Heuristic: if any of the standard LogRecord attributes contain the
    BYO endpoint path AND a JSON-shaped substring, replace the JSON
    block with `[REDACTED]`. Always returns True (never drops records).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Find the longest field that might contain the path.
        candidate_fields = ("msg", "request", "path", "request_path", "url")
        path_present = False
        for field in candidate_fields:
            val = getattr(record, field, None)
            if isinstance(val, str) and _REDACT_PATH in val:
                path_present = True
                break

        if not path_present:
            return True

        # Path is present — redact JSON-shaped blocks across record fields.
        for field in ("msg", "args"):
            val = getattr(record, field, None)
            if isinstance(val, str):
                setattr(record, field, _JSON_BLOCK.sub("[REDACTED]", val))
            elif isinstance(val, tuple):
                setattr(
                    record,
                    field,
                    tuple(_JSON_BLOCK.sub("[REDACTED]", v) if isinstance(v, str) else v for v in val),
                )

        # Wipe any custom request-body-shaped attributes if present.
        for field in ("body", "data", "request_body"):
            if hasattr(record, field):
                setattr(record, field, "[REDACTED]")

        return True
