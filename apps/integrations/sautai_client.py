"""sautai Phase 0 M2M client — the QStash-task-side HTTP call to sautai.

See docs/sautai-phase0-contract.md (contract v1) and
``apps.integrations.runtime_views.RuntimeSautaiGeneratePlanView`` (the
fast-ack proxy that creates the PENDING job this module operates on).
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings

from .models import SautaiMealPlanJob, SautaiMealPlanJobStatus

logger = logging.getLogger(__name__)

# sautai's generate call blocks 30-60s (Groq batch generation). The contract
# requires the caller (this QStash task) to allow comfortably past that.
REQUEST_TIMEOUT_SECONDS = 125.0


def call_sautai_generate_plan(job: SautaiMealPlanJob) -> None:
    """POST to sautai's ``/api/m2m/meal-plan/generate/`` and persist the result.

    On success: ``job.status=READY``, ``result``/``web_link`` stored, then the
    meditation-style completion notify fires. On any failure: ``job.status=
    FAILED`` with a safe (never-traceback) error message — QStash's own retry
    (3x default) re-invokes ``generate_sautai_meal_plan_task``, which
    re-claims a FAILED job and tries again.
    """
    tenant = job.tenant
    user = getattr(tenant, "user", None)
    email = (getattr(user, "email", "") or "").strip()
    if not email:
        _fail(job, "no_email: tenant has no user email to resolve a sautai account")
        return

    # Minimal structured payload only — never raw conversation (research doc
    # §Egress posture). The real email is already on the User row (not a PII
    # placeholder), so no rehydrate is needed for it; user_prompt may carry
    # [PERSON_N] placeholders from the agent's authoring turn and IS
    # rehydrated here, at the deliberate egress point.
    payload: dict = {
        "user_email": email,
        "number_of_days": job.number_of_days,
    }
    if job.week_start:
        payload["week_start"] = job.week_start.isoformat()

    prompt = (job.user_prompt or "").strip()
    if prompt:
        from apps.pii.redactor import rehydrate_for_tenant

        try:
            prompt = rehydrate_for_tenant(tenant, prompt)
        except Exception:
            logger.warning("call_sautai_generate_plan: prompt rehydrate failed for job %s", job.id, exc_info=True)
        payload["user_prompt"] = prompt

    base_url = (getattr(settings, "SAUTAI_API_BASE_URL", "") or "").rstrip("/")
    secret = getattr(settings, "SAUTAI_PLATFORM_SECRET", "") or ""
    if not base_url or not secret:
        _fail(job, "not_configured: SAUTAI_API_BASE_URL / SAUTAI_PLATFORM_SECRET missing")
        return

    url = f"{base_url}/api/m2m/meal-plan/generate/"
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"X-NBHD-Platform-Secret": secret},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("call_sautai_generate_plan: request failed for job %s: %s", job.id, exc)
        _fail(job, f"request_failed: {exc}")
        return

    if response.status_code != 200:
        detail = _safe_error_detail(response)
        logger.warning(
            "call_sautai_generate_plan: sautai returned %s for job %s: %s",
            response.status_code,
            job.id,
            detail,
        )
        _fail(job, f"sautai_error_{response.status_code}: {detail}")
        return

    try:
        body = response.json()
    except ValueError:
        _fail(job, "invalid_response: sautai returned non-JSON body")
        return

    plan = body.get("plan") if isinstance(body, dict) else None
    if not isinstance(plan, dict):
        _fail(job, "invalid_response: missing plan")
        return

    job.result = plan
    job.web_link = str(body.get("web_link") or "")[:500]
    job.status = SautaiMealPlanJobStatus.READY
    job.error = ""
    job.save(update_fields=["result", "web_link", "status", "error", "updated_at"])

    try:
        from apps.integrations.sautai_notify import notify_sautai_plan_ready

        notify_sautai_plan_ready(job)
    except Exception:
        logger.warning(
            "call_sautai_generate_plan: notify failed for job %s (plan already ready)", job.id, exc_info=True
        )


def _fail(job: SautaiMealPlanJob, message: str) -> None:
    job.status = SautaiMealPlanJobStatus.FAILED
    job.error = message[:480]
    job.save(update_fields=["status", "error", "updated_at"])


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "(non-JSON error body)"
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("code") or "")[:200]
    return ""
