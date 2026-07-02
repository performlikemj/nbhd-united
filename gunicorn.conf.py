"""Gunicorn server hooks.

Loaded via ``-c gunicorn.conf.py`` in startup.sh. CLI flags in startup.sh
still apply (they override file settings of the same name; we define none).
"""


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
    try:
        from apps.pii.engine import get_pii_pipeline

        get_pii_pipeline()
        worker.log.info("post_worker_init: PII pipeline warmed")
    except Exception as exc:
        worker.log.warning("post_worker_init: PII warm skipped (%s)", exc)
