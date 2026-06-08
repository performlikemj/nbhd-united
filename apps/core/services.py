"""Core services — meditation signal gathering, the render pipeline, and notify.

Split per the project invariant "backend computes evidence, LLM makes judgments":
``gather_meditation_signals`` returns RAW signals (no scores/formulas); the
assistant weighs them into a render manifest; ``render_meditation`` is the
deterministic executor (segment-and-stitch via Gemini TTS + ffmpeg — see
``apps.core.render``). ``notify_meditation_ready`` sends the cheap "it's ready"
ping to the linked channel.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.core import compose, render
from apps.core.models import CoreProfile, MeditationSession, MeditationStatus
from apps.orchestrator.azure_client import upload_workspace_file_binary
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Audio bytes live on the per-tenant Azure File Share; rows live in Postgres
# (never SQLite on the share — the fleet-corruption invariant). Binary writes
# bypass the SMB text-sanitize chokepoint, which is correct for mp3/ogg.
_MEDITATION_DIR = "workspace/meditations"
_READY_JOB_NAME = "_core:ready"


def gather_meditation_signals(tenant: Tenant) -> dict:
    """Raw, consented signals the LLM draws on to compose today's meditation.

    Phase 1 = in-app, user-consented signals only: the user's own
    ``CoreProfile.additional_context`` (which they typed for exactly this) and the
    last meditation's theme (to vary from). Richer journal/insights/fuel/finance
    signals are a deliberate follow-up — they'd add a new journal→OpenRouter egress
    that needs a PII-egress sign-off first. Returns RAW evidence; the LLM
    (``apps.core.compose``), not a backend formula, makes the judgment.
    """
    signals: dict = {"tenant_id": str(tenant.id)}
    try:
        profile = CoreProfile.objects.filter(tenant=tenant).first()
        if profile:
            if profile.additional_context.strip():
                signals["additional_context"] = profile.additional_context.strip()[:800]
            # The user's chosen length drives the compose target + the render-time
            # duration ceiling, so a sit lands near what they asked for.
            signals["preferred_duration_minutes"] = profile.preferred_duration_minutes
    except Exception:
        logger.debug("gather_meditation_signals: profile read failed", exc_info=True)
    try:
        last = (
            MeditationSession.objects.filter(tenant=tenant, status=MeditationStatus.READY)
            .order_by("-date", "-created_at")
            .first()
        )
        if last and (last.theme or "").strip():
            signals["last_meditation_theme"] = last.theme.strip()[:200]
    except Exception:
        logger.debug("gather_meditation_signals: last-meditation read failed", exc_info=True)
    return signals


def compose_meditation(session: MeditationSession) -> None:
    """Author a pending session's manifest via the LLM, then render it.

    The web orb's compose flow: gather signals → LLM authors the manifest
    (judgment) → persist it → ``render_meditation`` (deterministic execution).
    Authoring failure is terminal for this session (a retry won't help a refusal /
    invalid manifest); the manifest save uses the reconnect-safe path because the
    LLM call is itself a multi-second no-DB gap.
    """
    sid = str(session.id)
    try:
        signals = gather_meditation_signals(session.tenant)
        manifest = compose.author_manifest(signals, voice=session.voice)
    except compose.ComposeError as exc:
        logger.warning("compose_meditation: session %s authoring failed: %s", sid[:8], str(exc)[:160])
        _fail(session, f"compose_error: {exc}")
        return

    session.manifest = manifest
    session.title = str(manifest.get("title", ""))[:160]
    session.theme = str(manifest.get("theme", ""))
    _save_session(session, ["manifest", "title", "theme", "updated_at"])

    # Render it (claims PENDING→RENDERING, validates, renders, persists, notifies).
    render_meditation(session)


def render_meditation(session: MeditationSession) -> None:
    """Render a session's manifest to audio and flip it to ``ready``.

    Pipeline: claim the session (idempotency) → validate → render narration
    (bounded-parallel TTS, per-call timeout, retry, non-fatal silence fallback)
    → stitch silences + transcode (mp3 + ogg) → store on the per-tenant share →
    set ``audio_url`` / ``ogg_url`` / ``duration_ms`` / ``guidance_text`` →
    ``status=ready`` → notify the linked channel.

    Failure modes:
      * invalid manifest / missing key / quota → terminal: ``status=failed`` and
        return normally (a QStash retry can never succeed, so don't 500 into a
        retry storm);
      * transient render error (ffmpeg/network) → ``status=failed`` AND re-raise
        so QStash retries; the next attempt re-claims the ``failed`` row.
    """
    sid = str(session.id)

    # ---- idempotency claim (finance-style guard against QStash double-fire) ----
    # Atomically take ownership: a PENDING/FAILED row — or a RENDERING row whose
    # claim has gone stale (its worker was killed mid-render, e.g. at the gunicorn
    # boundary) — transitions to RENDERING. A live concurrent render keeps the row
    # fresh and is left alone, so a slow render is never duplicated/re-billed; a
    # dead one is recoverable instead of permanently wedged. We set ``updated_at``
    # explicitly because ``.update()`` bypasses ``auto_now`` — that timestamp is
    # the staleness clock.
    stale_minutes = int(getattr(settings, "CORE_RENDER_STALE_MINUTES", 15) or 15)
    stale_cutoff = timezone.now() - timedelta(minutes=stale_minutes)
    claimed = MeditationSession.objects.filter(
        Q(id=session.id)
        & (
            Q(status__in=[MeditationStatus.PENDING, MeditationStatus.FAILED])
            | Q(status=MeditationStatus.RENDERING, updated_at__lt=stale_cutoff)
        )
    ).update(status=MeditationStatus.RENDERING, error="", updated_at=timezone.now())
    if not claimed:
        logger.info("render_meditation: session %s not claimable (status=%s) — skipping", sid[:8], session.status)
        return
    session.refresh_from_db()

    # ---- validate before any TTS spend; a bad manifest is terminal ----
    errors = render.validate_manifest(session.manifest)
    if errors:
        logger.warning("render_meditation: session %s invalid manifest: %s", sid[:8], errors[:3])
        _fail(session, "invalid_manifest: " + "; ".join(errors))
        return

    voice = (
        session.voice
        or (session.manifest.get("voice") if isinstance(session.manifest, dict) else "")
        or render.DEFAULT_VOICE
    )
    model = getattr(settings, "GEMINI_TTS_MODEL", "") or render.DEFAULT_MODEL
    api_key = getattr(settings, "GEMINI_API_KEY", "") or ""
    concurrency = int(getattr(settings, "CORE_RENDER_CONCURRENCY", 4) or 4)

    if not api_key:
        # Config error — terminal; a retry won't conjure a key. Surface clearly.
        logger.error("render_meditation: session %s has no GEMINI_API_KEY configured", sid[:8])
        _fail(session, "GEMINI_API_KEY not configured")
        return

    try:
        result = render.render_manifest_to_audio(
            session.manifest,
            voice=voice,
            model=model,
            api_key=api_key,
            concurrency=concurrency,
            deadline_seconds=float(getattr(settings, "CORE_RENDER_DEADLINE_SECONDS", render.DEFAULT_RENDER_DEADLINE_S)),
            want_ogg=True,
        )
    except render.ManifestError as exc:
        _fail(session, f"manifest: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — transient: mark failed, then re-raise for QStash retry
        logger.exception("render_meditation: session %s render failed (will retry)", sid[:8])
        _fail(session, f"render_error: {exc}")
        raise

    # If most narration was rate-limited away (low Gemini tier per-minute cap),
    # don't ship a near-silent file — fail clearly so the cause is actionable.
    if result.speech_count and result.quota_failed_count > result.speech_count // 2:
        logger.warning(
            "render_meditation: session %s mostly rate-limited (%d/%d segments) — failing",
            sid[:8],
            result.quota_failed_count,
            result.speech_count,
        )
        _fail(
            session,
            f"tts_quota: {result.quota_failed_count}/{result.speech_count} segments rate-limited "
            "(raise the Gemini tier, lower CORE_RENDER_CONCURRENCY, or use a leaner manifest)",
        )
        return

    # ---- persist audio to the per-tenant share, then flip to ready ----
    # A failure here (transient Azure SMB throttle/timeout, or the final save)
    # must follow the same FAILED-then-reraise contract as the render branch —
    # otherwise the row is stranded at RENDERING with no audio and the claim
    # filter can't re-take it. Re-render on retry is safe: it overwrites the same
    # share paths.
    try:
        tenant_id = str(session.tenant_id)
        mp3_name = f"{sid}.mp3"
        upload_workspace_file_binary(tenant_id, f"{_MEDITATION_DIR}/{mp3_name}", result.mp3_bytes)
        api_base = (getattr(settings, "API_BASE_URL", "") or "").rstrip("/")
        audio_url = f"{api_base}/api/v1/meditations/{tenant_id}/{mp3_name}"

        ogg_url = ""
        if result.ogg_bytes:
            ogg_name = f"{sid}.ogg"
            upload_workspace_file_binary(tenant_id, f"{_MEDITATION_DIR}/{ogg_name}", result.ogg_bytes)
            ogg_url = f"{api_base}/api/v1/meditations/{tenant_id}/{ogg_name}"

        session.audio_url = audio_url
        session.ogg_url = ogg_url
        session.duration_ms = result.duration_ms
        session.guidance_text = result.guidance_text
        session.model = model
        session.voice = voice
        session.status = MeditationStatus.READY
        session.error = ""
        _save_session(
            session,
            [
                "audio_url",
                "ogg_url",
                "duration_ms",
                "guidance_text",
                "model",
                "voice",
                "status",
                "error",
                "updated_at",
            ],
        )
    except Exception as exc:  # noqa: BLE001 — transient persist failure: FAILED, then retry
        logger.exception("render_meditation: session %s persist failed (will retry)", sid[:8])
        _fail(session, f"persist_error: {exc}")
        raise
    logger.info(
        "render_meditation: session %s ready (%.1fs, %d segs, %d fallback)",
        sid[:8],
        result.duration_ms / 1000.0,
        result.speech_count,
        result.failed_count,
    )

    # Observability: the manifest is rejected pre-render when its ESTIMATE blows
    # past the target, but TTS length varies — flag a ready sit that still ran
    # long against its target so a drift in the composer is visible in logs.
    try:
        target_s = float(session.manifest.get("total_target_seconds") or 0) if isinstance(session.manifest, dict) else 0
    except (TypeError, ValueError):
        target_s = 0
    if target_s and result.duration_ms > target_s * 1000 * render.DURATION_TARGET_TOLERANCE:
        logger.warning(
            "render_meditation: session %s ran long (%.0fs vs target %.0fs)",
            sid[:8],
            result.duration_ms / 1000.0,
            target_s,
        )

    # ---- notify the linked channel (non-fatal: audio is already stored) ----
    try:
        notify_meditation_ready(session)
    except Exception:
        logger.warning("render_meditation: notify failed for session %s (audio already ready)", sid[:8], exc_info=True)


def _fail(session: MeditationSession, message: str) -> None:
    session.status = MeditationStatus.FAILED
    session.error = message[:480]
    _save_session(session, ["status", "error", "updated_at"])


def _save_session(session: MeditationSession, update_fields: list[str]) -> None:
    """Persist a post-render status change, recovering from a render-killed DB connection.

    The render does no DB work for minutes, so Postgres/Supabase kills the idle
    session; the first post-render write then fails with
    OperationalError/InterfaceError ("terminating connection due to idle-session
    timeout"). When that happens, drop the dead connection, re-establish the
    connection-scoped service-role RLS GUC on a fresh one (``trigger_task`` set it
    on the original connection; it's lost when the connection dies), and retry
    once. Without this the status update is lost and the row wedges at
    ``rendering`` forever. The retry only runs on an actual connection failure, so
    it's a no-op under normal operation (and in transactional tests).
    """
    from django.db import connection
    from django.db.utils import InterfaceError, OperationalError

    from apps.tenants.middleware import set_rls_context

    try:
        session.save(update_fields=update_fields)
    except (OperationalError, InterfaceError):
        logger.warning(
            "render_meditation: DB connection died during render — reconnecting + retrying save for %s",
            str(session.id)[:8],
        )
        connection.close()
        set_rls_context(service_role=True)
        session.save(update_fields=update_fields)


# ============================================================================
# Notify-on-ready — deterministic, all-channels (Telegram + LINE), non-fatal.
# Reuses the existing channel resolver, send helpers, PII rehydration, and
# ProactiveOutbound thread-continuity rather than re-implementing routing.
# ============================================================================


def notify_meditation_ready(session: MeditationSession) -> bool:
    """Send a short "your meditation is ready" ping to the tenant's channel.

    Returns True if a message was delivered. Audio is already stored, so any
    failure here is logged and swallowed — never propagated into the render.
    """
    tenant = session.tenant
    if tenant.status != Tenant.Status.ACTIVE:
        logger.info("Core notify skipped: tenant %s not active (%s)", str(tenant.id)[:8], tenant.status)
        return False

    user = getattr(tenant, "user", None)
    if user is None:
        return False

    from apps.router.cron_delivery import resolve_user_channel

    channel = resolve_user_channel(user)
    if channel is None:
        logger.info("Core notify skipped: tenant %s has no linked channel", str(tenant.id)[:8])
        return False

    message = _ready_message(session, tenant)

    if channel == "line":
        channel_user_id = getattr(user, "line_user_id", "") or ""
        delivered = _send_line_text(tenant, channel_user_id, message)
    else:
        chat_id = getattr(user, "telegram_chat_id", None)
        channel_user_id = str(chat_id or "")
        delivered = bool(chat_id) and _send_telegram_text(chat_id, message)

    if delivered and channel_user_id:
        try:
            from apps.router.proactive_context import record_proactive_outbound

            record_proactive_outbound(
                tenant=tenant,
                channel=channel,
                channel_user_id=channel_user_id,
                message_text=message,
                job_name=_READY_JOB_NAME,
            )
        except Exception:
            logger.debug("Core notify: proactive record failed", exc_info=True)

    return bool(delivered)


def _ready_message(session: MeditationSession, tenant: Tenant) -> str:
    title = (session.title or "").strip()
    # The title is assistant-authored and may carry PII placeholders ([PERSON_N]);
    # rehydrate before it reaches the channel (same boundary as CronDeliveryView).
    entity_map = getattr(tenant, "pii_entity_map", None)
    if title and entity_map:
        try:
            from apps.pii.redactor import rehydrate_text

            title = rehydrate_text(title, entity_map)
        except Exception:
            logger.debug("Core notify: title rehydrate failed", exc_info=True)

    headline = f'Your meditation "{title}" is ready 🧘' if title else "Your meditation is ready 🧘"
    frontend = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    link = f"\n\nOpen Core to listen: {frontend}/core" if frontend else ""
    return f"{headline}{link}"


def _send_telegram_text(chat_id: int, text: str) -> bool:
    from apps.router.services import send_telegram_message

    return send_telegram_message(chat_id, text)


def _send_line_text(tenant: Tenant, line_user_id: str, text: str) -> bool:
    if not line_user_id:
        return False
    access_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "")
    if not access_token:
        logger.warning("Core notify: LINE_CHANNEL_ACCESS_TOKEN not configured")
        return False

    import httpx

    messages = [{"type": "text", "text": text[:4900]}]
    try:
        resp = httpx.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"to": line_user_id, "messages": messages},
            timeout=10,
        )
    except Exception:
        logger.exception("Core notify: LINE push error")
        return False

    if not resp.is_success:
        logger.warning("Core notify: LINE push failed (%s): %s", resp.status_code, resp.text[:200])
        # Trip the fleet-wide quota gate if this is the monthly-cap 429.
        from apps.router.line_webhook import _maybe_trip_monthly_quota

        _maybe_trip_monthly_quota(resp.status_code, resp.text)
        return False

    # Record sent message ids so a user quote-reply attributes correctly.
    try:
        from apps.router.line_webhook import _record_line_outbound

        sent = (resp.json() or {}).get("sentMessages") or []
        _record_line_outbound(tenant, line_user_id, sent, messages)
    except Exception:
        logger.debug("Core notify: LINE outbound record failed", exc_info=True)
    return True
