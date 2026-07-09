"""Liveness + build-identity health-check endpoint for the CI deploy gate and load balancers.

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
here. If a readiness (DB-touching) probe is ever needed, add it as a SEPARATE
path (e.g. /health/ready/) so liveness stays decoupled. The build field is a
pure settings read, so it keeps that DB-free property.
"""

from django.conf import settings
from django.http import JsonResponse


def health(request):
    # getattr default "" guards a settings module that omits SENTRY_RELEASE
    # (it is defined with default "" in base.py, so this is belt-and-suspenders).
    return JsonResponse({"status": "ok", "build": getattr(settings, "SENTRY_RELEASE", "")})
