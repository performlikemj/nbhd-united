"""Eval-failure alerting — a content-free email to the platform owner (Wave B).

Modeled EXACTLY on ``apps/router/line_quota_handlers.handle_pre_warn``: gate on
``PLATFORM_OWNER_EMAIL``, render templates, ``send_mail(fail_silently=False)``,
catch + log, return a bool, and NEVER raise into the caller — a failed alert must
not mask the underlying eval failure or crash the task boundary.

The body carries ONLY content-safe metadata: suite, run id, git_sha, image_tag,
trigger, passed/total counts, failed case_ids, timestamps. Those are content-safe
by construction — ``record()`` / ``_assert_details_safe`` already scrubbed every
``EvalResult`` and ``case_id`` is a bounded single-line id
(docs/evals-directive.md INVARIANT #1). No message text, no decrypted value, no
PII ever reaches this email.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from apps.evals.models import EvalRun

logger = logging.getLogger(__name__)


def send_eval_failure_alert(run: EvalRun) -> bool:
    """Email the platform owner that an eval run did not pass. Returns True iff sent.

    Best-effort: returns False (and logs) when ``PLATFORM_OWNER_EMAIL`` is unset or
    the send raises — it NEVER propagates an exception (the caller's DLQ-raise is
    the real failure signal; a mail hiccup must not compound it).
    """
    owner_email = getattr(settings, "PLATFORM_OWNER_EMAIL", "")
    if not owner_email:
        logger.warning("eval alert: PLATFORM_OWNER_EMAIL not set — %s failure alert skipped", run.suite)
        return False

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

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[owner_email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("eval alert: failure email send failed for run %s", run.id)
        return False
    return True
