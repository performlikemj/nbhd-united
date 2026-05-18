"""ETag + default Cache-Control middleware.

Sits after RequestTimingMiddleware. For 200 GETs with a renderable body we:

  1. Hash the response body to a strong ETag.
  2. Compare to `If-None-Match`; on match, return 304 with empty body so the
     client reuses its copy (saves transit, not server work).
  3. Set `Cache-Control: private, max-age=10, stale-while-revalidate=60` so
     browsers and React Query reuse the response for 10s and refetch in the
     background for another 60s when stale.

We deliberately skip non-GET, non-200, and streaming responses. ETag work
runs after the view (and after `@tenant_cache`) so it covers both hits and
misses identically.
"""

from __future__ import annotations

import hashlib

from django.http import HttpResponseNotModified

_DEFAULT_CACHE_CONTROL = "private, max-age=10, stale-while-revalidate=60"


class ETagMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.method != "GET" or response.status_code != 200:
            return response
        if getattr(response, "streaming", False):
            return response
        # DRF may defer rendering; force it so we can hash content.
        if hasattr(response, "accepted_renderer") and not getattr(response, "_is_rendered", False):
            response.render()
        if not hasattr(response, "content"):
            return response

        etag = '"' + hashlib.md5(response.content, usedforsecurity=False).hexdigest() + '"'
        response["ETag"] = etag

        if request.META.get("HTTP_IF_NONE_MATCH") == etag:
            not_modified = HttpResponseNotModified()
            not_modified["ETag"] = etag
            for header in ("Cache-Control", "Vary"):
                if header in response:
                    not_modified[header] = response[header]
            return not_modified

        if "Cache-Control" not in response:
            response["Cache-Control"] = _DEFAULT_CACHE_CONTROL
        # Auth-bearing responses must vary on Authorization; otherwise a CDN
        # or proxy could leak tenant A's body to tenant B.
        existing_vary = response.get("Vary", "")
        if "Authorization" not in existing_vary:
            response["Vary"] = f"{existing_vary}, Authorization" if existing_vary else "Authorization"

        return response
