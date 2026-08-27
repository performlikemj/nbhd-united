"""Gunicorn server hooks.

Loaded via ``-c gunicorn.conf.py`` in startup.sh. CLI flags in startup.sh
still apply (they override file settings of the same name; we define none).
"""

import math
import os
import time


def _warm_wait_seconds() -> float:
    try:
        value = float(os.environ.get("PII_SHARED_WARM_WAIT_S", "90"))
    except (TypeError, ValueError):
        return 90.0
    return value if math.isfinite(value) and value >= 0 else 90.0


def _wait_for_shared_detector(worker) -> None:
    from apps.pii.shared_client import ping_shared_detector

    started = time.monotonic()
    wait_s = _warm_wait_seconds()
    deadline = started + wait_s
    while True:
        remaining = deadline - time.monotonic()
        if ping_shared_detector(timeout_s=max(0.01, min(1.0, remaining if remaining > 0 else 0.01))):
            worker.log.info(
                "post_worker_init: PII shared transport ready in %.1f s",
                time.monotonic() - started,
            )
            return
        if remaining <= 0:
            worker.log.warning(
                "post_worker_init: PII shared transport not ready after %.1f s; "
                "failing open (unconfirmed receipts) until ready",
                wait_s,
            )
            return
        time.sleep(min(1.0, remaining))


def post_worker_init(worker):
    """Warm heavy singletons BEFORE the worker accepts traffic.

    The ~554MB PII DeBERTa model otherwise lazy-loads inside the first
    request that calls the redactor — which is usually a user's chat POST.
    Measured in prod: that in-request load blocks the send for 8-114s,
    which outlives the iOS client's 60s transport timeout, so the user sees
    "Something went wrong" for a message that succeeds seconds later. Every
    worker recycle (--max-requests ~1000) re-pays the load, so this fired
    on 11 of 14 days. Warming here moves the cost to worker boot, where the
    sibling worker keeps serving.

    Never fail the worker: the redactor already degrades gracefully to
    pattern recognizers when the model is unavailable (engine caches the
    load error), so a failed warm just restores today's behavior.
    """
    if os.environ.get("PII_DETECTOR_TRANSPORT", "local").strip().lower() == "shared":
        _wait_for_shared_detector(worker)
    else:
        try:
            from apps.pii.engine import get_pii_pipeline

            get_pii_pipeline()
            worker.log.info("post_worker_init: PII pipeline warmed")
        except Exception as exc:
            # get_pii_pipeline() has already cached + logged the root failure.
            # Keep booting, but make the degraded posture unmistakable in the
            # Gunicorn stream too: requests retain pattern-recognizer coverage.
            worker.log.error(
                "post_worker_init: PII pipeline warm failed; neural detection disabled "
                "for this worker, pattern-recognizer fallback remains active (%s)",
                exc,
            )

    # Encryption-at-rest Phase 1 (PR4): best-effort DEK cache pre-warm, dark
    # (nothing decrypts yet — this only populates apps.crypto.cache so a
    # later phase's first decrypt isn't a cold Key Vault unwrap). Runs on
    # its own daemon thread, so this call returns immediately; same
    # never-fail posture as the PII warm above.
    try:
        from apps.crypto.prewarm import start_prewarm_thread

        start_prewarm_thread()
        worker.log.info("post_worker_init: DEK pre-warm thread started")
    except Exception as exc:
        worker.log.warning("post_worker_init: DEK pre-warm skipped (%s)", exc)
