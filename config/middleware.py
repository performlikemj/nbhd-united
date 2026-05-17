"""Custom project-level middleware."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from django.db import connection

logger = logging.getLogger("nbhd.perf")


@contextmanager
def _count_queries():
    counter = {"n": 0}

    def wrapper(execute, sql, params, many, context):
        counter["n"] += 1
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        yield counter


class RequestTimingMiddleware:
    """Log per-request timing and DB query count to stdout.

    Format: `PERF method path status=N total_ms=N db_queries=N`. Visible in
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

        logger.info(
            "PERF %s %s status=%d total_ms=%d db_queries=%d",
            request.method,
            path,
            response.status_code,
            total_ms,
            counter["n"],
        )
        return response
