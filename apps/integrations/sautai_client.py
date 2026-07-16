"""sautai M2M client — the HTTP calls from NBHD Django to sautai.

Phase 0 (docs/sautai-phase0-contract.md): generate + current-plan.
Phase 0.5 (contract addendum v2): account linking via ``/link/resolve/`` and
addressing sautai by ``sautai_user_id`` (a linked account) instead of the tenant
email. See ``apps.integrations.runtime_views`` (proxy) and
``apps.integrations.link_views`` (console connect flow).
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings

from .models import (
    Integration,
    SautaiMealPlanAddressedBy,
    SautaiMealPlanJob,
    SautaiMealPlanJobStatus,
)

logger = logging.getLogger(__name__)

# sautai's generate call blocks 30-60s (Groq batch generation). The contract
# requires the caller (this QStash task) to allow comfortably past that.
REQUEST_TIMEOUT_SECONDS = 125.0

# The current-plan read + link resolve are fast synchronous calls a caller waits
# on inside a 20s budget, so they get short timeouts.
CURRENT_PLAN_TIMEOUT_SECONDS = 10.0
LINK_RESOLVE_TIMEOUT_SECONDS = 10.0


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

    Terminal failures (4xx, non-JSON body, missing plan, no identity, unconfigured)
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


def sautai_identity(tenant) -> tuple[dict, Integration | None]:
    """Pick the M2M identity for a tenant: linked ``sautai_user_id`` over email.

    Phase 0.5: if the tenant's ``Provider.SAUTAI`` Integration carries a
    ``sautai_user_id`` (they linked an existing sautai account), address sautai
    by that id — their real dietary profile applies and sautai never auto-creates
    on the id path. Otherwise fall back to the tenant owner's verified email (the
    Phase 0 shell-account path). Returns the identity payload fragment plus the
    Integration row (so a caller can clear a stale link). Empty dict if the tenant
    has neither a link nor an email.
    """
    integration = Integration.objects.filter(tenant=tenant, provider=Integration.Provider.SAUTAI).first()
    if integration and integration.sautai_user_id:
        return {"sautai_user_id": integration.sautai_user_id}, integration
    user = getattr(tenant, "user", None)
    email = (getattr(user, "email", "") or "").strip()
    if email:
        return {"user_email": email}, integration
    return {}, integration


def clear_sautai_link(integration: Integration) -> None:
    """Null out a sautai account link (disconnect, or sautai lost the account)."""
    integration.sautai_user_id = None
    integration.linked_at = None
    integration.save(update_fields=["sautai_user_id", "linked_at", "updated_at"])


def call_sautai_generate_plan(job: SautaiMealPlanJob) -> None:
    """POST to sautai's ``/api/m2m/meal-plan/generate/`` and persist the result.

    On success: ``job.status=READY``, ``result``/``web_link``/``funnel`` stored,
    then the meditation-style completion notify fires.

    On failure the job is always marked FAILED with a safe (never-traceback)
    error, and the failure is classified so QStash retries only what can succeed:

    - RETRYABLE (transport/timeout, ``503`` busy, any ``5xx``) → ``_fail`` then
      raise :class:`RetryableSautaiError`, which propagates → ``trigger_task``
      500 → QStash redelivers (3x), re-claiming the FAILED row each time.
    - STALE LINK (``404 code=unknown_user`` on a ``sautai_user_id`` call) → clear
      the link (email auto-create resumes next time) and ``_fail`` with a
      reconnect hint. Terminal — retrying the same dead id won't help.
    - TERMINAL (other ``4xx``, non-JSON body, missing plan, no identity,
      unconfigured) → ``_fail`` and return normally (200); no retry.
    """
    tenant = job.tenant

    identity, integration = sautai_identity(tenant)
    if not identity:
        _fail(job, "no_identity: tenant has no sautai link and no email to resolve an account")
        return

    # Minimal structured payload only — never raw conversation (research doc
    # §Egress posture). A linked call sends sautai_user_id (no email); the email
    # path sends the real email (already a User-row value, not a PII placeholder).
    # user_prompt may carry [PERSON_N] placeholders and IS rehydrated here, at the
    # deliberate egress point.
    payload: dict = {**identity, "number_of_days": job.number_of_days}
    if job.week_start:
        payload["week_start"] = job.week_start.isoformat()
    if job.regenerate:
        payload["regenerate"] = True

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

    # Persist only the identity route and linked numeric id used for this egress.
    # Never duplicate the raw email onto the job row.
    if "sautai_user_id" in identity:
        job.addressed_by = SautaiMealPlanAddressedBy.LINKED_ID
        job.sautai_user_id = identity["sautai_user_id"]
    else:
        job.addressed_by = SautaiMealPlanAddressedBy.EMAIL
        job.sautai_user_id = None
    job.save(update_fields=["addressed_by", "sautai_user_id", "updated_at"])

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
        # Stale link: the linked sautai account no longer exists. Clear it so the
        # email (auto-create) path resumes next time, and fail with a reconnect
        # hint. Terminal — the same dead id will never resolve.
        if (
            response.status_code == 404
            and _response_code(response) == "unknown_user"
            and "sautai_user_id" in identity
            and integration is not None
        ):
            clear_sautai_link(integration)
            _fail(job, "sautai_link_invalid: linked sautai account not found — reconnect sautai in settings")
            return

        message = f"sautai_error_{response.status_code}: {detail}"
        # 503 busy (sautai's BoundedSemaphore(1)) and any 5xx resolve on
        # redelivery; other 4xx (bad request / auth) never will.
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
    job.funnel = _funnel_from_response(body)
    job.status = SautaiMealPlanJobStatus.READY
    job.error = ""
    job.save(update_fields=["result", "web_link", "funnel", "status", "error", "updated_at"])

    try:
        from apps.integrations.sautai_notify import notify_sautai_plan_ready

        notify_sautai_plan_ready(job)
    except Exception:
        logger.warning(
            "call_sautai_generate_plan: notify failed for job %s (plan already ready)", job.id, exc_info=True
        )


def _funnel_from_response(body: dict) -> dict:
    """Extract the Phase 0.5 funnel fields from a generate/current 200 body.

    ``claim_link`` is present only when ``account_claimed`` is false (contract
    addendum #4); ``already_existed`` drives the "a plan already existed —
    regenerate?" honesty branch in the notify copy.
    """
    return {
        "account_claimed": body.get("account_claimed"),
        "plan_count": body.get("plan_count"),
        "claim_link": str(body.get("claim_link") or "")[:500],
        "already_existed": body.get("already_existed"),
    }


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


def _response_code(response: httpx.Response) -> str:
    """The ``code`` field from a sautai error body (e.g. ``unknown_user``), or ""."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("code") or "") if isinstance(body, dict) else ""


def fetch_sautai_current_plan(*, identity: dict, week_start_iso: str | None) -> dict:
    """Synchronously read a user's current plan from sautai's ``/current/`` endpoint.

    ``identity`` is a payload fragment from :func:`sautai_identity` — either
    ``{"sautai_user_id": int}`` (linked) or ``{"user_email": str}``. Returns a
    small outcome dict the runtime view maps to an HTTP response; the HTTP call +
    contract-fixture parsing live here (unit-testable against the golden
    ``current_*.json`` fixtures), the cached-fallback policy in the view. Outcomes:

    - ``{"outcome": "ok", "plan": {...}, "web_link": "...", "funnel": {...}}``
    - ``{"outcome": "not_found"}`` — no plan (or unknown user_id); contract 404.
    - ``{"outcome": "not_configured"}`` — the M2M bridge env is unset (fail loud).
    - ``{"outcome": "error", "detail": "..."}`` — timeout / transport / non-200;
      the view falls back to the most recent READY job's cached plan.
    """
    base_url, secret = sautai_m2m_config()
    if not base_url or not secret:
        return {"outcome": "not_configured"}
    if not identity:
        return {"outcome": "not_found"}

    payload: dict = {**identity}
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

    return {
        "outcome": "ok",
        "plan": plan,
        "web_link": str(body.get("web_link") or "")[:500],
        "funnel": _funnel_from_response(body),
    }


def resolve_sautai_link_key(link_key: str, *, nbhd_tenant_id: str) -> dict:
    """Exchange a one-time connect key for a sautai user id + email via ``/link/resolve/``.

    Called SERVER-SIDE from the console connect endpoint (the raw key is never
    stored — one-time exchange, burn after resolve). Contract addendum #1: 200
    echoes the required opaque ``nbhd_tenant_id`` alongside ``sautai_user_id``
    and ``email``; unknown/expired/used key → 404 ``{"code":"invalid_key"}``.
    Outcomes:

    - ``{"outcome": "ok", ..., "nbhd_tenant_id": str}``
    - ``{"outcome": "invalid_key"}`` — reject with a clear user-facing message.
    - ``{"outcome": "not_configured"}`` — the M2M bridge env is unset.
    - ``{"outcome": "retryable", "detail": "..."}`` — 503 busy; caller retries.
    - ``{"outcome": "error", "detail": "..."}`` — timeout / transport / non-200.
    """
    base_url, secret = sautai_m2m_config()
    if not base_url or not secret:
        return {"outcome": "not_configured"}

    url = f"{base_url}/api/m2m/link/resolve/"
    try:
        response = httpx.post(
            url,
            json={"link_key": link_key, "nbhd_tenant_id": nbhd_tenant_id},
            headers={"X-NBHD-Platform-Secret": secret},
            timeout=LINK_RESOLVE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("resolve_sautai_link_key: request failed: %s", exc)
        return {"outcome": "error", "detail": f"request_failed: {exc}"}

    if response.status_code == 404:
        return {"outcome": "invalid_key"}
    if response.status_code == 503:
        return {
            "outcome": "retryable",
            "detail": f"sautai_error_503: {_safe_error_detail(response)}",
        }
    if response.status_code != 200:
        return {"outcome": "error", "detail": f"sautai_error_{response.status_code}: {_safe_error_detail(response)}"}

    try:
        body = response.json()
    except ValueError:
        return {"outcome": "error", "detail": "invalid_response: non-JSON body"}

    sautai_user_id = body.get("sautai_user_id") if isinstance(body, dict) else None
    if not isinstance(sautai_user_id, int) or isinstance(sautai_user_id, bool):
        return {"outcome": "error", "detail": "invalid_response: missing sautai_user_id"}
    echoed_tenant_id = body.get("nbhd_tenant_id")
    if echoed_tenant_id != nbhd_tenant_id:
        return {"outcome": "error", "detail": "invalid_response: nbhd_tenant_id echo mismatch"}
    return {
        "outcome": "ok",
        "sautai_user_id": sautai_user_id,
        "email": str(body.get("email") or ""),
        "nbhd_tenant_id": echoed_tenant_id,
    }
