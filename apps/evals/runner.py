"""Eval runner chassis — the three calls every suite is built on.

    run = open_run("behavior", EvalRun.Trigger.SCHEDULED)
    record(run, "case-1", EvalResult.Kind.BEHAVIOR, passed=True, score=0.91, threshold=0.80)
    close_run(run)   # computes status, stamps finished_at, logs ONE summary line

Contract (docs/evals-directive.md):
  * ``close_run`` derives the run status from its cases: any failed case ->
    ``fail``; zero cases recorded -> ``error`` (a suite that asserted nothing is
    broken, and must never read as a pass); otherwise ``pass``.
  * It emits exactly ONE summary log line so a run is greppable in Log Analytics:
    ``eval <suite>: PASS n/m`` or ``eval <suite>: FAIL n/m [failed case ids]``.
  * The QStash task wrapper RAISES when a run does not close ``pass``, so a
    failing eval lands in the DLQ instead of reporting a silent green — the same
    contract as ``crypto_roundtrip_smoke``.

INVARIANT #1: nothing written here may contain real-user content. ``details`` is
counts / ids / durations only (see EvalResult's docstring).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from django.conf import settings
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun

logger = logging.getLogger(__name__)

# Sentinel for open_run's image_tag: distinguishes "caller didn't say, infer the
# fleet tag" (runtime suites) from an explicit ``None`` (non-runtime suites —
# corpus/slo/smoke — which store NULL because they don't exercise a container).
_AUTO_IMAGE_TAG = object()


def open_run(
    suite: str,
    trigger: str,
    *,
    git_sha: str | None = None,
    image_tag: str | None = _AUTO_IMAGE_TAG,
) -> EvalRun:
    """Open a ``running`` EvalRun stamped with the build under test.

    ``git_sha`` defaults to the deployed release (``SENTRY_RELEASE``, set to the
    full sha by the deploy). ``image_tag`` defaults to the fleet's current
    OpenClaw tag for runtime suites; pass ``image_tag=None`` explicitly from a
    suite that doesn't exercise a container (corpus / slo / smoke) to store NULL.
    """
    if git_sha is None:
        git_sha = getattr(settings, "SENTRY_RELEASE", "") or ""
    if image_tag is _AUTO_IMAGE_TAG:
        image_tag = getattr(settings, "OPENCLAW_IMAGE_TAG", None) or None

    return EvalRun.objects.create(
        suite=suite,
        trigger=trigger,
        git_sha=git_sha,
        image_tag=image_tag,
        status=EvalRun.Status.RUNNING,
    )


# INVARIANT #1 chokepoint constants (docs/evals-directive.md §1.1). ``details`` is
# counts/ids/durations/scores/labels ONLY — never message text, a decrypted
# value, or PII. Long strings are where a transcript / judge-rationale hides;
# opaque objects are where anything hides. So we bound both.
_MAX_DETAILS_STR = 64  # a label/id/tool-name is short; a sentence is not
_MAX_DETAILS_DEPTH = 2  # a flat metrics dict, or one level of grouping — no trees
_MAX_CASE_ID = 64


def _assert_details_safe(details: dict) -> None:
    """Fail CLOSED if ``details`` could smuggle content into the eval pipeline.

    This is the single enforcement chokepoint every suite inherits by going
    through ``record()`` — the same shape as the SMB write-sanitize chokepoint in
    ``apps/orchestrator/azure_client._put_share_file`` (one place enforces the
    safety property so no producer can bypass it). A Wave-D author who stores a
    judge's free-text rationale (which quotes a transcript) is stopped HERE.

    A value is safe iff it nests at most ``_MAX_DETAILS_DEPTH`` levels, every leaf
    is ``int``/``float``/``bool``/``str``/``None``, and every ``str`` leaf is
    ``<= _MAX_DETAILS_STR`` chars. Raises ``ValueError`` otherwise — and the error
    message reports only the JSON path + a length/type, NEVER the offending value,
    so the guard cannot itself leak.
    """
    if not isinstance(details, dict):
        raise ValueError(f"eval details must be a dict, got {type(details).__name__}")

    def _walk(node, depth: int, path: str) -> None:
        if isinstance(node, dict):
            if depth >= _MAX_DETAILS_DEPTH:
                raise ValueError(f"eval details nested too deep at {path} (max depth {_MAX_DETAILS_DEPTH})")
            for k, v in node.items():
                _walk(v, depth + 1, f"{path}.{k}")
        elif isinstance(node, (list, tuple)):
            if depth >= _MAX_DETAILS_DEPTH:
                raise ValueError(f"eval details nested too deep at {path} (max depth {_MAX_DETAILS_DEPTH})")
            for i, v in enumerate(node):
                _walk(v, depth + 1, f"{path}[{i}]")
        elif isinstance(node, str):
            if len(node) > _MAX_DETAILS_STR:
                # Report the LENGTH only — never the string (it may be content).
                raise ValueError(
                    f"eval details string at {path} is {len(node)} chars (max {_MAX_DETAILS_STR}); "
                    "eval details carry counts/ids/labels only, never content"
                )
        elif isinstance(node, (int, float, bool)) or node is None:
            return
        else:
            raise ValueError(
                f"eval details leaf at {path} is a {type(node).__name__}; "
                "only int/float/bool/str/None leaves are allowed"
            )

    _walk(details, 0, "details")


def record(
    run: EvalRun,
    case_id: str,
    kind: str,
    passed: bool,
    *,
    score=None,
    threshold=None,
    details: dict | None = None,
    judge_model: str = "",
    rubric_version: str = "",
) -> EvalResult:
    """Record one case outcome against ``run``.

    ``details`` MUST be counts / ids / durations / scores / labels only — never
    message content or any user data (INVARIANT #1). This is enforced here at the
    chokepoint: ``details`` is validated (fail-closed) and ``case_id`` is bounded
    to a short, single-line id before anything is written.
    """
    details = details or {}
    _assert_details_safe(details)
    # Bound case_id to a short single-line id (no smuggling content via the id).
    case_id = (case_id or "").replace("\n", " ").replace("\r", " ").strip()[:_MAX_CASE_ID]

    return EvalResult.objects.create(
        run=run,
        case_id=case_id,
        kind=kind,
        passed=bool(passed),
        score=score,
        threshold=threshold,
        details=details,
        judge_model=judge_model or "",
        rubric_version=rubric_version or "",
    )


def close_run(run: EvalRun) -> EvalRun:
    """Compute the run's status from its cases, stamp it, and log one summary line."""
    results = list(run.results.all())
    total = len(results)
    passed_n = sum(1 for r in results if r.passed)
    failed_ids = [r.case_id for r in results if not r.passed]

    if total == 0:
        # A suite that recorded nothing asserted nothing — that is a broken
        # suite, not a pass. Fail loudly rather than reporting a vacuous green.
        status = EvalRun.Status.ERROR
    elif failed_ids:
        status = EvalRun.Status.FAIL
    elif run.status == EvalRun.Status.DEGRADED:
        # A suite may explicitly declare that all cases technically passed but
        # its evidence was too incomplete to call the run green.
        status = EvalRun.Status.DEGRADED
    else:
        status = EvalRun.Status.PASS

    run.status = status
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "finished_at"])

    if status == EvalRun.Status.PASS:
        logger.info("eval %s: PASS %d/%d", run.suite, passed_n, total)
    elif status == EvalRun.Status.DEGRADED:
        logger.warning("eval %s: DEGRADED %d/%d", run.suite, passed_n, total)
    elif status == EvalRun.Status.FAIL:
        logger.error("eval %s: FAIL %d/%d %s", run.suite, passed_n, total, failed_ids)
    else:
        logger.error("eval %s: ERROR 0/0 — suite recorded no cases", run.suite)

    return run


@contextmanager
def record_run(suite: str, trigger: str, *, git_sha: str | None = None, image_tag=_AUTO_IMAGE_TAG):
    """Open a run, yield it for the suite to ``record()`` into, and always close it.

    The safe way to run a suite: a crash BETWEEN ``open_run`` and ``close_run``
    (worker OOM, KeyError mid-suite) would otherwise strand the row at
    ``status='running'`` forever. On any exception escaping the block this closes
    the run as ``error`` + stamps ``finished_at`` before re-raising (fail closed —
    a phantom 'running' never reads as "still going"). On clean exit it closes
    normally (``close_run`` derives pass/fail/error from the recorded cases).

    Wave B TODO: a periodic reaper sweep should flip any run still ``running``
    past a timeout (e.g. the worker was killed so hard this ``finally`` never ran)
    to ``error``. Not built here.
    """
    run = open_run(suite, trigger, git_sha=git_sha, image_tag=image_tag)
    try:
        yield run
    except Exception:
        run.status = EvalRun.Status.ERROR
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        logger.error("eval %s: ERROR — exception during run (closed as error)", run.suite)
        raise
    else:
        close_run(run)
