"""Generic tool-contract capture for runtime (oc-* container → Django) calls.

Why middleware and not the auth helper: `validate_internal_runtime_request`
(apps/integrations/internal_auth.py) is the one function every runtime call passes
through, but it runs BEFORE the view, so it cannot see the outcome or the latency —
the two things telemetry exists to record. And its ergonomic wrapper
`_internal_auth_or_401` is copy-pasted into nine modules, with four more call sites
inlining the validator and `apps/actions` using different headers entirely; hooking
"it" would mean editing fifteen places and would still miss the next copy.

The middleware is the only genuinely single point that sees endpoint, tenant,
status and duration together, and it captures new runtime endpoints the day they
are added with zero per-tool wiring.

Selection is by path: every runtime mount contains `/runtime/`
(`/api/v1/<app>/runtime/<tenant_id>/...`, `/api/v1/internal/runtime/...`,
`/api/cron/runtime/...`). See docs/agents/telemetry.md for what this does NOT cover.
"""

from __future__ import annotations

import logging
import time

from .telemetry import emit_tool_event

logger = logging.getLogger(__name__)

RUNTIME_PATH_MARKER = "/runtime/"


def _outcome_for(status_code: int) -> str:
    # Imported lazily: middleware is constructed during app loading.
    from .models import ToolContractEvent

    if status_code >= 500:
        return ToolContractEvent.Outcome.ERROR
    if status_code >= 400:
        return ToolContractEvent.Outcome.REJECTED
    return ToolContractEvent.Outcome.ACCEPTED


def _app_segment(path: str) -> str:
    """The route segment a runtime mount lives under ('fuel', 'internal', 'cron').

    Derived from the fixed route prefix, so it is structural rather than
    user-supplied. On a 404 the path is arbitrary — the emitter's code-shape guard
    drops anything that does not look like a slug.
    """
    head = path.split(RUNTIME_PATH_MARKER, 1)[0]
    segments = [segment for segment in head.split("/") if segment]
    return segments[-1] if segments else ""


class ToolTelemetryMiddleware:
    """Emit one ToolContractEvent per runtime endpoint call.

    Placed directly inside RequestTimingMiddleware so the recorded duration covers
    the whole inner stack (auth, RLS, view) — i.e. what the calling container
    actually waited for.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if RUNTIME_PATH_MARKER not in request.path:
            return self.get_response(request)

        start = time.perf_counter()
        response = self.get_response(request)
        duration_ms = int((time.perf_counter() - start) * 1000)

        try:
            self._record(request, response, duration_ms)
        except Exception:
            # emit_tool_event is already fail-open; this guards the argument
            # gathering above it (a torn request object must not surface as a 500).
            logger.warning("runtime telemetry capture failed for %s", request.path, exc_info=True)

        return response

    def _record(self, request, response, duration_ms: int) -> None:
        match = getattr(request, "resolver_match", None)

        # url_name is a developer-authored slug and stable across refactors. When
        # resolution failed (404) there is no trustworthy name — the request path
        # is attacker-controlled, so we record the miss rather than echo it.
        tool_name = (match.url_name if match and match.url_name else None) or "unresolved"
        tenant_id = match.kwargs.get("tenant_id") if match else None

        status_code = getattr(response, "status_code", 0)
        emit_tool_event(
            namespace="runtime",
            tool_name=tool_name,
            tenant_id=tenant_id,
            outcome=_outcome_for(status_code),
            reason_code="" if status_code < 400 else f"http_{status_code}",
            detail={
                "status": status_code,
                "method": request.method,
                "app": _app_segment(request.path),
            },
            duration_ms=duration_ms,
        )
