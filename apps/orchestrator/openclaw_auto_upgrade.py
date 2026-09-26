"""Hands-free OpenClaw 5.28 -> 9.4 upgrade when an awake tenant goes idle.

The idle sweep (``hibernate_idle_tenants_task``) calls
``intercept_idle_tenant`` before hibernating. For an eligible 5.28 tenant it
takes the platform-wide lease and enqueues ``auto_upgrade_openclaw_task``
instead of hibernating; the task runs the existing ``migrate_tenant`` tool and
hibernates the tenant itself when it is done.

Every run ends in exactly one of two states:
  * PASS, tenant hibernated (or left awake for the sweep if it is busy); or
  * back on 5.28 and unfenced (``rollback_tenant`` / a no-undo reset),
    MJ emailed, tenant in cooldown.
Quiet give-ups (cron timing, tenant unavailable twice) also unfence first.

Known production behaviour this is built around (2026-09-25/26 fleet runs):
  * The Azure exec console rate-limits subscription-wide (429, retry-after
    600 s): one run at a time, runs spaced ``SPACING`` apart, and console
    retries wait ``TRANSIENT_RETRY_SECONDS``.
  * Slow consoles time out at ``crons``/``verify``; a later retry passes.
  * A failure at ``image``/``version``/``config`` leaves the tenant on a
    restart-looping 9.4 revision, so it rolls back at once.

Waits are delayed QStash deliveries, never sleeps. A heartbeat thread keeps
the lease alive while ``migrate_tenant`` runs; if the process dies the lease
expires and the next sweep starts recovery (resume via takeover once, then
roll back). No user content is read, logged, stored or emailed: outcomes are
step names and reason codes.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.tenants.models import OpenClawAutoUpgrade, OpenClawAutoUpgradeLock, Tenant

logger = logging.getLogger(__name__)

TASK_NAME = "auto_upgrade_openclaw"
SOURCE_VERSION = "2026.5.28"
TAG_RE = re.compile(r"2026\.9\.4-[0-9a-f]{7,40}")

SPACING = timedelta(minutes=12)
LEASE_TTL = timedelta(minutes=10)
HEARTBEAT_SECONDS = 60
FAILURE_COOLDOWN = timedelta(days=7)
QUIET_COOLDOWN = timedelta(hours=1)
WAKE_SETTLE_SECONDS = 120
UNAVAILABLE_RETRY_SECONDS = 300
TRANSIENT_RETRY_SECONDS = 720
MAX_REWAKES = 1
MAX_TRANSIENT_RESUMES = 2
MAX_JEV_RESUMES = 1
MAX_TAKEOVERS = 1
JEV_TRANSIENT_MIN_P = 0.85
HISTORY_LIMIT = 30

# Actions of the rules table.
FINISH_PASS = "finish_pass"
GIVE_UP_QUIET = "give_up_quiet"
REWAKE_RESUME = "rewake_resume"
WAIT_RESUME = "wait_resume"
ROLLBACK_ALERT = "rollback_alert"
ALERT_ONLY = "alert_only"
CLASSIFY = "classify"

POST_SWAP_STEPS = frozenset({"image", "version", "config"})
RESUMABLE_STEPS = frozenset({"preflight", "capture", "crons", "verify"})
UNAVAILABLE_REASONS = frozenset(
    {
        "tenant_unavailable",
        "Hibernated tenant refused: wake on its current image, then migrate",
        "Migration requires an active, provisioned tenant",
    }
)
# Operator console failures seen live on slow replicas and under the 429 limit.
TRANSIENT_MARKERS = (
    "Operator command timed out or returned no result",
    "Operator console failed",
    "Console protocol failure",
    "429",
    "Too Many Requests",
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return bool(getattr(settings, "OPENCLAW_AUTO_UPGRADE_ENABLED", False))


def allowlisted(tenant_id) -> bool:
    """Empty means nobody; ``*`` means every 5.28 tenant."""
    raw = str(getattr(settings, "OPENCLAW_AUTO_UPGRADE_TENANT_IDS", "") or "").strip()
    if not raw:
        return False
    if raw == "*":
        return True
    return str(tenant_id) in {part.strip() for part in raw.split(",") if part.strip()}


def target_tag() -> str:
    return str(
        getattr(settings, "OPENCLAW_AUTO_UPGRADE_TAG", "") or getattr(settings, "OPENCLAW_IMAGE_TAG", "") or ""
    ).strip()


def jev_enabled() -> bool:
    return bool(getattr(settings, "OPENCLAW_AUTO_UPGRADE_JEV_ENABLED", False))


# ---------------------------------------------------------------------------
# Platform-wide lease
# ---------------------------------------------------------------------------


def _lock_row() -> OpenClawAutoUpgradeLock:
    try:
        row, _ = OpenClawAutoUpgradeLock.objects.get_or_create(pk=1)
    except IntegrityError:
        row = OpenClawAutoUpgradeLock.objects.get(pk=1)
    return row


def acquire(tenant) -> str | None:
    """Take the free lease for ``tenant``; None when busy or inside spacing.

    An expired lease with a holder is NOT free: that run died and belongs to
    ``reap_stale_run``.
    """
    _lock_row()
    now = timezone.now()
    token = uuid.uuid4().hex
    taken = (
        OpenClawAutoUpgradeLock.objects.filter(pk=1, run_token="")
        .filter(Q(next_allowed_at__isnull=True) | Q(next_allowed_at__lte=now))
        .update(tenant=tenant, run_token=token, expires_at=now + LEASE_TTL)
    )
    return token if taken == 1 else None


def extend(run_token, until) -> bool:
    return OpenClawAutoUpgradeLock.objects.filter(pk=1, run_token=run_token).update(expires_at=until) == 1


def release(run_token, *, space=True) -> bool:
    updates = {"tenant": None, "run_token": "", "expires_at": None}
    if space:
        updates["next_allowed_at"] = timezone.now() + SPACING
    return OpenClawAutoUpgradeLock.objects.filter(pk=1, run_token=run_token).update(**updates) == 1


def holds(tenant_id, run_token) -> bool:
    return (
        bool(run_token)
        and OpenClawAutoUpgradeLock.objects.filter(pk=1, run_token=run_token, tenant_id=tenant_id).exists()
    )


def in_flight(tenant_id) -> bool:
    """True while a run (live or awaiting recovery) holds this tenant.

    The idle sweep and the cron-wake idle check must not hibernate it: the
    run hibernates the tenant itself once it is safe.
    """
    return OpenClawAutoUpgradeLock.objects.filter(pk=1, tenant_id=tenant_id).exclude(run_token="").exists()


@contextmanager
def heartbeat(run_token):
    """Renew the lease while a long synchronous step runs in this process."""
    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        yield
        return
    stop = threading.Event()

    def beat():
        from django.db import connection

        try:
            prepared = False
            while not stop.wait(HEARTBEAT_SECONDS):
                if not prepared:
                    from apps.tenants.middleware import set_rls_context

                    set_rls_context(service_role=True)
                    prepared = True
                if not extend(run_token, timezone.now() + LEASE_TTL):
                    return
        except Exception:
            logger.exception("auto_upgrade: heartbeat failed")
        finally:
            connection.close()

    thread = threading.Thread(target=beat, daemon=True, name="oc-auto-upgrade-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Per-tenant state
# ---------------------------------------------------------------------------


def _state(tenant) -> OpenClawAutoUpgrade:
    state, _ = OpenClawAutoUpgrade.objects.get_or_create(tenant=tenant)
    return state


def _note(state, event, **fields):
    state.history = (list(state.history or []) + [{"at": timezone.now().isoformat(), "event": event, **fields}])[
        -HISTORY_LIMIT:
    ]


def in_cooldown(tenant, now=None) -> bool:
    now = now or timezone.now()
    return OpenClawAutoUpgrade.objects.filter(tenant=tenant, cooldown_until__gt=now).exists()


def eligible(tenant) -> bool:
    if not enabled() or not allowlisted(tenant.id):
        return False
    if tenant.openclaw_version != SOURCE_VERSION or tenant.status != Tenant.Status.ACTIVE:
        return False
    if not tenant.container_id or not tenant.container_fqdn:
        return False
    # A fenced tenant or a non-terminal record from another run is a human case.
    if tenant.openclaw_migration_cron_fenced:
        return False
    if (tenant.openclaw_migration or {}).get("status") not in (None, "ROLLED_BACK"):
        return False
    if not TAG_RE.fullmatch(target_tag()):
        return False
    return not in_cooldown(tenant)


# ---------------------------------------------------------------------------
# Idle sweep hooks
# ---------------------------------------------------------------------------


def intercept_idle_tenant(tenant) -> bool:
    """Called by the idle sweep for an idle tenant. True = do not hibernate."""
    if in_flight(tenant.id):
        return True
    if not eligible(tenant):
        return False
    run_token = acquire(tenant)
    if not run_token:
        return False
    state = _state(tenant)
    # The tag is pinned for the whole run: a deploy mid-run must not retarget a resume.
    state.run_token, state.migration_token, state.counters, state.last_outcome = (
        run_token,
        "",
        {"tag": target_tag()},
        {},
    )
    _note(state, "start", tag=target_tag())
    state.save()
    try:
        from apps.cron.publish import publish_task

        publish_task(TASK_NAME, str(tenant.id), run_token, "start", retries=0)
    except Exception:
        logger.exception("auto_upgrade: failed to enqueue start for %s", str(tenant.id)[:8])
        release(run_token, space=False)
        return False
    logger.info("auto_upgrade: enqueued 5.28 -> 9.4 upgrade for %s", str(tenant.id)[:8])
    return True


def reap_stale_run() -> str | None:
    """Start recovery for a run whose lease expired (its process died)."""
    row = OpenClawAutoUpgradeLock.objects.filter(pk=1).exclude(run_token="").first()
    now = timezone.now()
    if row is None or (row.expires_at and row.expires_at > now):
        return None
    if row.tenant_id is None:
        release(row.run_token)
        return "cleared"
    run_token = uuid.uuid4().hex
    if (
        OpenClawAutoUpgradeLock.objects.filter(pk=1, run_token=row.run_token).update(
            run_token=run_token, expires_at=now + LEASE_TTL
        )
        != 1
    ):
        return None
    OpenClawAutoUpgrade.objects.filter(tenant_id=row.tenant_id).update(run_token=run_token)
    try:
        from apps.cron.publish import publish_task

        publish_task(TASK_NAME, str(row.tenant_id), run_token, "recover", retries=0)
    except Exception:
        # The lease expires again and the next sweep retries.
        logger.exception("auto_upgrade: failed to enqueue recovery for %s", str(row.tenant_id)[:8])
        return None
    logger.warning("auto_upgrade: lease expired for %s; recovery enqueued", str(row.tenant_id)[:8])
    return "recovering"


# ---------------------------------------------------------------------------
# Rules table (pure)
# ---------------------------------------------------------------------------


def is_transient(reason) -> bool:
    return any(marker in str(reason or "") for marker in TRANSIENT_MARKERS)


def decide(outcome: dict, counters: dict) -> str:
    """Map a migration outcome to the next action. No I/O.

    ``outcome``: ``status`` (PASS/NOOP/DEFER/DEFERRED/BLOCKED_UNSUPPORTED/
    FAILED/ERROR), ``step``, ``reason`` and readiness ``reasons``.
    """
    status, step, reason = outcome.get("status"), outcome.get("step"), str(outcome.get("reason") or "")
    reasons = set(outcome.get("reasons") or ()) | ({reason} if reason else set())
    unavailable = bool(reasons & UNAVAILABLE_REASONS)
    can_rewake = counters.get("rewakes", 0) < MAX_REWAKES
    if status in {"PASS", "NOOP"}:
        return FINISH_PASS
    if status == "BLOCKED_UNSUPPORTED":
        return ROLLBACK_ALERT
    if status == "DEFER":  # readiness refused before claiming anything
        return REWAKE_RESUME if unavailable and can_rewake else GIVE_UP_QUIET
    if status == "DEFERRED":  # cron timing, before any Azure write
        return GIVE_UP_QUIET
    if status == "FAILED":
        if unavailable:
            return REWAKE_RESUME if can_rewake else ROLLBACK_ALERT
        if reason == "migration_owner_fenced":
            return ALERT_ONLY  # someone else owns the record now; never undo their run
        if step in POST_SWAP_STEPS:
            return ROLLBACK_ALERT
        if step in RESUMABLE_STEPS and is_transient(reason):
            return WAIT_RESUME if counters.get("transient_resumes", 0) < MAX_TRANSIENT_RESUMES else ROLLBACK_ALERT
        return CLASSIFY
    if status == "ERROR":  # refused before claiming a run
        return GIVE_UP_QUIET if reason.startswith("Migration already running") else ALERT_ONLY
    return CLASSIFY


# ---------------------------------------------------------------------------
# Jev tie-breaker (unknown failures only)
# ---------------------------------------------------------------------------

JEV_CHOICES = {
    "transient_retry_later": "A temporary infrastructure hiccup (timeout, rate limit, slow start); "
    "the same step would likely pass if retried later unchanged.",
    "needs_rollback_now": "The upgrade is broken or unsafe to continue; restore the previous version now.",
    "needs_human": "An unfamiliar or data-shape problem that a person must look at.",
}
_SCRUB = (
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
    re.compile(r"\boc-[A-Za-z0-9-]+"),
    re.compile(r"\b[0-9a-fA-F]{8,}\b"),
)


def scrub(text, limit=240) -> str:
    text = str(text or "")
    for pattern in _SCRUB:
        text = pattern.sub("<id>", text)
    return text[:limit]


def jev_policy(choice, probabilities, *, used) -> str:
    """Policy lives here, not in the model: Jev only picks among safe actions.

    Retrying is allowed once, and only on a confident transient verdict;
    everything else (including low confidence) rolls back and emails MJ.
    """
    p = (probabilities or {}).get("transient_retry_later", 0.0)
    if choice == "transient_retry_later" and p >= JEV_TRANSIENT_MIN_P and used < MAX_JEV_RESUMES:
        return WAIT_RESUME
    return ROLLBACK_ALERT


def classify_with_jev(step, reason, error_class) -> dict | None:
    from apps.common import jev

    question = jev.ChoiceQuestion(
        instructions=(
            "An automatic software upgrade of one customer's assistant container stopped at the step "
            "named in the state. Classify the failure. Only the error text is given; no user data."
        ),
        criteria=dict(JEV_CHOICES),
    )
    state = {"step": str(step or "unknown"), "error_class": str(error_class or "unknown"), "error": scrub(reason)}
    try:
        answer = jev.decide(state, {"failure": question}).answers["failure"]
    except jev.JevUnavailable:
        return None
    return {"choice": answer.choice, "probabilities": dict(answer.probabilities), "confidence": answer.confidence}


def _classify(outcome, counters):
    if not jev_enabled() or outcome.get("step") in POST_SWAP_STEPS:
        return ROLLBACK_ALERT, None
    verdict = classify_with_jev(outcome.get("step"), outcome.get("reason"), outcome.get("status"))
    if verdict is None:
        return ROLLBACK_ALERT, {"choice": "unavailable"}
    action = jev_policy(verdict["choice"], verdict["probabilities"], used=counters.get("jev_resumes", 0))
    return action, verdict


# ---------------------------------------------------------------------------
# Safe exit guard
# ---------------------------------------------------------------------------


def ensure_safe_exit(tenant, migration_token) -> str:
    """Never leave this run's record fenced or on an unhealthy revision.

    Acts only on the record this run claimed (matching owner token). A PASS
    stays; otherwise roll back to the undo point, or reset a run that never
    reached an Azure write. Returns what happened.
    """
    from apps.orchestrator import openclaw_migration

    tenant.refresh_from_db(fields=["openclaw_migration", "openclaw_migration_cron_fenced"])
    record = tenant.openclaw_migration or {}
    if not migration_token or record.get("owner_token") != migration_token:
        return "not_ours"
    if record.get("status") == "PASS":
        return "pass"
    if record.get("status") == "RUNNING":
        # Only reached once this run's migrate_tenant has returned or its
        # process died, so the owner is gone; the tool's own failure
        # checkpoint did not land. Record that, so the undo is not refused.
        record = {**record, "status": "FAILED", "lease_until": timezone.now().isoformat()}
        record.setdefault("failure", {"step": record.get("step"), "reason": "owner_exited"})
        Tenant.objects.filter(pk=tenant.pk, openclaw_migration__owner_token=migration_token).update(
            openclaw_migration=record
        )
    last = "unknown"
    for _ in range(2):  # rollback is idempotent; one retry covers a slow console/health probe
        try:
            if record.get("undo"):
                openclaw_migration.rollback_tenant(tenant.id)
                return "rolled_back"
            openclaw_migration.reset_unsubmitted_migration(tenant.id)
            return "reset"
        except Exception as exc:
            last = str(exc) if isinstance(exc, openclaw_migration.MigrationError) else type(exc).__name__
            logger.warning("auto_upgrade: safe exit failed for %s (%s)", str(tenant.id)[:8], last)
    return "failed:" + last


def _safe_exit(tenant, state, run_token) -> str:
    # A rollback (share restore + revision copy + health wait) can take
    # minutes; keep the lease alive so recovery never races it.
    with heartbeat(run_token):
        return ensure_safe_exit(tenant, state.migration_token)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def alert(tenant, *, outcome, action, guard, detail="") -> bool:
    owner = getattr(settings, "PLATFORM_OWNER_EMAIL", "")
    if not owner:
        logger.warning("auto_upgrade: PLATFORM_OWNER_EMAIL not set; alert skipped")
        return False
    from django.core.mail import send_mail

    short = str(tenant.id)[:8]
    lines = [
        f"OpenClaw auto-upgrade (5.28 -> 9.4) stopped for tenant {short}.",
        "",
        f"Outcome: {outcome.get('status')} at step {outcome.get('step') or '-'}",
        f"Reason code: {scrub(outcome.get('reason') or ','.join(sorted(outcome.get('reasons') or {})) or '-')}",
        f"Action taken: {action}; safe exit: {guard}",
        f"Cooldown: skipped by auto-upgrade until {timezone.now() + FAILURE_COOLDOWN:%Y-%m-%d %H:%M} UTC",
    ]
    if detail:
        lines.append(f"Detail: {detail}")
    lines += [
        "",
        "Retry by hand (inside the Django container):",
        f"  python manage.py migrate_tenant_openclaw --tenant {tenant.id} --report",
        f"  python manage.py migrate_tenant_openclaw --tenant {tenant.id}",
        f"  python manage.py migrate_tenant_openclaw --tenant {tenant.id} --rollback   # if left on 9.4",
        "Runbook: docs/runbooks/openclaw-94-migration.md",
    ]
    if guard.startswith("failed"):
        lines.insert(1, "ACTION NEEDED: the automatic safe exit did not complete; the tenant may be down or fenced.")
    try:
        return (
            send_mail(
                subject=f"[nbhd] OpenClaw auto-upgrade {action} for {short}",
                message="\n".join(lines),
                from_email=None,
                recipient_list=[owner],
                fail_silently=False,
            )
            > 0
        )
    except Exception:
        logger.exception("auto_upgrade: alert delivery failed for %s", short)
        return False


# ---------------------------------------------------------------------------
# The task
# ---------------------------------------------------------------------------


def _schedule(tenant, run_token, phase, delay) -> bool:
    extend(run_token, timezone.now() + timedelta(seconds=delay) + LEASE_TTL)
    try:
        from apps.cron.publish import publish_task

        publish_task(TASK_NAME, str(tenant.id), run_token, phase, delay_seconds=delay, retries=0)
        return True
    except Exception:
        logger.exception("auto_upgrade: failed to schedule %s for %s", phase, str(tenant.id)[:8])
        return False


def _wake(tenant):
    """Keep the tenant awake: wake it if hibernated, else refresh the cron shield."""
    from apps.orchestrator.hibernation import wake_hibernated_tenant

    tenant.refresh_from_db(fields=["hibernated_at", "cron_wake_at"])
    if tenant.hibernated_at:
        return wake_hibernated_tenant(tenant, cron_wake=True)
    Tenant.objects.filter(pk=tenant.pk).update(cron_wake_at=timezone.now())
    return True


def _hibernate_if_idle(tenant) -> bool:
    from apps.orchestrator.hibernation import _cron_active_or_imminent, hibernate_idle_tenant

    tenant.refresh_from_db()
    cutoff = timezone.now() - timedelta(minutes=settings.TENANT_IDLE_HIBERNATE_MINUTES)
    if tenant.hibernated_at or tenant.status != Tenant.Status.ACTIVE:
        return False
    if tenant.last_message_at and tenant.last_message_at >= cutoff:
        return False  # the user came back; the normal sweep handles it later
    if _cron_active_or_imminent(tenant):
        return False
    return bool(hibernate_idle_tenant(tenant))


def _finish(tenant, state, run_token, *, hibernate, result):
    hibernated = False
    if hibernate:
        try:
            hibernated = _hibernate_if_idle(tenant)
        except Exception:
            logger.exception("auto_upgrade: hibernate after run failed for %s", str(tenant.id)[:8])
    _note(state, "finish", result=result, hibernated=hibernated)
    state.run_token = ""
    state.save()
    release(run_token)
    return {"status": result, "hibernated": hibernated}


def _handle(tenant, state, run_token, outcome):
    counters = dict(state.counters or {})
    action = decide(outcome, counters)
    verdict = None
    if action == CLASSIFY:
        action, verdict = _classify(outcome, counters)
        if verdict is not None:
            _log_jev(tenant, state, outcome, verdict, action)
    safe_outcome = {**outcome, "reason": scrub(outcome.get("reason"), 300)}
    state.last_outcome = {**safe_outcome, "action": action}
    _note(state, "outcome", **safe_outcome, action=action)
    state.counters = counters
    state.save()
    logger.info(
        "auto_upgrade: %s outcome=%s step=%s action=%s",
        str(tenant.id)[:8],
        outcome.get("status"),
        outcome.get("step"),
        action,
    )

    if action == FINISH_PASS:
        return _finish(tenant, state, run_token, hibernate=True, result="PASS")

    if action in (REWAKE_RESUME, WAIT_RESUME):
        if action == REWAKE_RESUME:
            counters["rewakes"] = counters.get("rewakes", 0) + 1
            delay = UNAVAILABLE_RETRY_SECONDS
            woke = _wake(tenant)
        else:
            key = "jev_resumes" if verdict else "transient_resumes"
            counters[key] = counters.get(key, 0) + 1
            delay, woke = TRANSIENT_RETRY_SECONDS, True
        state.counters = counters
        state.save()
        if woke and _schedule(tenant, run_token, "resume", delay):
            return {"status": "WAITING", "action": action, "delay_seconds": delay}
        # Cannot come back later: exit safely now.
        action = ROLLBACK_ALERT
        outcome = {**outcome, "reason": f"{outcome.get('reason') or ''} (resume could not be scheduled)".strip()}

    guard = _safe_exit(tenant, state, run_token)
    _note(state, "safe_exit", guard=guard)
    if action == GIVE_UP_QUIET:
        state.cooldown_until = timezone.now() + QUIET_COOLDOWN
        if guard.startswith("failed"):
            alert(tenant, outcome=outcome, action="give_up", guard=guard)
            state.cooldown_until = timezone.now() + FAILURE_COOLDOWN
        return _finish(tenant, state, run_token, hibernate=not guard.startswith("failed"), result="GAVE_UP")

    # ROLLBACK_ALERT / ALERT_ONLY
    state.cooldown_until = timezone.now() + FAILURE_COOLDOWN
    alerted = alert(tenant, outcome=outcome, action=action, guard=guard)
    _note(state, "alert", sent=alerted)
    result = "ROLLED_BACK" if guard in ("rolled_back", "reset") else "STOPPED"
    return _finish(tenant, state, run_token, hibernate=not guard.startswith("failed"), result=result)


def _log_jev(tenant, state, outcome, verdict, action):
    entry = {
        "at": timezone.now().isoformat(),
        "step": outcome.get("step"),
        "verdict": verdict,
        "action": action,
    }
    _note(state, "jev", **entry)
    # Also keep it on the migration record for later review of the run.
    tenant.refresh_from_db(fields=["openclaw_migration"])
    record = dict(tenant.openclaw_migration or {})
    if state.migration_token and record.get("owner_token") == state.migration_token:
        record["auto_upgrade_decisions"] = list(record.get("auto_upgrade_decisions", [])) + [entry]
        Tenant.objects.filter(pk=tenant.pk, openclaw_migration__owner_token=state.migration_token).update(
            openclaw_migration=record
        )


def _run_migration(tenant, state, run_token, *, takeover=None):
    from apps.orchestrator.openclaw_migration import MigrationError, MigrationFailed, migrate_tenant

    state.migration_token = uuid.uuid4().hex
    _note(state, "migrate", takeover=bool(takeover))
    state.save()
    extra = {"takeover": takeover, "confirm_owner_dead": True} if takeover else {}
    try:
        with heartbeat(run_token):
            tag = (state.counters or {}).get("tag") or target_tag()
            result = migrate_tenant(tenant.id, tag, owner_token=state.migration_token, **extra)
        outcome = {"status": result["status"], "step": None, "reason": "", "reasons": dict(result.get("reasons") or {})}
    except MigrationFailed as exc:
        outcome = {"status": exc.status, "step": exc.step, "reason": exc.reason, "reasons": {}}
    except MigrationError as exc:
        outcome = {"status": "ERROR", "step": None, "reason": str(exc), "reasons": {}}
    except Exception as exc:
        outcome = {"status": "ERROR", "step": None, "reason": type(exc).__name__, "reasons": {}}
    extend(run_token, timezone.now() + LEASE_TTL)
    return _handle(tenant, state, run_token, outcome)


def _recover(tenant, state, run_token):
    """The previous process died mid-run. Resume via takeover once, else undo."""
    record = tenant.openclaw_migration or {}
    ours = bool(state.migration_token) and record.get("owner_token") == state.migration_token
    _note(state, "recover", ours=ours, status=record.get("status"))
    state.save()
    if not ours:
        # Nothing claimed (died before migrate_tenant) or already undone.
        return _finish(tenant, state, run_token, hibernate=True, result="RECOVERED_NOOP")
    if record.get("status") == "PASS":
        return _finish(tenant, state, run_token, hibernate=True, result="PASS")
    if record.get("status") == "RUNNING":
        lease = parse_datetime(record.get("lease_until") or "")
        if lease and lease > timezone.now():
            # Never take over a live lease. If this publish fails, our own
            # lease lapses too and the next sweep re-enters recovery.
            delay = int((lease - timezone.now()).total_seconds()) + 30
            _schedule(tenant, run_token, "recover", delay)
            return {"status": "WAITING", "action": "await_record_lease", "delay_seconds": delay}
        counters = dict(state.counters or {})
        if counters.get("takeovers", 0) < MAX_TAKEOVERS:
            counters["takeovers"] = counters.get("takeovers", 0) + 1
            state.counters = counters
            state.save()
            _wake(tenant)
            return _run_migration(tenant, state, run_token, takeover=record["owner_token"])
    failure = record.get("failure") or {}
    outcome = {
        "status": "FAILED",
        "step": failure.get("step") or record.get("step"),
        "reason": "run_interrupted",
        "reasons": {},
    }
    guard = _safe_exit(tenant, state, run_token)
    state.cooldown_until = timezone.now() + FAILURE_COOLDOWN
    alert(tenant, outcome=outcome, action=ROLLBACK_ALERT, guard=guard, detail="worker died mid-run")
    return _finish(
        tenant,
        state,
        run_token,
        hibernate=not guard.startswith("failed"),
        result="ROLLED_BACK" if guard in ("rolled_back", "reset") else "STOPPED",
    )


def claim(tenant_id, run_token) -> str | None:
    """Make a delivery single-use: rotate the lease token, or refuse.

    A duplicate or stale QStash delivery then finds its token gone.
    """
    fresh = uuid.uuid4().hex
    rotated = OpenClawAutoUpgradeLock.objects.filter(pk=1, run_token=run_token, tenant_id=tenant_id).update(
        run_token=fresh, expires_at=timezone.now() + LEASE_TTL
    )
    if rotated != 1:
        return None
    OpenClawAutoUpgrade.objects.filter(tenant_id=tenant_id).update(run_token=fresh)
    return fresh


def run_phase(tenant_id: str, run_token: str, phase: str = "start") -> dict:
    """One phase of a run: start -> [resume ...] -> finish, or recover.

    Runs in its own process (``manage.py auto_upgrade_openclaw``) so a
    gunicorn worker recycle or request timeout cannot cut a migration short.
    """
    tenant = Tenant.objects.filter(pk=tenant_id).first()
    run_token = claim(tenant_id, run_token) if tenant is not None and run_token else None
    if not run_token:
        return {"status": "stale_run"}
    state = _state(tenant)
    try:
        if phase == "recover":
            return _recover(tenant, state, run_token)
        if phase == "start":
            if not eligible(tenant):
                return _finish(tenant, state, run_token, hibernate=False, result="NOT_ELIGIBLE")
            if tenant.hibernated_at:
                if not _wake(tenant) or not _schedule(tenant, run_token, "resume", WAKE_SETTLE_SECONDS):
                    return _finish(tenant, state, run_token, hibernate=False, result="WAKE_FAILED")
                return {"status": "WAITING", "action": "wake_settle", "delay_seconds": WAKE_SETTLE_SECONDS}
            _wake(tenant)
            return _run_migration(tenant, state, run_token)
        if phase == "resume":
            return _run_migration(tenant, state, run_token)
        return _finish(tenant, state, run_token, hibernate=False, result="UNKNOWN_PHASE")
    except Exception as exc:
        # A bug in this module must still honour the invariant.
        logger.exception("auto_upgrade: task error for %s", str(tenant.id)[:8])
        outcome = {"status": "ERROR", "step": None, "reason": type(exc).__name__, "reasons": {}}
        guard = _safe_exit(tenant, state, run_token)
        state.cooldown_until = timezone.now() + FAILURE_COOLDOWN
        alert(tenant, outcome=outcome, action=ROLLBACK_ALERT, guard=guard, detail="auto-upgrade task error")
        return _finish(tenant, state, run_token, hibernate=not guard.startswith("failed"), result="STOPPED")


def auto_upgrade_openclaw_task(tenant_id: str, run_token: str, phase: str = "start") -> dict:
    """QStash entry point: hand the phase to a detached process and return.

    A migration takes 10-25 minutes; inside the request it would outlive the
    ingress timeout and die on a worker recycle. The child claims the lease
    itself, so a stale token here spawns nothing useful but harms nothing.
    """
    import subprocess
    import sys

    if not holds(tenant_id, run_token):
        return {"status": "stale_run"}
    subprocess.Popen(  # noqa: S603 - fixed argv, validated UUID/hex/phase values
        [
            sys.executable,
            str(settings.BASE_DIR / "manage.py"),
            "auto_upgrade_openclaw",
            str(uuid.UUID(str(tenant_id))),
            re.fullmatch(r"[0-9a-f]{32}", run_token).group(0),
            {"start": "start", "resume": "resume", "recover": "recover"}[phase],
        ],
        cwd=str(settings.BASE_DIR),
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return {"status": "spawned", "phase": phase}
