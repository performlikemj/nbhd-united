"""Eval-failure alerting — a content-free email to the platform owner (Wave B).

The eval-failure alert is modeled on
``apps/router/line_quota_handlers.handle_pre_warn``: gate on
``PLATFORM_OWNER_EMAIL``, render templates, ``send_mail(fail_silently=False)``,
catch + log, and return a bool so an alert hiccup cannot mask the underlying eval
failure. The SLO digest helper also catches internally but returns an explicit
three-state outcome; its task boundary decides whether QStash should see failure.

The body carries ONLY content-safe metadata: suite, run id, git_sha, image_tag,
trigger, passed/total counts, failed case_ids, timestamps. Those are content-safe
by construction — ``record()`` / ``_assert_details_safe`` already scrubbed every
``EvalResult`` and ``case_id`` is a bounded single-line id
(docs/evals-directive.md INVARIANT #1). No message text, no decrypted value, no
PII ever reaches this email.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from apps.evals.models import EvalRun

logger = logging.getLogger(__name__)

SloDigestOutcome = Literal["sent", "skipped_no_owner", "failed"]
_ALWAYS_LOUD_FAILURE_CASE_IDS = frozenset(
    {
        # The shared budget is exhausted or its operator kill-switch is active,
        # making every tenant silent rather than one eval probe unhealthy.
        "chat_roundtrip_global_cap",
    }
)


def _legacy_alert_allowed(*, kind: str, run: EvalRun | None = None) -> bool:
    """Apply the single mute policy for per-run failure and reaper emails."""
    if getattr(settings, "EVAL_EMAIL_ALERTS_ENABLED", False):
        return True

    if run is not None:
        try:
            systemic_failure = run.results.filter(
                passed=False,
                case_id__in=_ALWAYS_LOUD_FAILURE_CASE_IDS,
            ).exists()
        except Exception:
            # Classification uncertainty must not suppress a possible fleet-wide
            # outage. The send helper remains best-effort below.
            logger.exception(
                "eval alert: systemic-failure classification failed; keeping alert loud suite=%s run=%s",
                run.suite,
                run.id,
            )
            return True
        if systemic_failure:
            logger.error(
                "eval alert: systemic outage bypasses disabled legacy alerts suite=%s run=%s",
                run.suite,
                run.id,
            )
            return True

    logger.info("%s: skipped because EVAL_EMAIL_ALERTS_ENABLED is false", kind)
    return False


def send_eval_failure_alert(run: EvalRun) -> bool:
    """Email the platform owner that an eval run did not pass. Returns True iff sent.

    Best-effort: returns False (and logs) when ``PLATFORM_OWNER_EMAIL`` is unset or
    the send raises — it NEVER propagates an exception (the caller's DLQ-raise is
    the real failure signal; a mail hiccup must not compound it).
    """
    if run.status == EvalRun.Status.DEGRADED:
        logger.info(
            "eval alert: degraded runs are digest-only suite=%s run=%s",
            run.suite,
            run.id,
        )
        return False

    owner_email = getattr(settings, "PLATFORM_OWNER_EMAIL", "")
    if not owner_email:
        logger.warning("eval alert: PLATFORM_OWNER_EMAIL not set — %s failure alert skipped", run.suite)
        return False
    if not _legacy_alert_allowed(kind="eval alert", run=run):
        return False
    from apps.steward.gate import (
        record_sent,
        record_suppressed,
        release_failed,
        should_send,
    )

    fingerprint = f"eval-email:{run.suite}:{run.status}"
    reservation = should_send(fingerprint, timedelta(hours=24))
    if reservation is None:
        record_suppressed(fingerprint)
        logger.info("eval alert: suppressed by cooldown fingerprint=%s", fingerprint)
        return False

    try:
        results = list(run.results.all())
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed_case_ids = [r.case_id for r in results if not r.passed]

        ctx = {
            "suite": run.suite,
            "run_id": run.id,
            "status": run.status,
            "trigger": run.trigger,
            "git_sha": run.git_sha,
            "image_tag": run.image_tag or "",
            "passed": passed,
            "total": total,
            "failed_case_ids": failed_case_ids,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }
        subject = render_to_string("email/evals/failure_subject.txt", ctx).strip()
        body = render_to_string("email/evals/failure_body.txt", ctx)
        sent = send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[owner_email],
            fail_silently=False,
        )
    except Exception:
        release_failed(fingerprint, reservation)
        logger.exception("eval alert: failure email send failed for run %s", run.id)
        return False
    if sent == 0:
        release_failed(fingerprint, reservation)
        logger.error("eval alert: email backend reported zero deliveries for run %s", run.id)
        return False
    record_sent(fingerprint)
    return True


def send_reaped_eval_runs_alert(runs: list[EvalRun]) -> bool:
    """Send one metadata-only alert for a batch of reaped eval runs."""
    if not runs:
        return False
    owner_email = getattr(settings, "PLATFORM_OWNER_EMAIL", "")
    if not owner_email:
        logger.warning("eval reaper alert: PLATFORM_OWNER_EMAIL not set — batch skipped")
        return False
    if not _legacy_alert_allowed(kind="eval reaper alert"):
        return False
    from apps.steward.gate import (
        record_sent,
        record_suppressed,
        release_failed,
        should_send,
    )

    fingerprint = "eval-email:reaper"
    reservation = should_send(fingerprint, timedelta(hours=6))
    if reservation is None:
        record_suppressed(fingerprint, count=len(runs))
        logger.info("eval reaper alert: suppressed by cooldown fingerprint=%s", fingerprint)
        return False

    run_ids = ", ".join(str(run.id) for run in runs)
    suites = ", ".join(sorted({run.suite for run in runs}))
    subject = f"[EVAL] {len(runs)} stuck eval run(s) reaped"
    body = f"Count: {len(runs)}\nRun IDs: {run_ids}\nSuites: {suites}\nStatus: error\n"
    try:
        sent = send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[owner_email],
            fail_silently=False,
        )
    except Exception:
        release_failed(fingerprint, reservation)
        logger.exception("eval reaper alert: batch email send failed")
        return False
    if sent == 0:
        release_failed(fingerprint, reservation)
        logger.error("eval reaper alert: email backend reported zero deliveries")
        return False
    record_sent(fingerprint)
    return True


def send_slo_digest(subject: str, body: str) -> SloDigestOutcome:
    """Email the platform owner the weekly SLO digest; return its delivery outcome.

    The helper remains non-raising so callers receive one of three explicit states:
    ``sent``, ``skipped_no_owner`` (benign configuration gate), or ``failed``
    (attempted delivery did not complete). The task boundary turns only ``failed``
    into a cron failure. ``subject`` and ``body`` are pre-rendered by
    ``build_weekly_digest`` and carry only metric ids, thresholds, and counts —
    content-free by construction (INVARIANT #1).
    """
    owner_email = getattr(settings, "PLATFORM_OWNER_EMAIL", "")
    if not owner_email:
        logger.warning("slo digest: PLATFORM_OWNER_EMAIL not set — weekly digest skipped")
        return "skipped_no_owner"

    try:
        sent = send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[owner_email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("slo digest: weekly digest email send failed")
        return "failed"
    if sent == 0:
        logger.error("slo digest: weekly digest email backend reported zero deliveries")
        return "failed"
    return "sent"
