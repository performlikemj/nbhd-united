"""Drive one real chat turn against a synthetic tenant and observe its terminal state.

This is the REUSABLE core of Probe 1 (chat round-trip) AND Probe 4
(hibernation-wake, PR-B4): it POSTs a message through the REAL user path

    POST /api/v1/chat/messages/  ->  ChatMessageView.post -> enqueue_tenant_turn
    -> AppChatMessage(PENDING) + PendingMessage(IOS) -> QStash drain -> the
    tenant's OpenClaw container -> reply stamped back onto the row

authenticated as the synthetic tenant's user via a long-lived PAT, then polls

    GET /api/v1/chat/messages/<client_msg_id>/  ->  ChatMessageDetailView

until the turn reaches a terminal state (``ready``/``error``) or the deadline
expires. It returns ONLY metadata — status / source / error / round-trip ms /
poll counts — and NEVER the reply text, the user text, or any content
(docs/evals-directive.md INVARIANT #1: nothing content-bearing enters the eval
pipeline; this driver reads ``status``/``source``/``error``/``created_at``/
``replied_at``/``waking_at``/``phase`` from the JSON and drops the rest).

The suite-specific PASS/FAIL judgment lives in the SUITE, not here — the
round-trip suite asserts ``status==ready AND error=="" AND source==tenant AND
round-trip<=SLO``; the wake suite additionally asserts ``waking_at`` flipped
non-null. So this driver stays assertion-agnostic and both probes share one
real-path implementation (``waking_at_seen``/``phase_seen`` are surfaced for B4).

INVARIANT #8 (no external call inside ``transaction.atomic()``): every httpx call
here runs OUTSIDE any transaction — the suite opens no ``atomic()`` around the
driver and only writes EvalResult rows AFTER this returns.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

import httpx
from django.conf import settings

from apps.evals.journey.targets import JourneyConfigError

logger = logging.getLogger(__name__)

# Round-trip probe defaults. SLO ~45s (a healthy warm turn replies well inside
# this); poll deadline ~90s, comfortably under the 300s gunicorn worker ceiling
# (docs/evals-wave-b-plan.md fact #2 — a task that brushes 300s is SIGKILL'd
# mid-run). PR-B4 (wake) passes its own larger values (cold start ~180s SLO /
# ~240s deadline).
DEFAULT_SLO_SECONDS = 45
DEFAULT_DEADLINE_SECONDS = 90
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
# Per-request HTTP timeout — bounds a single POST/GET so one wedged request
# can't consume the whole deadline. The DEADLINE governs how long we keep
# polling; this governs each individual call.
_HTTP_TIMEOUT_SECONDS = 15.0

_MESSAGES_PATH = "/api/v1/chat/messages/"
# AppChatMessage.Status terminal values (apps/router/models.py Status): a turn
# is done once it leaves PENDING.
_TERMINAL_STATUSES = frozenset({"ready", "error"})


@dataclass
class ObservedTurn:
    """Metadata-only observation of one driven chat turn. NEVER carries content.

    Every field is a status/code/count/duration — the reply text and user text
    read off the polled JSON are dropped on the floor, never stored here
    (INVARIANT #1).
    """

    client_msg_id: str
    http_ok: bool = False
    http_status: int | None = None
    # Where an HTTP-layer failure happened, for triage: "" (none) / "post" / "poll".
    failure_stage: str = ""
    # Last observed turn state (empty string until first successful read).
    status: str = ""
    source: str = ""
    error: str = ""
    # Server-authoritative round trip: (replied_at - created_at) in ms, computed
    # from the row's own timestamps so probe/DB clock skew can't distort the SLO.
    # None until a terminal row carries both timestamps.
    round_trip_ms: int | None = None
    # True if ``waking_at`` was ever non-null while the turn was still PENDING —
    # the positive wake signal PR-B4 asserts on (a warm reply leaves it null).
    waking_at_seen: bool = False
    # True if the container ever emitted a non-empty ``phase`` (liveness narration).
    phase_seen: bool = False
    # Reached a terminal status within the deadline.
    terminal: bool = False
    # Deadline expired with the turn still PENDING (assistant went silent — the
    # incident class this probe guards).
    timed_out: bool = False
    polls: int = 0
    elapsed_ms: int = 0


def resolve_base_url() -> str:
    """Return the control-plane's own public base URL, or RAISE ``JourneyConfigError``.

    The probe drives the REAL HTTP surface (not the Python view directly), so it
    needs the control plane's public origin. ``DJANGO_BASE_URL`` is exactly that
    — the deploy pipeline sets it alongside ``SENTRY_RELEASE`` and the daily
    ``reconcile_system_crons`` task already uses it to reach the control plane
    over HTTP (apps/cron/system_cron_registry.py). Unset ⇒ loud failure, never a
    silent skip (docs/evals-directive.md INVARIANT #3).
    """
    base_url = (getattr(settings, "DJANGO_BASE_URL", "") or "").rstrip("/")
    if not base_url:
        raise JourneyConfigError(
            "DJANGO_BASE_URL is not set — the chat probe cannot reach the control "
            "plane's own API. The deploy pipeline sets it alongside SENTRY_RELEASE."
        )
    return base_url


def resolve_journey_pat(tenant) -> str:
    """Return the raw journey PAT, or RAISE ``JourneyConfigError``.

    Beyond "is it set", this closes the same hole ``resolve_journey_tenant``
    closes for the tenant id: the PAT is the credential that actually drives
    traffic, so we verify it is VALID and belongs to the SAME synthetic journey
    tenant — refusing to POST a chat message as a real subscriber if the secret
    was mis-minted. A mismatch is a loud config failure, not a silent skip.
    """
    raw = getattr(settings, "EVAL_JOURNEY_PAT", "") or ""
    if not raw:
        raise JourneyConfigError(
            "EVAL_JOURNEY_PAT is not set — cannot authenticate the chat probe as the synthetic journey tenant's user."
        )

    from apps.tenants.pat_models import PersonalAccessToken, hash_token

    try:
        pat = PersonalAccessToken.objects.select_related("user", "user__tenant").get(token_hash=hash_token(raw))
    except PersonalAccessToken.DoesNotExist as exc:
        raise JourneyConfigError("EVAL_JOURNEY_PAT does not match any personal access token.") from exc
    if not pat.is_valid:
        raise JourneyConfigError("EVAL_JOURNEY_PAT is revoked or expired.")
    pat_tenant = getattr(pat.user, "tenant", None)
    if pat_tenant is None or pat_tenant.id != tenant.id:
        raise JourneyConfigError(
            "EVAL_JOURNEY_PAT belongs to a different tenant than EVAL_JOURNEY_TENANT_ID — "
            "refusing to drive a chat turn through the wrong account."
        )
    return raw


def _parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _absorb(observed: ObservedTurn, body: dict) -> None:
    """Fold a serialized-turn JSON body into ``observed`` — METADATA ONLY.

    Reads status/source/error/timestamps/waking_at/phase-presence. It never
    touches ``reply_text``/``user_text`` (or any other content key) so nothing
    content-bearing is ever carried out of this module (INVARIANT #1).
    """
    observed.status = str(body.get("status") or "")
    observed.source = str(body.get("source") or "")
    observed.error = str(body.get("error") or "")
    if body.get("waking_at"):
        observed.waking_at_seen = True
    if body.get("phase"):
        observed.phase_seen = True
    created = _parse_iso(body.get("created_at"))
    replied = _parse_iso(body.get("replied_at"))
    if created is not None and replied is not None:
        observed.round_trip_ms = int((replied - created).total_seconds() * 1000)
    observed.terminal = observed.status in _TERMINAL_STATUSES


def drive_chat_turn(
    *,
    base_url: str,
    pat: str,
    text: str,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    client_msg_id: str | None = None,
    client: httpx.Client | None = None,
) -> ObservedTurn:
    """POST one turn and poll it to a terminal state. Returns metadata only.

    The SLO is the SUITE's assertion — this driver never fails a
    slow-but-successful turn, it just observes and reports the round-trip ms.
    ``client`` may be injected (tests); otherwise a short-lived one is created
    and closed here. Every network call is outside any transaction (INVARIANT #8).
    """
    client_msg_id = client_msg_id or uuid.uuid4().hex
    observed = ObservedTurn(client_msg_id=client_msg_id)
    headers = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}
    post_url = f"{base_url}{_MESSAGES_PATH}"
    detail_url = f"{base_url}{_MESSAGES_PATH}{client_msg_id}/"

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
    started = time.monotonic()
    try:
        # 1) Inject the turn on the REAL path. A fresh client_msg_id is both the
        #    idempotency key and the poll key.
        try:
            resp = client.post(post_url, json={"client_msg_id": client_msg_id, "text": text}, headers=headers)
        except httpx.HTTPError:
            observed.failure_stage = "post"
            observed.elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.exception("chat_drive: POST %s failed", _MESSAGES_PATH)
            return observed
        observed.http_status = resp.status_code
        if resp.status_code not in (200, 201):
            observed.failure_stage = "post"
            observed.elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.error("chat_drive: POST returned HTTP %s (expected 200/201)", resp.status_code)
            return observed
        observed.http_ok = True
        _absorb(observed, resp.json())
        # A budget-exhausted turn (or any already-terminal POST body) never woke a
        # container — no point polling.
        if observed.terminal:
            observed.elapsed_ms = int((time.monotonic() - started) * 1000)
            return observed

        # 2) Poll the detail endpoint until terminal or the deadline.
        deadline = started + deadline_seconds
        while time.monotonic() < deadline:
            time.sleep(poll_interval_seconds)
            try:
                resp = client.get(detail_url, headers=headers)
            except httpx.HTTPError:
                # A transient poll error is not itself terminal — keep polling
                # until the deadline; only a never-terminal turn fails.
                observed.polls += 1
                observed.failure_stage = "poll"
                logger.warning("chat_drive: poll GET failed (will retry until deadline)")
                continue
            observed.polls += 1
            observed.http_status = resp.status_code
            if resp.status_code != 200:
                observed.failure_stage = "poll"
                logger.warning("chat_drive: poll returned HTTP %s", resp.status_code)
                continue
            observed.http_ok = True
            observed.failure_stage = ""
            _absorb(observed, resp.json())
            if observed.terminal:
                break

        if not observed.terminal:
            observed.timed_out = True
        observed.elapsed_ms = int((time.monotonic() - started) * 1000)
        return observed
    finally:
        if owns_client:
            client.close()
