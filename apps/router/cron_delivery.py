"""Endpoint for tenant agents to send messages to users via Django poller.

Used by cron jobs and proactive agent actions. Routes messages through
the central Telegram bot or LINE Push API, depending on user preference.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.eval_sink import suppresses_real_transport
from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Rate limit: max 20 messages per hour per tenant (prevents runaway cron loops)
RATE_LIMIT_PER_HOUR = 20

# In-memory rate tracking (reset on process restart, which is fine)
_rate_counts: dict[str, list[float]] = {}


def degraded_occurrence_key(*, tenant_id, job_name: str, fired_at: datetime) -> str:
    """Return the P0 receipt-hour identity for one proactive delivery."""
    if timezone.is_naive(fired_at):
        fired_at = fired_at.replace(tzinfo=UTC)
    hour = fired_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    material = f"{tenant_id}|{job_name or ''}|{hour.isoformat()}"
    return hashlib.sha256(material.encode()).hexdigest()


def _delivery_dedup_enabled(tenant) -> bool:
    configured = str(getattr(settings, "NBHD_DELIVERY_DEDUP_TENANTS", "") or "").strip()
    if not configured:
        return False
    if configured == "*":
        return True
    return str(tenant.id) in {value.strip() for value in configured.split(",") if value.strip()}


def _claim_delivery_attempt(*, tenant, occurrence_key: str, job_name: str, channel: str):
    """Claim a delivery key, or return the successful duplicate response."""
    from apps.router.models import DeliveryAttempt

    for _attempt in range(3):
        try:
            with transaction.atomic():
                claimed = DeliveryAttempt.objects.create(
                    tenant=tenant,
                    occurrence_key=occurrence_key,
                    job_name=(job_name or "")[:128],
                    channel=channel,
                )
            return claimed, None
        except IntegrityError:
            # The insert is the concurrency primitive. Lock the winner only for
            # inspection/release; the transport call happens after this commits.
            with transaction.atomic():
                prior = (
                    DeliveryAttempt.objects.select_for_update()
                    .filter(tenant=tenant, occurrence_key=occurrence_key)
                    .first()
                )
                if prior is None:
                    # A failed claimant may have released the row between our
                    # unique violation and lookup. Retry the insert.
                    continue
                if prior.state != DeliveryAttempt.State.FAILED:
                    logger.warning(
                        "delivery_duplicate_suppressed tenant=%s occurrence=%s channel=%s prior_state=%s",
                        str(tenant.id)[:8],
                        occurrence_key[:12],
                        channel,
                        prior.state,
                    )
                    return None, Response(
                        {
                            "status": "duplicate_suppressed",
                            "channel": channel,
                            "prior_state": prior.state,
                        }
                    )

                # A definitive failure is safe to retry. Delete and recreate
                # while holding the row lock so only one retry takes ownership.
                prior.delete()
                claimed = DeliveryAttempt.objects.create(
                    tenant=tenant,
                    occurrence_key=occurrence_key,
                    job_name=(job_name or "")[:128],
                    channel=channel,
                )
                return claimed, None

    raise RuntimeError("delivery claim changed repeatedly before it could be inspected")


def _response_excerpt(response: Response | None, *, truncate: bool = True) -> str:
    if response is None:
        return ""
    data = getattr(response, "data", None)
    try:
        rendered = json.dumps(data, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(data)
    return rendered[:500] if truncate else rendered


def _resolve_delivery_attempt(attempt, *, state: str, response: Response | None = None, excerpt: str = "") -> None:
    if attempt is None:
        return

    from apps.pii.egress import redact_known_values
    from apps.router.models import DeliveryAttempt

    flag_on = attempt.tenant.layer1_placeholder_writes
    # The legacy response path sliced before its known-value scrub. Keep that
    # exact order while allowing the flag-on path to see the complete value:
    # author_text must replace a name that crosses character 500 *before* its
    # model-aware, placeholder-safe length cap is applied.
    raw_excerpt = excerpt or _response_excerpt(response, truncate=not flag_on)

    from apps.pii.authoring import author_text, receipt_placeholders, truncate_placeholder_safe

    if not flag_on:
        # Keep A4 byte compatibility with the pre-P3 seam: known-value scrub,
        # then an ordinary 500-character slice.
        raw_excerpt = redact_known_values(
            attempt.tenant,
            raw_excerpt,
            seam="cron_delivery_response_excerpt",
        )
    authored = author_text(
        attempt.tenant,
        raw_excerpt,
        seam="cron_delivery_response_excerpt",
        writer="background",
        field="response_excerpt",
        model_label="router.DeliveryAttempt",
    )
    # Response bodies can already exceed the CharField limit before authoring,
    # so author_text's growth-only cap intentionally does not shorten them.
    # Apply this seam's legacy 500-char cap to the *authored* value and rebuild
    # offset-free receipt metadata from the exact final bytes.
    response_excerpt = truncate_placeholder_safe(authored.text, 500) if flag_on else authored.text[:500]
    receipt = dict(authored.receipt)
    if "redactions" in receipt:
        receipt["redactions"] = receipt_placeholders(response_excerpt)
    DeliveryAttempt.objects.filter(pk=attempt.pk, state=DeliveryAttempt.State.CLAIMED).update(
        state=state,
        resolved_at=timezone.now(),
        response_excerpt=response_excerpt,
        pii_receipts={"response_excerpt": receipt},
    )
    if state == DeliveryAttempt.State.AMBIGUOUS:
        logger.error(
            "delivery_outcome_ambiguous tenant=%s occurrence=%s channel=%s response=%s",
            str(attempt.tenant_id)[:8],
            attempt.occurrence_key[:12],
            attempt.channel,
            response_excerpt or "-",
        )


def _with_delivery_state(response: Response, state: str) -> Response:
    """Attach transport certainty for the shared post-send resolver."""
    response._nbhd_delivery_state = state
    return response


def _check_rate_limit(tenant_id: str) -> bool:
    """Return True if under rate limit."""
    import time

    now = time.time()
    cutoff = now - 3600
    counts = _rate_counts.get(tenant_id, [])
    counts = [t for t in counts if t > cutoff]
    _rate_counts[tenant_id] = counts
    return len(counts) < RATE_LIMIT_PER_HOUR


def _record_send(tenant_id: str) -> None:
    import time

    _rate_counts.setdefault(tenant_id, []).append(time.time())


def _is_morning_briefing_send(*, tenant, job_name: str) -> bool:
    try:
        normalized_job_name = str(job_name or "").strip()
    except Exception:
        return False
    if normalized_job_name.casefold() == "morning briefing":
        return True
    if not normalized_job_name:
        return False

    try:
        from django.db.models import CharField, Q
        from django.db.models.fields.json import KeyTextTransform
        from django.db.models.functions import Cast

        from apps.cron.models import CronJob

        row_matches = (
            CronJob.objects.filter(
                tenant=tenant,
                name="Morning Briefing",
            )
            .annotate(
                row_id_text=Cast("id", output_field=CharField()),
                data_id_text=KeyTextTransform("id", "data"),
            )
            .filter(
                Q(row_id_text=normalized_job_name)
                | Q(gateway_job_id=normalized_job_name)
                | Q(data_id_text=normalized_job_name)
            )
            .exists()
        )
    except Exception:
        row_matches = False
    if row_matches:
        return True

    try:
        snapshot = tenant.cron_jobs_snapshot
        if not isinstance(snapshot, dict):
            return False
        jobs = snapshot.get("jobs") or []
        if not isinstance(jobs, list):
            return False
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if str(job.get("id") or "") != normalized_job_name:
                continue
            if str(job.get("name") or "").strip().casefold() == "morning briefing":
                return True
    except Exception:
        return False
    return False


def resolve_user_channel(user) -> str | None:
    """Determine which channel to use for outbound / proactive messages to ``user``.

    Order (MJ direction — keep Telegram/LINE, but push toward the app when it's
    installed):

    0. explicit eval-sink tenant (``Tenant.is_eval_sink``) → ``"eval"``,
       regardless of every other linked surface. No real transport — the
       ``ProactiveOutbound`` evidence row IS the delivery (see
       ``CronDeliveryView``).
    1. iOS device token registered → ``"app"``. Proactive content lands in the
       app feed as the PRIMARY surface: the APNs push + the ``?since=`` feed row
       (both produced by ``record_proactive_outbound``) ARE the delivery, so the
       same content no longer ALSO arrives in Telegram/LINE for token-holders.
    2. else Telegram linked → ``"telegram"``.
    3. else LINE linked → ``"line"``.
    4. else ``None`` (no delivery surface at all).

    Telegram-before-LINE in the messaging fallback (mirroring
    ``_resolve_gate_channel``) preserves prior behavior for the only both-linked
    cohort in production. The old resolver honoured ``preferred_channel``
    (universally the "telegram" schema default), so a user with BOTH channels
    linked has always received proactive/outbound messages on Telegram — a
    line-first fallback would silently move their delivery surface to LINE and
    split it from where their interactive gates land.

    ``preferred_channel`` is deliberately NOT honoured. In production all rows
    carry the schema default ``"telegram"`` (nobody ever chose it — the frontend
    hook is dead code and iOS never shipped the control), so reading it would
    honour noise, not intent. The column is left in place but vestigial; see the
    PR description.

    Linked Telegram/LINE users WITHOUT the app are unaffected — they keep full
    two-way delivery via their linked channel. Only token-holders flip from
    telegram/line to the app.

    Module-level so backend proactive senders (e.g. Core notify-on-ready) route
    identically to ``CronDeliveryView`` without duplicating the logic.
    """
    # 0. Explicit eval-sink tenants FIRST — before the DeviceToken check, so a
    # stale or accidentally registered real transport (an APNs token, a linked
    # Telegram/LINE id) can never make an eval target emit. Eval-sink is an
    # explicit operational mode, independent of the broader ``is_synthetic``
    # business-aggregate flag.
    tenant = getattr(user, "tenant", None)
    if getattr(tenant, "is_eval_sink", False):
        return "eval"

    # 1. Prefer the app whenever an iOS device is registered.
    from apps.router.models import DeviceToken

    if DeviceToken.objects.filter(user=user).exists():
        return "app"

    # 2/3. No device — fall back to whichever messaging channel is linked so
    # linked users without the app keep working. Telegram before LINE so a
    # both-linked user keeps the delivery surface they've always had (see
    # docstring); line-only users still resolve to LINE.
    line_user_id = getattr(user, "line_user_id", None)
    telegram_chat_id = getattr(user, "telegram_chat_id", None)
    if telegram_chat_id:
        return "telegram"
    if line_user_id:
        return "line"

    # No linked surface and not an explicitly configured eval sink.
    #
    # A synthetic tenant has no phone and no chat account, so it used to fall
    # through to None → HTTP 422 no_channel_linked → CronDeliveryView returned
    # before record_proactive_outbound, and NOTHING was written. The consequence
    # was green theater: the eval-behavior tenant has zero ProactiveOutbound rows
    # ever recorded, and even its one PASSING reminder scenario delivered nothing —
    # the cron fired, 422'd, and no assertion could have caught it.
    #
    # The journey probe worked around this by planting a FAKE APNs DeviceToken
    # before every run so the "app" branch would resolve. That hack is
    # self-destroying: a successful delivery pushes to the fabricated token, APNs
    # rejects it as BadDeviceToken, and push_views PRUNES the row — so every pass
    # destroyed the channel for the next fire (prod runs 8→9 alternated
    # pass/fail forever). The sink removes the need for it entirely.
    #
    return None


class SendToUserSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=8192)
    parse_mode = serializers.ChoiceField(
        choices=["Markdown", "HTML", "plain"],
        default="Markdown",
        required=False,
    )


class CronDeliveryView(APIView):
    """Send a message to the tenant's user via Telegram or LINE.

    Auth: X-NBHD-Internal-Key + X-NBHD-Tenant-Id headers.
    Called by tenant OpenClaw containers (cron jobs, proactive messages).

    Routes to the user's preferred channel (or whichever is linked).
    Existing Telegram-only users are unaffected.
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request, tenant_id):
        received_at = timezone.now()

        # Auth
        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
                expected_tenant_id=str(tenant_id),
            )
        except InternalAuthError as exc:
            return Response(
                {"error": "internal_auth_failed", "detail": str(exc)},
                status=http_status.HTTP_401_UNAUTHORIZED,
            )

        # Resolve tenant
        tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
        if tenant is None:
            return Response({"error": "tenant_not_found"}, status=http_status.HTTP_404_NOT_FOUND)

        # Block delivery for suspended/inactive tenants (trial expired, payment lapsed)
        if tenant.status != Tenant.Status.ACTIVE:
            logger.info(
                "Cron delivery blocked: tenant %s status=%s (not active)",
                tenant_id,
                tenant.status,
            )
            # Return 200 to prevent QStash/cron retries — this is expected, not an error
            return Response(
                {
                    "status": "blocked",
                    "reason": "tenant_not_active",
                    "tenant_status": tenant.status,
                }
            )

        # Determine channel
        channel = self._resolve_channel(tenant.user)
        if channel is None:
            return Response(
                {
                    "error": "no_channel_linked",
                    "detail": "User has no Telegram/LINE link and no registered device.",
                },
                status=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Rate limit
        tid = str(tenant_id)
        if not _check_rate_limit(tid):
            return Response(
                {"error": "rate_limited", "detail": "Max 20 messages per hour."},
                status=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Validate payload
        serializer = SendToUserSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=http_status.HTTP_400_BAD_REQUEST)

        # As authored by the agent — PII-placeholder space ([PERSON_1]). Retained
        # so the at-rest copies (ProactiveOutbound.message_text, the LINE
        # quote-reply excerpt) are stored placeholder-space; only the copy
        # actually sent to the user is rehydrated.
        placeholder_message_text = serializer.validated_data["message"]
        parse_mode = serializer.validated_data.get("parse_mode", "Markdown")

        # Strip the generic marker before every transport, but retain its labels
        # in placeholder space on ProactiveOutbound. The row is cross-channel:
        # even a Telegram/LINE delivery can later render pills in the iOS feed.
        # Quick-replies parsing stays first to preserve its final-line contract;
        # journal-link extraction below is placement-tolerant on the remainder.
        from apps.router.journal_link import extract_journal_link
        from apps.router.quick_replies import extract_quick_replies

        placeholder_message_text, quick_replies = extract_quick_replies(
            placeholder_message_text, tenant_id=tenant.id, channel=f"cron_{channel}"
        )
        # The journal deep-link chip, unlike quick-replies, IS persisted for
        # proactive sends: it rides the ProactiveOutbound row and surfaces in the
        # iOS ?since= feed (cross-channel), so a Telegram-delivered morning report
        # still gets a tappable "View in Journal" chip when its author opens the
        # app. Strip the marker from the text sent on every channel (no chip
        # transport on Telegram/LINE), but keep the parsed link for the row below.
        placeholder_message_text, journal_link = extract_journal_link(
            placeholder_message_text, tenant_id=tenant.id, channel=f"cron_{channel}"
        )
        if journal_link is None:
            try:
                if _is_morning_briefing_send(
                    tenant=tenant,
                    job_name=request.headers.get("X-NBHD-Job-Name", ""),
                ):
                    from apps.common.tenant_tz import tenant_today
                    from apps.journal.models import Document

                    daily_slug = str(tenant_today(tenant))
                    if Document.objects.filter(
                        tenant=tenant,
                        kind=Document.Kind.DAILY,
                        slug=daily_slug,
                    ).exists():
                        journal_link = {
                            "kind": Document.Kind.DAILY,
                            "slug": daily_slug,
                            "title": "Morning Report",
                        }
                        logger.warning(
                            "briefing_missing_journal_link_fallback",
                            extra={"tenant_id": str(tenant.id)},
                        )
            except Exception:
                # Job identification and fallback lookup are best-effort. A
                # missing chip must never block the underlying delivery.
                pass

        # Final owner-facing integrity guard.
        entity_map = tenant.pii_entity_map
        from apps.router.reply_text import finalize_outbound_text

        message_text = finalize_outbound_text(
            placeholder_message_text,
            entity_map,
            tenant_id=tenant.id,
            channel=f"cron_{channel}",
        )

        # Log-only instrumentation: ASCII chart leakage when no marker emitted.
        from apps.router.output_guards import log_ascii_chart_leak

        log_ascii_chart_leak(message_text, tenant_id=tenant.id, channel=f"cron_{channel}")

        from apps.router.proactive_context import record_proactive_outbound

        job_name = request.headers.get("X-NBHD-Job-Name", "")
        delivery_attempt = None
        if _delivery_dedup_enabled(tenant):
            occurrence_key = degraded_occurrence_key(
                tenant_id=tenant.id,
                job_name=job_name,
                fired_at=received_at,
            )
            delivery_attempt, duplicate_response = _claim_delivery_attempt(
                tenant=tenant,
                occurrence_key=occurrence_key,
                job_name=job_name,
                channel=channel,
            )
            if duplicate_response is not None:
                return duplicate_response

        # The current runtime carries job_name but no full schedule occurrence
        # identity. P0's degraded receipt-hour claim above is intentionally
        # private to delivery; structured-artifact persistence keeps its existing
        # content fallback until the P1 runtime headers arrive.
        artifact_dedup_key = None

        # Route to appropriate channel
        if channel == "line":
            channel_user_id = tenant.user.line_user_id or ""
            try:
                resp = self._send_via_line(
                    tenant_id=tid,
                    line_user_id=channel_user_id,
                    message_text=message_text,
                    # Store the quote-reply excerpt in placeholder space, not the
                    # rehydrated body we actually push.
                    excerpt_override=placeholder_message_text,
                )
            except Exception as exc:
                _resolve_delivery_attempt(
                    delivery_attempt,
                    state="ambiguous",
                    excerpt=f"{type(exc).__name__}: {exc}",
                )
                raise
        elif channel == "app":
            # iOS-only user: there's no Telegram/LINE chat to send to — the
            # ProactiveOutbound row (the APNs push + the ?since= feed row it
            # produces) IS the delivery, not an audit trail of one that already
            # happened. So it has to be PERSISTED before we claim success.
            #
            # record_proactive_outbound swallows write failures and returns None
            # by design ("losing the audit row is a smaller wrong than 500ing the
            # cron tool call"). That trade is right for telegram/line — the send
            # already reached the user — but on app it would answer 200 "sent"
            # while delivering absolutely nothing. So record FIRST and gate the
            # response on the row: a lost write returns a retryable 5xx and QStash
            # runs the cron again.
            channel_user_id = str(tenant.user_id)
            try:
                row = record_proactive_outbound(
                    tenant=tenant,
                    channel=channel,
                    channel_user_id=channel_user_id,
                    # Placeholder-space at rest; record_proactive_outbound rehydrates
                    # only for the owner-facing iOS push it fires.
                    message_text=placeholder_message_text,
                    job_name=job_name,
                    # Parsed "View in Journal" deep-link (placeholder-space title);
                    # the ?since= feed rehydrates + renders it as a chip. None when
                    # the send carried no marker.
                    journal_link=journal_link,
                    quick_replies=quick_replies,
                    artifact_dedup_key=artifact_dedup_key,
                )
            except Exception as exc:
                _resolve_delivery_attempt(
                    delivery_attempt,
                    state="ambiguous",
                    excerpt=f"{type(exc).__name__}: {exc}",
                )
                raise
            if row is None:
                logger.error(
                    "Cron delivery (app): ProactiveOutbound write failed for tenant %s — "
                    "nothing was delivered; returning 503 so the cron retries",
                    tid,
                )
                response = Response(
                    {
                        "error": "app_delivery_not_recorded",
                        "detail": "Could not persist the app-feed row; nothing was delivered.",
                    },
                    status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                )
                _resolve_delivery_attempt(delivery_attempt, state="failed", response=response)
                return response
            _record_send(tid)
            response = Response({"status": "sent", "channel": "app"})
            _resolve_delivery_attempt(delivery_attempt, state="sent", response=response)
            return response
        elif channel == "eval":
            # EXPLICIT EVAL-SINK TENANT: no Telegram, LINE, or APNs call is made
            # (record_proactive_outbound returns before the push dispatcher for
            # this channel). The ProactiveOutbound row is internal evidence for
            # eval assertions and is excluded from operational history readers.
            #
            # This branch MUST sit above the telegram fallback below: that fallback
            # is an ``else``, so an unrecognised channel would silently attempt a
            # real Telegram send with an empty chat_id. A sink that leaks is worse
            # than no sink.
            #
            # Persist-first, like the app branch above: the evidence row is the
            # ONLY artifact of an eval delivery, so answering 200 "sent" on a lost
            # write would be green theater — the exact failure the sink exists to
            # kill. A lost write returns a retryable 5xx instead.
            channel_user_id = str(tenant.user_id)
            try:
                row = record_proactive_outbound(
                    tenant=tenant,
                    channel=channel,
                    channel_user_id=channel_user_id,
                    # Placeholder-space at rest; eval rows are never rehydrated or
                    # pushed anywhere.
                    message_text=placeholder_message_text,
                    job_name=job_name,
                    journal_link=journal_link,
                    quick_replies=quick_replies,
                    artifact_dedup_key=artifact_dedup_key,
                )
            except Exception as exc:
                _resolve_delivery_attempt(
                    delivery_attempt,
                    state="ambiguous",
                    excerpt=f"{type(exc).__name__}: {exc}",
                )
                raise
            if row is None:
                logger.error(
                    "Cron delivery (eval): ProactiveOutbound write failed for tenant %s — "
                    "no evidence was recorded; returning 503 so the cron retries",
                    tid,
                )
                response = Response(
                    {
                        "error": "eval_delivery_not_recorded",
                        "detail": "Could not persist the eval evidence row; nothing was recorded.",
                    },
                    status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                )
                _resolve_delivery_attempt(delivery_attempt, state="failed", response=response)
                return response
            _record_send(tid)
            # Counts + ids only — never the body (evals-directive INVARIANT #1).
            logger.info(
                "eval sink delivery: tenant=%s job=%s chars=%d",
                str(tenant.id)[:8],
                (job_name or "-")[:64],
                len(message_text),
            )
            response = Response({"status": "sent", "channel": "eval"})
            _resolve_delivery_attempt(delivery_attempt, state="sent", response=response)
            return response
        else:
            channel_user_id = str(tenant.user.telegram_chat_id or "")
            try:
                resp = self._send_via_telegram(
                    tenant_id=tid,
                    chat_id=tenant.user.telegram_chat_id,
                    message_text=message_text,
                    parse_mode=parse_mode,
                )
            except Exception as exc:
                _resolve_delivery_attempt(
                    delivery_attempt,
                    state="ambiguous",
                    excerpt=f"{type(exc).__name__}: {exc}",
                )
                raise

        # Telegram/LINE only from here — the app and eval channels recorded,
        # counted and returned above (their row IS the delivery/evidence, so it
        # can't be best-effort).

        # Count every successful send against the per-tenant hourly cap, so the
        # runaway-loop throttle covers all channels uniformly.
        if 200 <= resp.status_code < 300:
            _record_send(tid)

        from apps.router.models import DeliveryAttempt

        delivery_state = getattr(
            resp,
            "_nbhd_delivery_state",
            DeliveryAttempt.State.SENT if 200 <= resp.status_code < 300 else DeliveryAttempt.State.FAILED,
        )
        _resolve_delivery_attempt(delivery_attempt, state=delivery_state, response=resp)

        # Record the outbound for thread-continuity on the next inbound.
        # This is the deterministic replacement for the LLM-mediated
        # ``_phase2_sync_block`` path — see apps.router.proactive_context.
        # Best-effort here BY DESIGN: the message already reached the user on
        # telegram/line, so a lost row must not 5xx a delivery that happened.
        if 200 <= resp.status_code < 300 and channel_user_id:
            record_proactive_outbound(
                tenant=tenant,
                channel=channel,
                channel_user_id=channel_user_id,
                # Placeholder-space at rest; record_proactive_outbound rehydrates
                # only for the owner-facing iOS push it fires.
                message_text=placeholder_message_text,
                job_name=job_name,
                # Parsed "View in Journal" deep-link (placeholder-space title);
                # the ?since= feed rehydrates + renders it as a chip. None when
                # the send carried no marker.
                journal_link=journal_link,
                quick_replies=quick_replies,
                artifact_dedup_key=artifact_dedup_key,
            )

        return resp

    def _resolve_channel(self, user) -> str | None:
        """Determine which channel to use for outbound messages.

        Thin wrapper over the module-level ``resolve_user_channel`` (shared with
        backend proactive senders).
        """
        return resolve_user_channel(user)

    def _send_via_telegram(
        self,
        *,
        tenant_id: str,
        chat_id: int,
        message_text: str,
        parse_mode: str,
    ) -> Response:
        """Send via Telegram Bot API."""
        tenant_obj = Tenant.objects.filter(id=tenant_id).first()
        if tenant_obj is not None and suppresses_real_transport(tenant_obj):
            logger.error("eval-sink transport block: tenant=%s transport=telegram", tenant_obj.id)
            return _with_delivery_state(
                Response({"status": "blocked", "reason": "eval_sink"}),
                "failed",
            )
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            logger.error("TELEGRAM_BOT_TOKEN not configured for cron delivery")
            return _with_delivery_state(
                Response(
                    {"error": "telegram_not_configured"},
                    status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                ),
                "failed",
            )

        api_base = f"https://api.telegram.org/bot{bot_token}"
        sent_count = 0

        from apps.router.telegram_format import (
            markdown_to_plaintext,
            render_telegram_html,
            strip_telegram_html,
        )

        # Render the assistant's markdown into Telegram HTML (bold headings,
        # aligned tables, anchors — no visible markdown). ``parse_mode="plain"``
        # opts out and sends markdown-stripped text.
        if parse_mode == "plain":
            chunks = _split_message(markdown_to_plaintext(message_text))
            html_mode = False
        else:
            chunks = render_telegram_html(message_text) or _split_message(message_text)
            html_mode = True

        try:
            with httpx.Client(timeout=10) as http:
                for chunk in chunks:
                    payload: dict = {"chat_id": chat_id, "text": chunk}
                    if html_mode:
                        payload["parse_mode"] = "HTML"

                    resp = http.post(f"{api_base}/sendMessage", json=payload)

                    if resp.status_code == 400 and html_mode:
                        # HTML rejected — retry as tag-free text (no markdown).
                        resp = http.post(
                            f"{api_base}/sendMessage",
                            json={"chat_id": chat_id, "text": strip_telegram_html(chunk)},
                        )

                    if not resp.is_success:
                        logger.warning(
                            "Cron delivery sendMessage failed (%s): %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                        state = "ambiguous" if sent_count or resp.status_code >= 500 else "failed"
                        return _with_delivery_state(
                            Response(
                                {"error": "telegram_send_failed", "detail": resp.text[:200]},
                                status=http_status.HTTP_502_BAD_GATEWAY,
                            ),
                            state,
                        )
                    sent_count += 1

        except httpx.HTTPError as exc:
            logger.exception("Cron delivery Telegram HTTP error for tenant %s", tenant_id)
            return _with_delivery_state(
                Response(
                    {"error": "telegram_send_failed", "detail": str(exc)[:200]},
                    status=http_status.HTTP_502_BAD_GATEWAY,
                ),
                "ambiguous",
            )

        logger.info(
            "Cron delivery (telegram): tenant=%s chat_id=%s chunks=%d",
            tenant_id,
            chat_id,
            sent_count,
        )
        return _with_delivery_state(
            Response({"status": "sent", "channel": "telegram", "chunks": sent_count}),
            "sent",
        )

    def _send_via_line(
        self,
        *,
        tenant_id: str,
        line_user_id: str,
        message_text: str,
        excerpt_override: str | None = None,
    ) -> Response:
        """Send via LINE Push Message API with branded Flex messages.

        ``message_text`` is the rehydrated body sent to the user; when
        ``excerpt_override`` is given it is the placeholder-space copy stored as
        the quote-reply excerpt (so ``LineOutboundMessage.text_excerpt`` holds no
        real names).
        """
        tenant_obj = Tenant.objects.filter(id=tenant_id).first()
        if tenant_obj is not None and suppresses_real_transport(tenant_obj):
            logger.error("eval-sink transport block: tenant=%s transport=line", tenant_obj.id)
            return _with_delivery_state(
                Response({"status": "blocked", "reason": "eval_sink"}),
                "failed",
            )
        access_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "")
        if not access_token:
            logger.error("LINE_CHANNEL_ACCESS_TOKEN not configured for cron delivery")
            return _with_delivery_state(
                Response(
                    {"error": "line_not_configured"},
                    status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                ),
                "failed",
            )

        import re

        from apps.router.line_flex import (
            attach_quick_reply,
            build_flex_bubble,
            extract_quick_reply_buttons,
        )
        from apps.router.line_webhook import _convert_tables, _strip_markdown

        # Extract quick reply buttons before processing
        clean_text, quick_reply_items = extract_quick_reply_buttons(message_text)

        # Pre-process: convert tables and strip code blocks
        clean_text = _convert_tables(clean_text)
        clean_text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", clean_text, flags=re.DOTALL)

        # Build Flex message
        try:
            flex_msg = build_flex_bubble(clean_text)
            if quick_reply_items:
                flex_msg = attach_quick_reply(flex_msg, quick_reply_items)
            messages = [flex_msg]
        except Exception:
            logger.debug("Cron Flex build failed, falling back to plain text", exc_info=True)
            plain = _strip_markdown(clean_text)
            plain = re.sub(r"\[\[button:[^\]]+\]\]", "", plain)
            plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
            chunks = _split_message(plain, max_len=5000)
            messages = [{"type": "text", "text": c} for c in chunks[:5]]

        from apps.router.line_webhook import _record_line_outbound

        sent_count = 0
        try:
            with httpx.Client(timeout=10) as http:
                # Send in batches of 5 (LINE limit)
                for i in range(0, len(messages), 5):
                    batch = messages[i : i + 5]
                    resp = http.post(
                        "https://api.line.me/v2/bot/message/push",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json",
                        },
                        json={"to": line_user_id, "messages": batch},
                    )
                    if not resp.is_success:
                        logger.warning(
                            "Cron delivery LINE push failed (%s): %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                        # Trip the fleet-wide quota gate if this is the
                        # monthly-cap 429 (vs a transient rate-limit).
                        from apps.router.line_webhook import _maybe_trip_monthly_quota

                        _maybe_trip_monthly_quota(resp.status_code, resp.text)
                        state = "ambiguous" if sent_count or resp.status_code >= 500 else "failed"
                        return _with_delivery_state(
                            Response(
                                {"error": "line_send_failed", "detail": resp.text[:200]},
                                status=http_status.HTTP_502_BAD_GATEWAY,
                            ),
                            state,
                        )
                    if tenant_obj:
                        try:
                            sent_messages = (resp.json() or {}).get("sentMessages") or []
                        except Exception:
                            sent_messages = []
                        _record_line_outbound(
                            tenant_obj, line_user_id, sent_messages, batch, excerpt_override=excerpt_override
                        )
                    sent_count += len(batch)

        except httpx.HTTPError as exc:
            logger.exception("Cron delivery LINE HTTP error for tenant %s", tenant_id)
            return _with_delivery_state(
                Response(
                    {"error": "line_send_failed", "detail": str(exc)[:200]},
                    status=http_status.HTTP_502_BAD_GATEWAY,
                ),
                "ambiguous",
            )

        logger.info(
            "Cron delivery (line): tenant=%s line_user=%s chunks=%d",
            tenant_id,
            line_user_id[:8] if line_user_id else "?",
            sent_count,
        )
        return _with_delivery_state(
            Response({"status": "sent", "channel": "line", "chunks": sent_count}),
            "sent",
        )


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a long message into chunks for Telegram."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut == -1:
            cut = remaining.rfind("\n", 0, max_len)
        if cut == -1:
            cut = remaining.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    return [c for c in chunks if c]
