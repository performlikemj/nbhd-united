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

# The current-plan read is a fast synchronous call the plugin waits on inside
# its 20s tool budget, so it gets a short timeout (sautai's /current/ endpoint
# is an unbounded fast read — see docs/sautai-phase0-contract.md #2).
CURRENT_PLAN_TIMEOUT_SECONDS = 10.0


class RetryableSautaiError(Exception):
    """A sautai generate failure that QStash should redeliver.

    The job is marked FAILED (claimable again) BEFORE this is raised. It then
    propagates out of ``generate_sautai_meal_plan_task`` — which the task does
    NOT catch — so ``apps.cron.views.trigger_task`` returns 500 and QStash
    redelivers (3x). Each redelivery re-claims the FAILED row (FAILED→GENERATING
    via the task's atomic CAS) and retries; once QStash's retries are exhausted
    the row simply stays FAILED. This is the mechanism the contract relies on to
    absorb sautai's ``503 busy`` (its ``BoundedSemaphore(1)`` when two
    generations overlap) and cold-start 5xx/transport blips.

    Terminal failures (4xx, non-JSON body, missing plan, no email, unconfigured)
    do the opposite — ``_fail`` then a normal return (200) — so QStash does NOT
    retry a request that can never succeed (the #557 no-retry-storm rationale).
    """


def sautai_m2m_config() -> tuple[str, str]:
    """Return ``(base_url, secret)`` for the sautai M2M bridge.

    Both come from production-only settings (``SAUTAI_M2M_BASE_URL`` /
    ``SAUTAI_PLATFORM_SECRET`` — config/settings/production.py, names matched to
    the Azure Container App env vars). There is deliberately no default host, so
    an empty string in either slot is the "not configured" signal every caller
    fails loud on rather than POSTing a user's email to the wrong place.
    """
    base_url = (getattr(settings, "SAUTAI_M2M_BASE_URL", "") or "").rstrip("/")
    secret = getattr(settings, "SAUTAI_PLATFORM_SECRET", "") or ""
    return base_url, secret


def call_sautai_generate_plan(job: SautaiMealPlanJob) -> None:
    """POST to sautai's ``/api/m2m/meal-plan/generate/`` and persist the result.

    On success: ``job.status=READY``, ``result``/``web_link`` stored, then the
    meditation-style completion notify fires.

    On failure the job is always marked FAILED with a safe (never-traceback)
    error, and the failure is classified so QStash retries only what can succeed:

    - RETRYABLE (transport/timeout, ``503`` busy, any ``5xx``) → ``_fail`` then
      raise :class:`RetryableSautaiError`, which propagates → ``trigger_task``
      500 → QStash redelivers (3x), re-claiming the FAILED row each time. This is
      how the contract's "busy resolves on redelivery" actually works.
    - TERMINAL (``4xx``, non-JSON body, missing plan, no email, unconfigured) →
      ``_fail`` and return normally (200); QStash must not retry a doomed request.
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

    base_url, secret = sautai_m2m_config()
    if not base_url or not secret:
        _fail(job, "not_configured: SAUTAI_M2M_BASE_URL / SAUTAI_PLATFORM_SECRET missing")
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
        # Transport/timeout is always worth a redelivery (sautai cold-starting
        # right after un-pause is the common case).
        logger.warning("call_sautai_generate_plan: request failed for job %s: %s", job.id, exc)
        _fail_retryable(job, f"request_failed: {exc}")

    if response.status_code != 200:
        detail = _safe_error_detail(response)
        logger.warning(
            "call_sautai_generate_plan: sautai returned %s for job %s: %s",
            response.status_code,
            job.id,
            detail,
        )
        message = f"sautai_error_{response.status_code}: {detail}"
        # 503 busy (sautai's BoundedSemaphore(1)) and any 5xx resolve on
        # redelivery; 4xx (bad request / auth / not found) never will.
        if response.status_code == 503 or response.status_code >= 500:
            _fail_retryable(job, message)
        _fail(job, message)
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


def _fail_retryable(job: SautaiMealPlanJob, message: str) -> None:
    """Mark FAILED (claimable again) then raise so QStash redelivers. Never returns."""
    _fail(job, message)
    raise RetryableSautaiError(message)


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "(non-JSON error body)"
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("code") or "")[:200]
    return ""


def fetch_sautai_current_plan(*, user_email: str, week_start_iso: str | None) -> dict:
    """Synchronously read a user's current plan from sautai's ``/current/`` endpoint.

    Returns a small outcome dict the runtime view maps to an HTTP response. The
    HTTP call + contract-fixture parsing live here (unit-testable against the
    golden ``current_*.json`` fixtures); the cached-fallback policy lives in the
    view. Outcomes:

    - ``{"outcome": "ok", "plan": {...}, "web_link": "..."}``
    - ``{"outcome": "not_found"}`` — sautai has no plan (or no account) for this
      email + week; contract #2 returns 404 for both.
    - ``{"outcome": "not_configured"}`` — the M2M bridge env is unset (fail loud).
    - ``{"outcome": "error", "detail": "..."}`` — timeout / transport / non-200;
      the view falls back to the most recent READY job's cached plan.
    """
    base_url, secret = sautai_m2m_config()
    if not base_url or not secret:
        return {"outcome": "not_configured"}

    payload: dict = {"user_email": user_email}
    if week_start_iso:
        payload["week_start"] = week_start_iso

    url = f"{base_url}/api/m2m/meal-plan/current/"
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"X-NBHD-Platform-Secret": secret},
            timeout=CURRENT_PLAN_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("fetch_sautai_current_plan: request failed: %s", exc)
        return {"outcome": "error", "detail": f"request_failed: {exc}"}

    if response.status_code == 404:
        return {"outcome": "not_found"}
    if response.status_code != 200:
        return {"outcome": "error", "detail": f"sautai_error_{response.status_code}: {_safe_error_detail(response)}"}

    try:
        body = response.json()
    except ValueError:
        return {"outcome": "error", "detail": "invalid_response: non-JSON body"}

    plan = body.get("plan") if isinstance(body, dict) else None
    if not isinstance(plan, dict):
        return {"outcome": "error", "detail": "invalid_response: missing plan"}

    return {"outcome": "ok", "plan": plan, "web_link": str(body.get("web_link") or "")[:500]}
