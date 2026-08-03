"""sautai M2M client — the HTTP calls from NBHD Django to sautai.

Phase 0 (docs/sautai-phase0-contract.md): generate + current-plan.
Phase 0.5 (contract addendum v2): account linking via ``/link/resolve/`` and
addressing sautai by ``sautai_user_id`` (a linked account). Post-link data calls
never identify a user by email. See ``apps.integrations.runtime_views`` (proxy) and
``apps.integrations.link_views`` (console connect flow).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    Integration,
    SautaiMealPlanAddressedBy,
    SautaiMealPlanJob,
    SautaiMealPlanJobStatus,
)

logger = logging.getLogger(__name__)

# Transition safety: the server may still be synchronous, so the generate POST
# keeps its original timeout byte-for-byte until a valid HTTP 202 acknowledgement
# is followed by one valid signed status response. Later POSTs can then use the
# short acknowledgement timeout; status/current calls are always bounded by the
# poll deadline.
REQUEST_TIMEOUT_SECONDS = 125.0
ASYNC_GENERATE_TIMEOUT_SECONDS = 20.0
GENERATION_STATUS_TIMEOUT_SECONDS = 10.0
SAUTAI_POLL_DELAY_SECONDS = 15
SAUTAI_POLL_TIMEOUT_SECONDS = 10 * 60
SAUTAI_POLL_MAX_ATTEMPTS = SAUTAI_POLL_TIMEOUT_SECONDS // SAUTAI_POLL_DELAY_SECONDS

SAUTAI_GENERATE_POLL_PENDING = "poll_pending"
ASYNC_GENERATION_STATE_KEY = "_sautai_generation"
ASYNC_CONTRACT_CONFIRMED_KEY = "async_contract_confirmed"
ASYNC_CONTRACT_EVENT_KEY = "async_contract_capability"
ASYNC_CONTRACT_EVENT_AT_KEY = "async_contract_capability_observed_at"
ASYNC_CONTRACT_EVENT_VALIDATED = "validated"
ASYNC_CONTRACT_EVENT_LEGACY = "legacy"
ASYNC_CONTRACT_REQUEST_DECISION_KEY = "async_contract_request_enabled"
SAUTAI_POLL_TIMEOUT_ERROR = (
    f"sautai_poll_timeout: generation did not finish within {SAUTAI_POLL_TIMEOUT_SECONDS} seconds"
)

_ACTIVE_GENERATION_STATUSES = frozenset({"queued", "running"})
_SUCCESS_GENERATION_STATUSES = frozenset({"completed", "completed_with_failures"})

# The current-plan read + link resolve are fast synchronous calls a caller waits
# on inside a 20s budget, so they get short timeouts.
CURRENT_PLAN_TIMEOUT_SECONDS = 10.0
LINK_RESOLVE_TIMEOUT_SECONDS = 10.0

SAUTAI_LINK_REQUIRED_DETAIL = (
    "Connect your sautai account first from the sautai connection invitation in Fuel, then try again."
)


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
    """Return the tenant's linked ``sautai_user_id`` M2M identity, if present.

    Post-link data calls are consent-scoped: the tenant's
    ``Provider.SAUTAI`` Integration must carry a ``sautai_user_id`` captured by
    the connect-key flow. There is deliberately no email fallback. Returns the
    identity payload fragment plus the Integration row (so a caller can clear a
    stale link), or an empty dict when the account is not linked.
    """
    integration = Integration.objects.filter(tenant=tenant, provider=Integration.Provider.SAUTAI).first()
    if integration and integration.sautai_user_id:
        return {"sautai_user_id": integration.sautai_user_id}, integration
    return {}, integration


def clear_sautai_link(integration: Integration) -> None:
    """Null out a sautai account link (disconnect, or sautai lost the account)."""
    integration.sautai_user_id = None
    integration.linked_at = None
    integration.save(update_fields=["sautai_user_id", "linked_at", "updated_at"])


def sautai_async_contract_confirmed() -> bool:
    """Return the latest validated/reverted server capability observation.

    A 202 alone is deliberately not evidence. A ``validated`` event is written
    only after that acknowledgement decodes and the same remote job returns one
    valid signed status payload. A later valid legacy 200 writes a ``legacy``
    event, so rollback is immediate and durable. Event time is the generate
    response observation time; an old async job that polls after a newer legacy
    response therefore cannot accidentally undo the rollback.
    """
    latest: tuple[datetime, str, str] | None = None
    events = SautaiMealPlanJob.objects.filter(funnel__has_key=ASYNC_CONTRACT_EVENT_KEY).values_list("id", "funnel")
    for job_id, funnel in events.iterator():
        if not isinstance(funnel, dict):
            continue
        event = funnel.get(ASYNC_CONTRACT_EVENT_KEY)
        if event not in {ASYNC_CONTRACT_EVENT_VALIDATED, ASYNC_CONTRACT_EVENT_LEGACY}:
            continue
        observed_at = parse_datetime(str(funnel.get(ASYNC_CONTRACT_EVENT_AT_KEY) or ""))
        if observed_at is None or timezone.is_naive(observed_at):
            continue
        candidate = (observed_at, str(job_id), event)
        if latest is None or candidate[:2] > latest[:2]:
            latest = candidate
    return latest is not None and latest[2] == ASYNC_CONTRACT_EVENT_VALIDATED


def sautai_request_uses_async_contract(job: SautaiMealPlanJob) -> bool:
    """Return and persist the single capability decision for this POST.

    Runtime admission writes the decision before enqueueing. Older queued rows
    have no snapshot, so they conservatively retain the legacy contract rather
    than consulting a second, possibly changed source halfway through a request.
    """
    funnel = job.funnel if isinstance(job.funnel, dict) else {}
    decision = funnel.get(ASYNC_CONTRACT_REQUEST_DECISION_KEY)
    if isinstance(decision, bool):
        return decision
    decision = False
    job.funnel = {**funnel, ASYNC_CONTRACT_REQUEST_DECISION_KEY: decision}
    job.save(update_fields=["funnel", "updated_at"])
    return decision


def async_generation_state(job: SautaiMealPlanJob) -> dict | None:
    result = job.result if isinstance(job.result, dict) else {}
    state = result.get(ASYNC_GENERATION_STATE_KEY)
    return state if isinstance(state, dict) else None


def sautai_poll_generation_filter(poll_generation: int) -> dict:
    """JSON lookup used by every database CAS for one poll generation."""
    return {f"result__{ASYNC_GENERATION_STATE_KEY}__poll_generation": poll_generation}


def sautai_poll_deadline(state: dict) -> datetime | None:
    started_at = parse_datetime(str(state.get("started_at") or ""))
    if started_at is None or timezone.is_naive(started_at):
        return None
    return started_at + timedelta(seconds=SAUTAI_POLL_TIMEOUT_SECONDS)


def build_sautai_generate_payload(
    *,
    sautai_user_id: int,
    week_start,
    number_of_days: int,
    user_prompt: str,
    regenerate: bool,
) -> dict:
    """Build a generate request; NBHD never sends explicit ``replace_slots``."""
    payload: dict = {
        "sautai_user_id": sautai_user_id,
        "number_of_days": number_of_days,
    }
    if week_start is not None:
        payload["week_start"] = week_start.isoformat()
    if regenerate:
        payload["regenerate"] = True
    if user_prompt:
        payload["user_prompt"] = user_prompt
    return payload


def call_sautai_generate_plan(job: SautaiMealPlanJob, *, poll_generation: int | None = None) -> str | None:
    """Advance one bounded step of a sautai generation job.

    A new job POSTs ``generate/``. A legacy ``200`` body takes the unchanged
    synchronous success path. A ``202`` acknowledgement is persisted on this
    same NBHD row and returns :data:`SAUTAI_GENERATE_POLL_PENDING`; the task
    schedules a later delivery, which performs one short status GET. No call
    sleeps or loops in a worker.

    Active remote jobs return to local ``PENDING`` between polls so the task's
    existing atomic PENDING→GENERATING claim remains the overlap guard. Remote
    completion reads the materialized plan from ``current/`` and then uses the
    same READY persistence/notification path as the old synchronous response.
    """
    tenant = job.tenant
    result = job.result if isinstance(job.result, dict) else {}
    state = None
    if ASYNC_GENERATION_STATE_KEY in result:
        state = async_generation_state(job)
        if not isinstance(state, dict):
            _fail(job, "invalid_response: malformed persisted generation state")
            return
        if (
            not isinstance(poll_generation, int)
            or isinstance(poll_generation, bool)
            or state.get("poll_generation") != poll_generation
        ):
            logger.info("call_sautai_generate_plan: stale poll delivery skipped for job %s", str(job.id)[:8])
            return None
        deadline = sautai_poll_deadline(state)
        if deadline is None:
            _fail_generation(
                job,
                state,
                "invalid_response: malformed persisted generation state",
                poll_generation=poll_generation,
            )
            return None
        if timezone.now() >= deadline:
            _fail_generation(
                job,
                state,
                SAUTAI_POLL_TIMEOUT_ERROR,
                poll_generation=poll_generation,
            )
            return None

    identity, integration = sautai_identity(tenant)
    if not identity:
        if state is None:
            _fail(job, f"sautai_link_required: {SAUTAI_LINK_REQUIRED_DETAIL}")
        else:
            _fail_generation(
                job,
                state,
                f"sautai_link_required: {SAUTAI_LINK_REQUIRED_DETAIL}",
                poll_generation=poll_generation,
            )
        return

    base_url, secret = sautai_m2m_config()
    if not base_url or not secret:
        if state is None:
            _fail(job, "not_configured: SAUTAI_M2M_BASE_URL / SAUTAI_PLATFORM_SECRET missing")
        else:
            _fail_generation(
                job,
                state,
                "not_configured: SAUTAI_M2M_BASE_URL / SAUTAI_PLATFORM_SECRET missing",
                poll_generation=poll_generation,
            )
        return

    if state is not None:
        return _poll_sautai_generation(
            job,
            state=state,
            poll_generation=poll_generation,
            identity=identity,
            integration=integration,
            secret=secret,
        )

    # Minimal structured payload only — never raw conversation (research doc
    # §Egress posture). Post-link calls send only the stored sautai_user_id;
    # user_prompt may carry [PERSON_N] placeholders and IS rehydrated here, at
    # the deliberate egress point.
    prompt = job.user_prompt or ""
    if prompt:
        from apps.pii.redactor import rehydrate_for_tenant

        try:
            prompt = rehydrate_for_tenant(tenant, prompt)
        except Exception:
            logger.warning("call_sautai_generate_plan: prompt rehydrate failed for job %s", job.id, exc_info=True)

    payload = build_sautai_generate_payload(
        sautai_user_id=identity["sautai_user_id"],
        week_start=job.week_start,
        number_of_days=job.number_of_days,
        user_prompt=prompt,
        regenerate=job.regenerate,
    )

    async_contract_for_request = sautai_request_uses_async_contract(job)

    # Persist the linked numeric id used for this egress.
    job.addressed_by = SautaiMealPlanAddressedBy.LINKED_ID
    job.sautai_user_id = identity["sautai_user_id"]
    job.save(update_fields=["addressed_by", "sautai_user_id", "updated_at"])

    url = f"{base_url}/api/m2m/meal-plan/generate/"
    request_timeout = ASYNC_GENERATE_TIMEOUT_SECONDS if async_contract_for_request else REQUEST_TIMEOUT_SECONDS
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"X-NBHD-Platform-Secret": secret},
            timeout=request_timeout,
        )
    except httpx.HTTPError as exc:
        # Transport/timeout is always worth a redelivery (sautai cold-starting
        # right after un-pause is the common case).
        logger.warning("call_sautai_generate_plan: request failed for job %s: %s", job.id, exc)
        _fail_retryable(job, f"request_failed: {exc}")

    if response.status_code == 202:
        try:
            body = response.json()
        except ValueError:
            _fail(job, "invalid_response: sautai returned non-JSON acknowledgement")
            return
        state = _generation_state_from_ack(body, base_url=base_url)
        if state is None:
            _fail(job, "invalid_response: malformed async generation acknowledgement")
            return
        job.result = {ASYNC_GENERATION_STATE_KEY: state}
        job.status = SautaiMealPlanJobStatus.PENDING
        job.error = ""
        job.save(update_fields=["result", "status", "error", "updated_at"])
        return SAUTAI_GENERATE_POLL_PENDING

    if response.status_code != 200:
        detail = _safe_error_detail(response)
        logger.warning(
            "call_sautai_generate_plan: sautai returned %s for job %s: %s",
            response.status_code,
            job.id,
            detail,
        )
        response_code = _response_code(response)
        if response.status_code == 403 and response_code == "link_required":
            if integration is not None:
                clear_sautai_link(integration)
            _fail(job, f"sautai_link_required: {SAUTAI_LINK_REQUIRED_DETAIL}")
            return

        # Stale link: the linked sautai account no longer exists. Clear it and
        # fail with a reconnect hint. Terminal — the same dead id will never
        # resolve.
        if response.status_code == 404 and response_code == "unknown_user" and integration is not None:
            clear_sautai_link(integration)
            _fail(job, f"sautai_link_required: {SAUTAI_LINK_REQUIRED_DETAIL}")
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

    if not isinstance(body, dict) or not isinstance(body.get("plan"), dict):
        _fail(job, "invalid_response: missing plan")
        return

    funnel = _funnel_from_response(body)
    funnel[ASYNC_CONTRACT_REQUEST_DECISION_KEY] = async_contract_for_request
    if async_contract_for_request:
        observed_at = timezone.now().isoformat()
        funnel.update(
            {
                ASYNC_CONTRACT_CONFIRMED_KEY: False,
                ASYNC_CONTRACT_EVENT_KEY: ASYNC_CONTRACT_EVENT_LEGACY,
                ASYNC_CONTRACT_EVENT_AT_KEY: observed_at,
            }
        )
        logger.warning(
            "call_sautai_generate_plan: valid legacy 200 reverted async capability for job %s",
            str(job.id)[:8],
        )

    _complete_sautai_job(
        job,
        plan=body["plan"],
        web_link=body.get("web_link"),
        funnel=funnel,
    )


def _generation_state_from_ack(body: object, *, base_url: str) -> dict | None:
    """Decode the required 202 fields while ignoring unknown additive keys."""
    if not isinstance(body, dict):
        return None

    raw_job_id = body.get("job_id")
    status_url = body.get("status_url")
    regeneration = body.get("regeneration")
    if not isinstance(raw_job_id, str) or not isinstance(status_url, str) or not isinstance(regeneration, dict):
        return None
    try:
        remote_job_id = str(UUID(raw_job_id))
    except (TypeError, ValueError, AttributeError):
        return None
    expected_path = f"/api/m2m/generation-jobs/{remote_job_id}/"
    parsed_status_url = urlsplit(status_url)
    if parsed_status_url.scheme not in {"http", "https"} or parsed_status_url.path != expected_path:
        return None

    requested = regeneration.get("requested")
    mode = regeneration.get("mode")
    replace_slots = regeneration.get("replace_slots")
    if not isinstance(requested, bool) or not isinstance(mode, str) or not isinstance(replace_slots, list):
        return None

    # Never send the platform secret to a response-controlled origin. The
    # contract fixes the path, so retain the acknowledged id and reconstruct
    # the status URL against the configured sautai origin.
    canonical_status_url = f"{base_url}/api/m2m/generation-jobs/{remote_job_id}/"
    return {
        "job_id": remote_job_id,
        "status_url": canonical_status_url,
        "status": "accepted",
        "started_at": timezone.now().isoformat(),
        "poll_attempts": 0,
        "poll_generation": 1,
        "regeneration": dict(regeneration),
    }


def _poll_sautai_generation(
    job: SautaiMealPlanJob,
    *,
    state: dict,
    poll_generation: int,
    identity: dict,
    integration: Integration | None,
    secret: str,
) -> str | None:
    """Perform one status read and persist/re-enqueue/finalize its outcome."""
    status_url = state.get("status_url")
    deadline = sautai_poll_deadline(state)
    attempts = state.get("poll_attempts", 0)
    if (
        not isinstance(status_url, str)
        or not status_url
        or deadline is None
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
        or state.get("poll_generation") != poll_generation
    ):
        _fail_generation(
            job,
            state,
            "invalid_response: malformed persisted generation state",
            poll_generation=poll_generation,
        )
        return None

    now = timezone.now()
    remaining_seconds = (deadline - now).total_seconds()
    if remaining_seconds <= 0 or attempts >= SAUTAI_POLL_MAX_ATTEMPTS:
        _fail_generation(
            job,
            state,
            SAUTAI_POLL_TIMEOUT_ERROR,
            poll_generation=poll_generation,
        )
        return None

    state = dict(state)
    state["poll_attempts"] = attempts + 1
    state["last_polled_at"] = now.isoformat()

    try:
        response = httpx.get(
            status_url,
            headers={"X-NBHD-Platform-Secret": secret},
            timeout=min(GENERATION_STATUS_TIMEOUT_SECONDS, remaining_seconds),
        )
    except httpx.HTTPError as exc:
        logger.warning("poll_sautai_generation: request failed for job %s: %s", job.id, exc)
        if timezone.now() >= deadline:
            _fail_generation(
                job,
                state,
                SAUTAI_POLL_TIMEOUT_ERROR,
                poll_generation=poll_generation,
            )
            return None
        state["last_error"] = f"request_failed: {exc}"[:200]
        return _persist_pending_generation(
            job,
            state,
            deadline=deadline,
            poll_generation=poll_generation,
        )

    if timezone.now() >= deadline:
        _fail_generation(
            job,
            state,
            SAUTAI_POLL_TIMEOUT_ERROR,
            poll_generation=poll_generation,
        )
        return None

    if response.status_code == 429 or response.status_code >= 500:
        state["last_error"] = f"sautai_error_{response.status_code}: {_safe_error_detail(response)}"[:200]
        return _persist_pending_generation(
            job,
            state,
            deadline=deadline,
            poll_generation=poll_generation,
        )
    if response.status_code != 200:
        _fail_generation(
            job,
            state,
            f"sautai_status_error_{response.status_code}: {_safe_error_detail(response)}",
            poll_generation=poll_generation,
        )
        return None

    try:
        body = response.json()
    except ValueError:
        _fail_generation(
            job,
            state,
            "invalid_response: sautai returned non-JSON generation status",
            poll_generation=poll_generation,
        )
        return None

    decoded = _decode_generation_status(body)
    if decoded is None:
        _fail_generation(
            job,
            state,
            "invalid_response: malformed generation status",
            poll_generation=poll_generation,
        )
        return None

    if timezone.now() >= deadline:
        _fail_generation(
            job,
            state,
            SAUTAI_POLL_TIMEOUT_ERROR,
            poll_generation=poll_generation,
        )
        return None

    if not _record_validated_async_contract(
        job,
        state=state,
        poll_generation=poll_generation,
    ):
        return None

    state.update(decoded)
    state.pop("last_error", None)
    remote_status = decoded["status"]
    if remote_status in _ACTIVE_GENERATION_STATUSES:
        return _persist_pending_generation(
            job,
            state,
            deadline=deadline,
            poll_generation=poll_generation,
        )
    if remote_status == "failed":
        failed_count = len(decoded["failed_slots"])
        _fail_generation(
            job,
            state,
            f"sautai_generation_failed: remote job failed with {failed_count} failed slot(s)",
            poll_generation=poll_generation,
        )
        return None

    return _finalize_async_generation(
        job,
        state=state,
        deadline=deadline,
        poll_generation=poll_generation,
        identity=identity,
        integration=integration,
    )


def _decode_generation_status(body: object) -> dict | None:
    """Decode a status payload, tolerating unknown additive response keys."""
    if not isinstance(body, dict):
        return None
    remote_status = body.get("status")
    remaining_count = body.get("remaining_count")
    failed_slots = body.get("failed_slots")
    plan_id = body.get("plan_id")
    week_start_date = body.get("week_start_date")
    if (
        remote_status not in _ACTIVE_GENERATION_STATUSES | _SUCCESS_GENERATION_STATUSES | {"failed"}
        or not isinstance(remaining_count, int)
        or isinstance(remaining_count, bool)
        or remaining_count < 0
        or not isinstance(failed_slots, list)
        or not isinstance(plan_id, int)
        or isinstance(plan_id, bool)
        or plan_id <= 0
        or not isinstance(week_start_date, str)
    ):
        return None
    try:
        date.fromisoformat(week_start_date)
    except ValueError:
        return None
    if remote_status in _SUCCESS_GENERATION_STATUSES and remaining_count != 0:
        return None
    if remote_status == "completed" and failed_slots:
        return None
    if remote_status == "completed_with_failures" and not failed_slots:
        return None

    return {
        "status": remote_status,
        "remaining_count": remaining_count,
        "failed_slots": failed_slots,
        "plan_id": plan_id,
        "week_start_date": week_start_date,
    }


def _poll_cas_queryset(job: SautaiMealPlanJob, poll_generation: int):
    return SautaiMealPlanJob.objects.filter(
        id=job.id,
        status=SautaiMealPlanJobStatus.GENERATING,
        **sautai_poll_generation_filter(poll_generation),
    )


def _record_validated_async_contract(
    job: SautaiMealPlanJob,
    *,
    state: dict,
    poll_generation: int,
) -> bool:
    """Persist capability only after a decoded status response for this ack."""
    funnel = job.funnel if isinstance(job.funnel, dict) else {}
    request_decision = funnel.get(ASYNC_CONTRACT_REQUEST_DECISION_KEY)
    if not isinstance(request_decision, bool):
        # Pre-deployment queued rows had no admission snapshot. Treat their
        # original POST conservatively as legacy until this poll validates it.
        request_decision = False

    next_funnel = {
        **funnel,
        ASYNC_CONTRACT_REQUEST_DECISION_KEY: request_decision,
        ASYNC_CONTRACT_CONFIRMED_KEY: True,
    }
    if not request_decision and next_funnel.get(ASYNC_CONTRACT_EVENT_KEY) != ASYNC_CONTRACT_EVENT_VALIDATED:
        acknowledged_at = parse_datetime(str(state.get("started_at") or ""))
        if acknowledged_at is None or timezone.is_naive(acknowledged_at):
            return False
        next_funnel.update(
            {
                ASYNC_CONTRACT_EVENT_KEY: ASYNC_CONTRACT_EVENT_VALIDATED,
                ASYNC_CONTRACT_EVENT_AT_KEY: acknowledged_at.isoformat(),
            }
        )

    if next_funnel == funnel:
        return True
    updated = _poll_cas_queryset(job, poll_generation).update(funnel=next_funnel)
    if not updated:
        logger.info("record_validated_async_contract: stale lease skipped for job %s", str(job.id)[:8])
        return False
    job.funnel = next_funnel
    return True


def _persist_pending_generation(
    job: SautaiMealPlanJob,
    state: dict,
    *,
    deadline: datetime,
    poll_generation: int,
) -> str | None:
    if timezone.now() >= deadline:
        _fail_generation(
            job,
            state,
            SAUTAI_POLL_TIMEOUT_ERROR,
            poll_generation=poll_generation,
        )
        return None

    next_state = dict(state)
    next_state["poll_generation"] = poll_generation + 1
    result = {ASYNC_GENERATION_STATE_KEY: next_state}
    updated = _poll_cas_queryset(job, poll_generation).update(
        result=result,
        status=SautaiMealPlanJobStatus.PENDING,
        error="",
        updated_at=timezone.now(),
    )
    if not updated:
        logger.info("persist_pending_generation: stale lease skipped for job %s", str(job.id)[:8])
        return None
    job.result = result
    job.status = SautaiMealPlanJobStatus.PENDING
    job.error = ""
    return SAUTAI_GENERATE_POLL_PENDING


def _fail_generation(
    job: SautaiMealPlanJob,
    state: dict,
    message: str,
    *,
    poll_generation: int,
) -> bool:
    result = {ASYNC_GENERATION_STATE_KEY: state}
    error = message[:480]
    updated = _poll_cas_queryset(job, poll_generation).update(
        result=result,
        status=SautaiMealPlanJobStatus.FAILED,
        error=error,
        updated_at=timezone.now(),
    )
    if not updated:
        logger.info("fail_generation: stale lease skipped for job %s", str(job.id)[:8])
        return False
    job.result = result
    job.status = SautaiMealPlanJobStatus.FAILED
    job.error = error
    return True


def _finalize_async_generation(
    job: SautaiMealPlanJob,
    *,
    state: dict,
    deadline: datetime,
    poll_generation: int,
    identity: dict,
    integration: Integration | None,
) -> str | None:
    remaining_seconds = (deadline - timezone.now()).total_seconds()
    if remaining_seconds <= 0:
        _fail_generation(
            job,
            state,
            SAUTAI_POLL_TIMEOUT_ERROR,
            poll_generation=poll_generation,
        )
        return None

    week_start_iso = job.week_start.isoformat() if job.week_start else state.get("week_start_date")
    current = fetch_sautai_current_plan(
        identity=identity,
        week_start_iso=week_start_iso,
        timeout_seconds=min(CURRENT_PLAN_TIMEOUT_SECONDS, remaining_seconds),
    )
    if timezone.now() >= deadline:
        _fail_generation(
            job,
            state,
            SAUTAI_POLL_TIMEOUT_ERROR,
            poll_generation=poll_generation,
        )
        return None

    outcome = current.get("outcome")
    if outcome == "link_required":
        failed = _fail_generation(
            job,
            state,
            f"sautai_link_required: {SAUTAI_LINK_REQUIRED_DETAIL}",
            poll_generation=poll_generation,
        )
        if failed and integration is not None:
            clear_sautai_link(integration)
        return None
    if outcome == "not_configured":
        _fail_generation(
            job,
            state,
            "not_configured: SAUTAI_M2M_BASE_URL / SAUTAI_PLATFORM_SECRET missing",
            poll_generation=poll_generation,
        )
        return None
    if outcome != "ok":
        state["last_error"] = str(current.get("detail") or outcome or "current_plan_unavailable")[:200]
        return _persist_pending_generation(
            job,
            state,
            deadline=deadline,
            poll_generation=poll_generation,
        )

    funnel = dict(current.get("funnel") or {})
    contract_funnel = job.funnel if isinstance(job.funnel, dict) else {}
    for key in (
        ASYNC_CONTRACT_REQUEST_DECISION_KEY,
        ASYNC_CONTRACT_CONFIRMED_KEY,
        ASYNC_CONTRACT_EVENT_KEY,
        ASYNC_CONTRACT_EVENT_AT_KEY,
    ):
        if key in contract_funnel:
            funnel[key] = contract_funnel[key]
    failed_slots = state.get("failed_slots") if isinstance(state.get("failed_slots"), list) else []
    funnel.update(
        {
            "generation_status": state.get("status"),
            "remaining_count": state.get("remaining_count"),
            "failed_slots": failed_slots,
            "failed_slot_count": len(failed_slots),
            "plan_id": state.get("plan_id"),
            "week_start_date": state.get("week_start_date"),
            "regeneration": state.get("regeneration"),
        }
    )
    if timezone.now() >= deadline:
        _fail_generation(
            job,
            state,
            SAUTAI_POLL_TIMEOUT_ERROR,
            poll_generation=poll_generation,
        )
        return None
    _complete_sautai_job(
        job,
        plan=current["plan"],
        web_link=current.get("web_link"),
        funnel=funnel,
        poll_generation=poll_generation,
    )
    return None


def _complete_sautai_job(
    job: SautaiMealPlanJob,
    *,
    plan: dict,
    web_link: object,
    funnel: dict,
    poll_generation: int | None = None,
) -> bool:
    """Persist READY and fire the existing completion notification once."""
    saved_web_link = str(web_link or "")[:500]
    if poll_generation is None:
        job.result = plan
        job.web_link = saved_web_link
        job.funnel = funnel
        job.status = SautaiMealPlanJobStatus.READY
        job.error = ""
        job.save(update_fields=["result", "web_link", "funnel", "status", "error", "updated_at"])
    else:
        updated = _poll_cas_queryset(job, poll_generation).update(
            result=plan,
            web_link=saved_web_link,
            funnel=funnel,
            status=SautaiMealPlanJobStatus.READY,
            error="",
            updated_at=timezone.now(),
        )
        if not updated:
            logger.info("complete_sautai_job: stale lease skipped for job %s", str(job.id)[:8])
            return False
        job.result = plan
        job.web_link = saved_web_link
        job.funnel = funnel
        job.status = SautaiMealPlanJobStatus.READY
        job.error = ""

    try:
        from apps.integrations.sautai_notify import notify_sautai_plan_ready

        notify_sautai_plan_ready(job)
    except Exception:
        logger.warning(
            "call_sautai_generate_plan: notify failed for job %s (plan already ready)", job.id, exc_info=True
        )
    return True


def _funnel_from_response(body: dict) -> dict:
    """Extract persisted metadata from a generate/current 200 body.

    ``claim_link`` is present only when ``account_claimed`` is false (contract
    addendum #4); ``already_existed`` drives the "a plan already existed —
    regenerate?" honesty branch in the notify copy. ``complete`` and
    ``missing_days`` preserve sautai's top-level plan-integrity result so cached
    READY jobs can make the same repair and disclosure decisions as live reads.
    """
    return {
        "account_claimed": body.get("account_claimed"),
        "plan_count": body.get("plan_count"),
        "claim_link": str(body.get("claim_link") or "")[:500],
        "already_existed": body.get("already_existed"),
        "complete": body.get("complete"),
        "missing_days": body.get("missing_days"),
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


def fetch_sautai_current_plan(
    *,
    identity: dict,
    week_start_iso: str | None,
    timeout_seconds: float = CURRENT_PLAN_TIMEOUT_SECONDS,
) -> dict:
    """Synchronously read a user's current plan from sautai's ``/current/`` endpoint.

    ``identity`` must be the ``{"sautai_user_id": int}`` fragment returned by
    :func:`sautai_identity`. Email identities are rejected locally without an
    outbound call. Returns a small outcome dict the runtime view maps to an HTTP
    response; the HTTP call + contract-fixture parsing live here (unit-testable
    against the golden ``current_*.json`` fixtures), the cached-fallback policy
    in the view. Outcomes:

    - ``{"outcome": "ok", "plan": {...}, "web_link": "...", "funnel": {...}}``
    - ``{"outcome": "not_found"}`` — no plan (or unknown user_id); contract 404.
    - ``{"outcome": "link_required"}`` — no local link or sautai rejected it.
    - ``{"outcome": "not_configured"}`` — the M2M bridge env is unset (fail loud).
    - ``{"outcome": "error", "detail": "..."}`` — timeout / transport / non-200;
      the view falls back to the most recent READY job's cached plan.
    """
    sautai_user_id = identity.get("sautai_user_id") if isinstance(identity, dict) else None
    if not isinstance(sautai_user_id, int) or isinstance(sautai_user_id, bool) or sautai_user_id <= 0:
        return {"outcome": "link_required"}

    base_url, secret = sautai_m2m_config()
    if not base_url or not secret:
        return {"outcome": "not_configured"}

    payload: dict = {"sautai_user_id": sautai_user_id}
    if week_start_iso:
        payload["week_start"] = week_start_iso

    url = f"{base_url}/api/m2m/meal-plan/current/"
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"X-NBHD-Platform-Secret": secret},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        logger.warning("fetch_sautai_current_plan: request failed: %s", exc)
        return {"outcome": "error", "detail": f"request_failed: {exc}"}

    if response.status_code == 404:
        return {"outcome": "not_found"}
    if response.status_code == 403 and _response_code(response) == "link_required":
        return {"outcome": "link_required"}
    if response.status_code != 200:
        return {"outcome": "error", "detail": f"sautai_error_{response.status_code}: {_safe_error_detail(response)}"}

    try:
        body = response.json()
    except ValueError:
        return {"outcome": "error", "detail": "invalid_response: non-JSON body"}

    plan = body.get("plan") if isinstance(body, dict) else None
    if not isinstance(plan, dict):
        return {"outcome": "error", "detail": "invalid_response: missing plan"}

    funnel = _funnel_from_response(body)
    return {
        "outcome": "ok",
        "plan": plan,
        "web_link": str(body.get("web_link") or "")[:500],
        "complete": body.get("complete"),
        "missing_days": body.get("missing_days"),
        "funnel": funnel,
    }


def resolve_sautai_link_key(
    link_key: str,
    *,
    nbhd_tenant_id: str,
    account_email: str | None = None,
    display_name: str | None = None,
) -> dict:
    """Exchange a one-time connect key for a sautai user id + email via ``/link/resolve/``.

    Called SERVER-SIDE from the console connect endpoint (the raw key is never
    stored — one-time exchange, burn after resolve). Contract addendum #1: 200
    echoes the required opaque ``nbhd_tenant_id`` alongside ``sautai_user_id``
    and ``email``; unknown/expired/used key → 404 ``{"code":"invalid_key"}``.
    Optional account labels are display-only request metadata and are omitted
    unless supplied as non-empty strings.
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

    payload: dict = {"link_key": link_key, "nbhd_tenant_id": nbhd_tenant_id}
    if isinstance(account_email, str) and account_email:
        payload["nbhd_account_email"] = account_email[:255]
    if isinstance(display_name, str) and display_name:
        payload["nbhd_display_name"] = display_name[:255]

    url = f"{base_url}/api/m2m/link/resolve/"
    try:
        response = httpx.post(
            url,
            json=payload,
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
