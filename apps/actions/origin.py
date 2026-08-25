"""Verified, tracking-only provenance for human-review gate proposals."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

from apps.cron.models import CronJob

logger = logging.getLogger(__name__)

ORIGIN_STAMP_MAX_AGE_SECONDS = 900


@dataclass(frozen=True)
class OriginStamp:
    kind: str = "unknown"
    run_id: str = ""
    job_id: str = ""
    cron_name: str = ""


def _unknown(tenant, reason: str) -> OriginStamp:
    logger.info(
        "origin_stamp_invalid tenant=%s reason=%s",
        str(getattr(tenant, "id", ""))[:8],
        reason,
    )
    return OriginStamp()


def verify_origin_stamp(tenant, origin) -> OriginStamp:
    """Verify a plugin-produced cron origin stamp; never raise.

    Provenance is recorded for labels and history only. Approval policy must
    never read the returned value.
    """

    try:
        if not isinstance(origin, dict):
            return _unknown(tenant, "absent_or_not_object")
        if type(origin.get("v")) is not int or origin.get("v") != 1:
            return _unknown(tenant, "version")
        if origin.get("kind") != "cron":
            return _unknown(tenant, "kind")
        tenant_id = origin.get("tenant_id")
        run_id = origin.get("run_id")
        job_id = origin.get("job_id")
        timestamp = origin.get("ts")
        signature = origin.get("sig")
        if tenant_id != str(tenant.id):
            return _unknown(tenant, "tenant")
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id) > 64
            or not isinstance(job_id, str)
            or not job_id
            or len(job_id) > 64
            or type(timestamp) is not int
            or not isinstance(signature, str)
        ):
            return _unknown(tenant, "shape")
        if abs(int(time.time()) - timestamp) > ORIGIN_STAMP_MAX_AGE_SECONDS:
            return _unknown(tenant, "stale")

        key = str(getattr(tenant, "internal_api_key", "") or "")
        if not key:
            return _unknown(tenant, "key_unavailable")
        message = f"nbhd-origin.v1|{tenant_id}|cron|{run_id}|{job_id}|{timestamp}"
        expected = hmac.new(
            key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return _unknown(tenant, "signature")

        cron_name = (
            CronJob.objects.filter(tenant=tenant, gateway_job_id=job_id).values_list("name", flat=True).first() or ""
        )
        return OriginStamp(kind="cron", run_id=run_id, job_id=job_id, cron_name=cron_name)
    except Exception:
        logger.info(
            "origin_stamp_invalid tenant=%s reason=verification_error",
            str(getattr(tenant, "id", ""))[:8],
            exc_info=True,
        )
        return OriginStamp()
