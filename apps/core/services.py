"""Core services — meditation signal gathering, the render pipeline, and notify.

Split per the project invariant "backend computes evidence, LLM makes judgments":
``gather_meditation_signals`` returns RAW signals (no scores/formulas); the
assistant weighs them into a render manifest; ``render_meditation`` is the
deterministic executor (segment-and-stitch via Gemini TTS + ffmpeg — see
``apps.core.render``). ``notify_meditation_ready`` sends the cheap "it's ready"
ping to the linked channel.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core import compose, render
from apps.core.models import (
    CoreOnboardingStatus,
    CoreProfile,
    MeditationFailureClass,
    MeditationSession,
    MeditationStatus,
)
from apps.orchestrator.azure_client import download_workspace_file, upload_workspace_file_binary
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Audio bytes live on the per-tenant Azure File Share; rows live in Postgres
# (never SQLite on the share — the fleet-corruption invariant). Binary writes
# bypass the SMB text-sanitize chokepoint, which is correct for mp3/ogg.
_MEDITATION_DIR = "workspace/meditations"
_READY_JOB_NAME = "_core:ready"
_COMPOSE_CLAIM_MARKER = "compose_claim: active"
_COMPOSE_CLAIM_STALE_MINUTES = 10

# Forward-only onboarding ladder. DECLINED is off-ladder (a user opt-out we never
# auto-override). pending → in_progress (they engaged: saved a profile) →
# completed (they landed a first meditation). See advance_core_onboarding.
_ONBOARDING_RANK = {
    CoreOnboardingStatus.PENDING: 0,
    CoreOnboardingStatus.IN_PROGRESS: 1,
    CoreOnboardingStatus.COMPLETED: 2,
}


def advance_core_onboarding(profile: CoreProfile, target: str) -> bool:
    """Idempotently move a CoreProfile forward through onboarding.

    Never regresses (a higher status is left alone), never touches a DECLINED
    profile (an explicit opt-out), and is a no-op when already at/past ``target``.
    Returns True only when it actually advanced. Best-effort at call sites: the
    onboarding status is a nicety, never worth failing a save/render over.
    """
    if profile.onboarding_status == CoreOnboardingStatus.DECLINED:
        return False
    if _ONBOARDING_RANK.get(target, 0) <= _ONBOARDING_RANK.get(profile.onboarding_status, 0):
        return False
    profile.onboarding_status = target
    profile.save(update_fields=["onboarding_status", "updated_at"])
    return True


def _advance_onboarding_on_ready(tenant: Tenant) -> None:
    """A first successful (ready) meditation completes onboarding. Best-effort."""
    try:
        profile, _created = CoreProfile.objects.get_or_create(tenant=tenant)
        advance_core_onboarding(profile, CoreOnboardingStatus.COMPLETED)
    except Exception:
        logger.debug("core onboarding advance-on-ready failed", exc_info=True)


def gather_meditation_signals(tenant: Tenant) -> dict:
    """Raw, consented signals the LLM draws on to compose today's meditation.

    Returns RAW evidence; the LLM (``apps.core.compose``), not a backend formula,
    makes the judgment. All sources are in-app, consented, and best-effort:

    * ``CoreProfile.additional_context`` — free-text the user typed for this.
    * the last meditation's theme — so today's sit varies from it.
    * recent constellation activity — the stars (durable lessons) they've been
      actively working through, with their pinned notes, star reflections, and
      the honest tutoring signals. Shaped by
      ``apps.lessons.agent_context.build_constellation_context``.
    * recent daily-note snippets — what their week actually held.

    The journal/constellation sources egress to OpenRouter, which is configured for
    zero-data-retention — the basis for lifting the earlier PII-egress deferral on
    these signals.
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
    try:
        # Local import — keeps the lessons embedding/search stack out of module load.
        from apps.lessons.agent_context import build_constellation_context

        constellation = build_constellation_context(tenant, days=30, limit=4)
        stars = constellation.get("active_stars") if constellation else None
        if stars:
            signals["constellation_stars"] = stars
    except Exception:
        logger.debug("gather_meditation_signals: constellation read failed", exc_info=True)
    try:
        snippets = _recent_note_snippets(tenant)
        if snippets:
            signals["recent_notes"] = snippets
    except Exception:
        logger.debug("gather_meditation_signals: daily-note read failed", exc_info=True)
    try:
        goals = _active_goal_titles(tenant)
        if goals:
            signals["active_goals"] = goals
    except Exception:
        logger.debug("gather_meditation_signals: goals read failed", exc_info=True)
    # Fuel is consent-scoped: only surface a recent-activity line when the tenant
    # has Fuel enabled (the same enablement gate the Fuel plugin/UI honor).
    if getattr(tenant, "fuel_enabled", False):
        try:
            fuel = _recent_fuel_summary(tenant)
            if fuel:
                signals["fuel_summary"] = fuel
        except Exception:
            logger.debug("gather_meditation_signals: fuel read failed", exc_info=True)
    # TODO(purpose): when the North Star (Purpose) layer lands, fold the user's
    # active purpose/north-star (title only, consent-scoped) in here so the sit
    # can gently orient toward it. Do NOT import the Purpose model yet — it is
    # built in a parallel branch; wire this once that model is on main.
    return signals


def _active_goal_titles(tenant: Tenant, *, limit: int = 5) -> list[str]:
    """Titles of the user's active journal goals (titles only — no descriptions).

    Bounded and title-only on purpose: the guide gets the shape of what they're
    working toward without a data dump. Best-effort; the caller swallows failures.
    """
    from apps.journal.models import Goal

    titles = (
        Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE)
        .order_by("-updated_at")
        .values_list("title", flat=True)[:limit]
    )
    return [t.strip()[:120] for t in titles if (t or "").strip()]


def _recent_fuel_summary(tenant: Tenant, *, days: int = 7) -> str:
    """One gentle line about the user's recent training (consent-scoped to Fuel).

    Counts completed workouts in the tenant-local last-``days`` window. Returns a
    single short sentence (never a per-workout dump), or "" when nothing to say.
    """
    from apps.common.tenant_tz import tenant_today
    from apps.fuel.models import Workout, WorkoutStatus

    since = tenant_today(tenant) - timedelta(days=days)
    done = Workout.objects.filter(tenant=tenant, status=WorkoutStatus.DONE, date__gte=since).count()
    if not done:
        return ""
    unit = "workout" if done == 1 else "workouts"
    return f"They completed {done} {unit} in the last {days} days."


def _recent_note_snippets(tenant: Tenant, *, days: int = 7, limit: int = 3, cap: int = 220) -> list[str]:
    """Short, cleaned excerpts from the user's most recent daily notes.

    Strips markdown headings and log scaffolding so the guide sees the reflective
    prose, not the note's structure. Best-effort; the caller swallows failures.
    """
    from apps.journal.models import Document

    cutoff = (timezone.now() - timedelta(days=days)).date()
    docs = Document.objects.filter(tenant=tenant, kind="daily", slug__gte=str(cutoff)).order_by("-slug")[:limit]
    out: list[str] = []
    for doc in docs:
        body_lines = [
            ln.strip() for ln in (doc.markdown or "").splitlines() if ln.strip() and not ln.lstrip().startswith("#")
        ]
        body = " ".join(body_lines).strip()
        if len(body) < 12:
            continue
        if len(body) > cap:
            body = body[: cap - 1].rstrip() + "…"
        out.append(f"{doc.slug}: {body}")
    return out


def _claim_compose_authoring(session: MeditationSession) -> bool:
    """Lease invalid-manifest authoring without holding a DB lock over the LLM."""
    now = timezone.now()
    stale_cutoff = now - timedelta(minutes=_COMPOSE_CLAIM_STALE_MINUTES)
    claimed = (
        MeditationSession.objects.filter(
            id=session.id,
            status=MeditationStatus.PENDING,
        )
        .exclude(
            error=_COMPOSE_CLAIM_MARKER,
            updated_at__gte=stale_cutoff,
        )
        .update(
            error=_COMPOSE_CLAIM_MARKER,
            updated_at=now,
        )
    )
    if claimed:
        session.error = _COMPOSE_CLAIM_MARKER
        session.updated_at = now
        return True
    logger.info("compose_meditation: session %s already has a live authoring claim", str(session.id)[:8])
    return False


def compose_meditation(session: MeditationSession) -> None:
    """Ensure a pending session has a manifest, then enqueue its render.

    The web orb's compose flow: gather signals → LLM authors the manifest
    (judgment) → persist it → publish ``render_meditation`` (deterministic
    execution in a second QStash request). A redelivery with an already-valid
    persisted manifest skips authoring and republishes the render task. Authoring
    failure is terminal for this session (a retry won't help a refusal / invalid
    manifest); the manifest save uses the reconnect-safe path because the LLM call
    is itself a multi-second no-DB gap.
    """
    sid = str(session.id)
    if render.validate_manifest(session.manifest):
        if not _claim_compose_authoring(session):
            return
        try:
            signals = gather_meditation_signals(session.tenant)
            manifest = compose.author_manifest(signals, voice=session.voice, tenant=session.tenant)
        except compose.ComposeError as exc:
            logger.warning("compose_meditation: session %s authoring failed: %s", sid[:8], str(exc)[:160])
            _fail(session, f"compose_error: {exc}")
            _log_compose_failure(session.tenant, str(exc))
            return

        session.manifest = manifest
        session.title = str(manifest.get("title", ""))[:160]
        session.theme = str(manifest.get("theme", ""))
        _save_session(session, ["manifest", "title", "theme", "updated_at"])

    # A publish failure intentionally propagates. QStash will redeliver compose,
    # which resumes from the valid persisted manifest without another LLM call.
    from apps.cron.publish import publish_task

    publish_task("render_meditation", sid)


def _claim_render_session(session_id) -> tuple[MeditationSession | None, bool]:
    """Atomically claim one render attempt and return its persist-resume hint."""
    now = timezone.now()
    stale_minutes = int(getattr(settings, "CORE_RENDER_STALE_MINUTES", 15) or 15)
    stale_cutoff = now - timedelta(minutes=stale_minutes)
    max_attempts = int(getattr(settings, "CORE_RENDER_MAX_ATTEMPTS", 3) or 3)

    with transaction.atomic():
        current = MeditationSession.objects.select_for_update().filter(id=session_id).first()
        if current is None:
            return None, False

        failed_retry = (
            current.status == MeditationStatus.FAILED and current.failure_class == MeditationFailureClass.TRANSIENT
        )
        stale_render = current.status == MeditationStatus.RENDERING and current.updated_at < stale_cutoff
        claimable = current.status == MeditationStatus.PENDING or failed_retry or stale_render
        if not claimable:
            logger.info(
                "render_meditation: session %s not claimable (status=%s, failure_class=%s) — skipping",
                str(current.id)[:8],
                current.status,
                current.failure_class,
            )
            return None, False

        if current.attempt_count >= max_attempts:
            suffix = "attempts exhausted"
            existing = (current.error or "").rstrip()
            if not existing.endswith(suffix):
                current.error = f"{existing[: 480 - len(suffix) - 2]}; {suffix}" if existing else suffix
            current.status = MeditationStatus.FAILED
            current.failure_class = MeditationFailureClass.TERMINAL
            current.updated_at = now
            current.save(update_fields=["status", "failure_class", "error", "updated_at"])
            logger.warning(
                "render_meditation: session %s exhausted %d attempts — terminal",
                str(current.id)[:8],
                current.attempt_count,
            )
            return None, False

        # A consumer retry resets recoverable FAILED rows to PENDING before
        # publishing. Preserve the typed persist checkpoint hint from either
        # pre-claim status. If that publish also failed, dispatch_error replaced
        # the persist prefix; the durable hash plus the required MP3 probe below
        # still make artifact resume safe (new dispatch failures have no hash).
        error = current.error or ""
        resume_persist = (
            current.failure_class == MeditationFailureClass.TRANSIENT
            and (
                error.startswith("persist_error:")
                or (error.startswith("dispatch_error:") and bool(current.artifact_manifest_sha256))
            )
        ) or (
            # A worker can die after saving the MP3/hash checkpoint but before
            # setting READY. The stale claim has no failure prefix because its
            # original claim cleared it; the hash + required artifact probe are
            # the durable evidence that persistence can resume without TTS.
            stale_render and bool(current.artifact_manifest_sha256)
        )
        current.status = MeditationStatus.RENDERING
        current.failure_class = MeditationFailureClass.NONE
        current.error = ""
        current.attempt_count += 1
        current.updated_at = now
        current.save(
            update_fields=[
                "status",
                "failure_class",
                "error",
                "attempt_count",
                "updated_at",
            ]
        )
        return current, resume_persist


def _manifest_sha256(manifest: dict) -> str:
    """Return a stable fingerprint for a JSON render manifest."""
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _meditation_artifact_exists(tenant_id: str, file_path: str) -> bool:
    """Probe a deterministic meditation path through the existing share API."""
    return download_workspace_file(tenant_id, file_path) is not None


def _resume_uploaded_artifacts(
    session: MeditationSession,
    *,
    manifest_sha256: str,
    voice: str,
    model: str,
) -> bool:
    """Finalize a matching persist checkpoint without another TTS render."""
    if session.artifact_manifest_sha256 != manifest_sha256:
        return False

    sid = str(session.id)
    tenant_id = str(session.tenant_id)
    mp3_name = f"{sid}.mp3"
    if not _meditation_artifact_exists(tenant_id, f"{_MEDITATION_DIR}/{mp3_name}"):
        return False

    ogg_name = f"{sid}.ogg"
    has_ogg = _meditation_artifact_exists(tenant_id, f"{_MEDITATION_DIR}/{ogg_name}")
    api_base = (getattr(settings, "API_BASE_URL", "") or "").rstrip("/")
    session.audio_url = f"{api_base}/api/v1/meditations/{tenant_id}/{mp3_name}"
    session.ogg_url = f"{api_base}/api/v1/meditations/{tenant_id}/{ogg_name}" if has_ogg else ""
    session.guidance_text = session.guidance_text or render.flatten_guidance_text(session.manifest)
    session.model = session.model or model
    session.voice = session.voice or voice
    session.status = MeditationStatus.READY
    session.failure_class = MeditationFailureClass.NONE
    session.error = ""
    _save_session(
        session,
        [
            "audio_url",
            "ogg_url",
            "guidance_text",
            "model",
            "voice",
            "status",
            "failure_class",
            "error",
            "updated_at",
        ],
    )
    return True


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
    # The locked row is the single authority for claimability, attempt caps, and
    # whether this is specifically a persist-only retry.
    session, resume_persist = _claim_render_session(session.id)
    if session is None:
        return

    # ---- validate before any TTS spend; a bad manifest is terminal ----
    errors = render.validate_manifest(session.manifest)
    if errors:
        logger.warning("render_meditation: session %s invalid manifest: %s", sid[:8], errors[:3])
        _fail(session, "invalid_manifest: " + "; ".join(errors))
        return

    manifest_sha256 = _manifest_sha256(session.manifest)
    voice = (
        session.voice
        or (session.manifest.get("voice") if isinstance(session.manifest, dict) else "")
        or render.DEFAULT_VOICE
    )
    model = getattr(settings, "GEMINI_TTS_MODEL", "") or render.DEFAULT_MODEL

    if resume_persist:
        try:
            resumed = _resume_uploaded_artifacts(
                session,
                manifest_sha256=manifest_sha256,
                voice=voice,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001 — transient share/DB probe failure
            logger.exception("render_meditation: session %s checkpoint resume failed (will retry)", sid[:8])
            _fail(session, f"persist_error: checkpoint resume: {exc}")
            raise
        if resumed:
            logger.info("render_meditation: session %s ready from artifact checkpoint", sid[:8])
            _advance_onboarding_on_ready(session.tenant)
            try:
                notify_meditation_ready(session)
            except Exception:
                logger.warning(
                    "render_meditation: notify failed for session %s (audio already ready)",
                    sid[:8],
                    exc_info=True,
                )
            return

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
            tenant=session.tenant,
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
    # must follow the same FAILED-then-reraise contract as the render branch.
    # Once the MP3 exists, checkpoint the metadata + manifest hash while the row
    # remains RENDERING. A retry can then probe the deterministic paths and
    # finish persistence without spending on TTS again.
    checkpoint_fields: list[str] = []
    try:
        tenant_id = str(session.tenant_id)
        mp3_name = f"{sid}.mp3"
        upload_workspace_file_binary(tenant_id, f"{_MEDITATION_DIR}/{mp3_name}", result.mp3_bytes)
        api_base = (getattr(settings, "API_BASE_URL", "") or "").rstrip("/")
        session.audio_url = f"{api_base}/api/v1/meditations/{tenant_id}/{mp3_name}"
        session.ogg_url = ""
        session.duration_ms = result.duration_ms
        session.guidance_text = result.guidance_text
        session.model = model
        session.voice = voice
        session.artifact_manifest_sha256 = manifest_sha256
        checkpoint_fields = [
            "audio_url",
            "ogg_url",
            "duration_ms",
            "guidance_text",
            "model",
            "voice",
            "artifact_manifest_sha256",
            "updated_at",
        ]
        _save_session(session, checkpoint_fields)

        if result.ogg_bytes:
            ogg_name = f"{sid}.ogg"
            upload_workspace_file_binary(tenant_id, f"{_MEDITATION_DIR}/{ogg_name}", result.ogg_bytes)
            session.ogg_url = f"{api_base}/api/v1/meditations/{tenant_id}/{ogg_name}"

        session.status = MeditationStatus.READY
        session.failure_class = MeditationFailureClass.NONE
        session.error = ""
        _save_session(
            session,
            [
                "ogg_url",
                "status",
                "failure_class",
                "error",
                "updated_at",
            ],
        )
    except Exception as exc:  # noqa: BLE001 — transient persist failure: FAILED, then retry
        logger.exception("render_meditation: session %s persist failed (will retry)", sid[:8])
        _fail(session, f"persist_error: {exc}", update_fields=checkpoint_fields)
        raise
    logger.info(
        "render_meditation: session %s ready (%.1fs, %d segs, %d fallback)",
        sid[:8],
        result.duration_ms / 1000.0,
        result.speech_count,
        result.failed_count,
    )

    # A first successful sit completes Core onboarding (idempotent, best-effort —
    # a failure here must never touch the already-stored render).
    _advance_onboarding_on_ready(session.tenant)

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


def _failure_class(message: str) -> str:
    """Classify retry safety from the pipeline's typed error prefix."""
    if message.startswith(("render_error:", "persist_error:")):
        return MeditationFailureClass.TRANSIENT
    return MeditationFailureClass.TERMINAL


def _fail(session: MeditationSession, message: str, *, update_fields: list[str] | None = None) -> None:
    session.status = MeditationStatus.FAILED
    session.failure_class = _failure_class(message)
    session.error = message[:480]
    fields = ["status", "failure_class", "error", *(update_fields or []), "updated_at"]
    _save_session(session, list(dict.fromkeys(fields)))


def _log_compose_failure(tenant: Tenant, reason: str) -> None:
    """Record a terminal compose failure to platform_logs for fleet visibility.

    Core's compose has a history of quiet failures; surfacing each terminal one
    (every model in the chain failed) as a PlatformIssueLog row makes the rate
    actionable instead of invisible. Best-effort — never breaks the compose path.
    """
    try:
        from apps.platform_logs.models import PlatformIssueLog

        PlatformIssueLog.objects.create(
            tenant=tenant,
            category=PlatformIssueLog.Category.OTHER,
            severity=PlatformIssueLog.Severity.MEDIUM,
            tool_name="core_compose",
            summary="Core meditation compose failed (every model in the chain)"[:500],
            detail=reason[:2000],
        )
    except Exception:
        logger.debug("core compose: platform-log record failed", exc_info=True)


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
    # Placeholder-space twin of ``message`` for the at-rest records below.
    placeholder_message = _ready_message(session, tenant, rehydrate=False)

    if channel == "line":
        channel_user_id = getattr(user, "line_user_id", "") or ""
        delivered = _send_line_text(tenant, channel_user_id, message, excerpt_override=placeholder_message)
    elif channel in ("app", "eval"):
        # App: the recorded row drives APNs + the owner-facing feed. Eval: the
        # recorded row is internal evidence and record_proactive_outbound skips
        # APNs. Neither channel should fall through to Telegram.
        channel_user_id = str(user.id)
        delivered = True
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
                # Placeholder-space at rest; record_proactive_outbound rehydrates
                # only for the owner-facing iOS push.
                message_text=placeholder_message,
                job_name=_READY_JOB_NAME,
            )
        except Exception:
            logger.debug("Core notify: proactive record failed", exc_info=True)

    return bool(delivered)


def _ready_message(session: MeditationSession, tenant: Tenant, *, rehydrate: bool = True) -> str:
    """Build the "meditation ready" body.

    ``rehydrate=True`` (default) resolves PII placeholders in the title for the
    copy actually sent to the user; ``rehydrate=False`` returns the
    placeholder-space copy used for the at-rest records (ProactiveOutbound /
    LINE quote-reply excerpt), so those columns store no real names.
    """
    title = (session.title or "").strip()
    # The title is assistant-authored and may carry PII placeholders ([PERSON_N]);
    # rehydrate before it reaches the channel (same boundary as CronDeliveryView).
    entity_map = getattr(tenant, "pii_entity_map", None)
    if title and rehydrate and entity_map:
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


def _send_line_text(tenant: Tenant, line_user_id: str, text: str, *, excerpt_override: str | None = None) -> bool:
    from apps.common.eval_sink import suppresses_real_transport

    if suppresses_real_transport(tenant):
        logger.error("eval-sink transport block: tenant=%s transport=line", tenant.id)
        return False
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
        _record_line_outbound(tenant, line_user_id, sent, messages, excerpt_override=excerpt_override)
    except Exception:
        logger.debug("Core notify: LINE outbound record failed", exc_info=True)
    return True
