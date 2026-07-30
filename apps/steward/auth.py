from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from django.conf import settings
from django.http import HttpRequest

MAX_TIMESTAMP_SKEW_SECONDS = 300


@dataclass(frozen=True)
class StewardAuthError(Exception):
    message: str
    status_code: int

    def __str__(self) -> str:
        return self.message


def validate_steward_hmac(request: HttpRequest) -> None:
    """Validate the portfolio-scoped Steward ingest signature, failing closed."""
    secret = getattr(settings, "STEWARD_INGEST_SECRET", "").strip()
    if not secret:
        raise StewardAuthError("Steward ingest is not configured.", 503)

    timestamp = request.headers.get("X-Steward-Timestamp", "").strip()
    signature = request.headers.get("X-Steward-Signature", "").strip().lower()
    if not timestamp or not signature:
        raise StewardAuthError("Missing Steward authentication headers.", 401)

    try:
        timestamp_s = int(timestamp)
    except ValueError as exc:
        raise StewardAuthError("Invalid Steward timestamp.", 401) from exc
    if abs(time.time() - timestamp_s) > MAX_TIMESTAMP_SKEW_SECONDS:
        raise StewardAuthError("Steward timestamp is outside the 300-second window.", 401)

    signed = timestamp.encode("ascii") + b"." + request.body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise StewardAuthError("Invalid Steward signature.", 401)
