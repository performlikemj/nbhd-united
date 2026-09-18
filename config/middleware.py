"""Custom project-level middleware."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from django.db import connection

logger = logging.getLogger("nbhd.perf")


@contextmanager
def _count_queries():
    # ``db_s`` sums wall time inside cursor.execute — network round-trip plus
    # server time. Like ``n`` it only sees statements: BEGIN/COMMIT and the
    # connection health check never pass through execute_wrapper, so
    # total_ms - db_ms bounds those hidden round-trips plus Python time.
    counter = {"n": 0, "db_s": 0.0}

    def wrapper(execute, sql, params, many, context):
        counter["n"] += 1
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            counter["db_s"] += time.perf_counter() - started

    with connection.execute_wrapper(wrapper):
        yield counter


class RequestTimingMiddleware:
    """Log per-request timing and DB query count to stdout.

    Format: `PERF method path status=N total_ms=N db_queries=N db_ms=N cache=S`. Visible in
    `az containerapp logs show`. Must be the outermost middleware (first
    entry in MIDDLEWARE) so it captures total request time including all
    inner middleware.
    """

    SKIP_PATHS = ("/health", "/static/", "/favicon.ico", "/admin/jsi18n/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if any(path == p or path.startswith(p) for p in self.SKIP_PATHS):
            return self.get_response(request)

        start = time.perf_counter()
        with _count_queries() as counter:
            response = self.get_response(request)
        total_ms = int((time.perf_counter() - start) * 1000)

        cache_state = response.get("X-Cache", "-") if hasattr(response, "get") else "-"
        logger.info(
            "PERF %s %s status=%d total_ms=%d db_queries=%d db_ms=%d cache=%s",
            request.method,
            path,
            response.status_code,
            total_ms,
            counter["n"],
            int(counter["db_s"] * 1000),
            cache_state,
        )
        return response
