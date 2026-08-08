"""Liveness/readiness + build-identity health endpoint for deploy gates and load balancers.

A plain Django view (not DRF) so it skips the project's auth/permission classes
and answers unauthenticated probes. Returns 200 when the WSGI app has booted and
can route a request — exactly the signal the deploy gate needs, since a broken
or crash-looping image returns 502/timeout instead.

The response also carries a ``build`` field echoing ``settings.SENTRY_RELEASE``.
The deploy step stamps ``SENTRY_RELEASE=<git-sha>`` onto the container app env
(ci-cd.yml), so the CI gate can poll this endpoint and confirm the NEW revision —
not merely *some* healthy revision — is serving traffic before it fires
post-deploy side effects. This closes the 2026-07-09 all-green race where the
old revision answered a blind 200-poll while production served stale code.
``build`` is additive: a revision deployed before this field existed serves
``/health/`` WITHOUT the key, and the CI gate treats missing/empty as not-ready.

Deliberately does NOT touch the database. This control plane runs behind a
Supavisor pooler that occasionally drops idle connections; coupling the deploy
gate / LB liveness to a transient pooler hiccup would cause false deploy failures
and needless restarts. Real database faults surface as errors in Sentry, not
here. Azure readiness intentionally uses this path as an HTTP probe. Gunicorn
cannot dispatch the request until ``post_worker_init`` has returned, so a 200
also proves that this worker's PII warm-up resolved (loaded or failed-and-cached)
without making liveness depend on the model or database. Keep the warm-up
synchronous if this invariant changes. The build field is a pure settings read,
so the endpoint retains its DB-free property.
"""

from django.conf import settings
from django.http import JsonResponse


def health(request):
    # getattr default "" guards a settings module that omits SENTRY_RELEASE
    # (it is defined with default "" in base.py, so this is belt-and-suspenders).
    return JsonResponse({"status": "ok", "build": getattr(settings, "SENTRY_RELEASE", "")})
