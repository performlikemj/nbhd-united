"""Liveness health-check endpoint for the CI deploy gate and load balancers.

A plain Django view (not DRF) so it skips the project's auth/permission classes
and answers unauthenticated probes. Returns 200 when the WSGI app has booted and
can route a request — exactly the signal the deploy gate needs, since a broken
or crash-looping image returns 502/timeout instead.

Deliberately does NOT touch the database. This control plane runs behind a
Supavisor pooler that occasionally drops idle connections; coupling the deploy
gate / LB liveness to a transient pooler hiccup would cause false deploy failures
and needless restarts. Real database faults surface as errors in Sentry, not
here. If a readiness (DB-touching) probe is ever needed, add it as a SEPARATE
path (e.g. /health/ready/) so liveness stays decoupled.
"""

from django.http import JsonResponse


def health(request):
    return JsonResponse({"status": "ok"})
