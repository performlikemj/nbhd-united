"""Tenant-scoped, tag-invalidated cache decorator for DRF views.

Cache keys are derived from (view qualname, tenant id, tag version, request
path, sorted query params). A mutation bumps the matching tag's version,
which invalidates every cached read for that tenant + tag in a single INCR,
without scanning Redis or tracking per-key membership.

Falls back to LocMemCache when Redis is unavailable so local development and
tests work without external dependencies; in that case invalidation is still
per-process correct but does not propagate across workers.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from functools import wraps

from django.core.cache import cache
from rest_framework.response import Response

logger = logging.getLogger("nbhd.cache")

DEFAULT_TTL = 60
PING_KEY = "nbhd:health:cache-ping"

# How long a tag version stays alive in Redis. Long enough that we don't
# accidentally invalidate by expiry; short enough that an idle tenant doesn't
# leak version state forever.
TAG_VERSION_TTL = 60 * 60 * 24 * 30  # 30 days


def tag_version_key(tenant_id, tag: str) -> str:
    return f"nbhd:tag:{tenant_id}:{tag}:v"


def get_tag_version(tenant_id, tag: str) -> int:
    """Return the current version for (tenant, tag), seeding it to 1 if absent."""
    key = tag_version_key(tenant_id, tag)
    version = cache.get(key)
    if version is None:
        # add() is atomic: if another process raced us, we read theirs back.
        cache.add(key, 1, TAG_VERSION_TTL)
        version = cache.get(key) or 1
    return int(version)


def bump_tag(tenant_id, tag: str) -> int:
    """Atomically advance the tag version, invalidating all reads bound to it."""
    key = tag_version_key(tenant_id, tag)
    try:
        return int(cache.incr(key))
    except ValueError:
        # Key didn't exist yet; seed and re-incr.
        cache.add(key, 1, TAG_VERSION_TTL)
        try:
            return int(cache.incr(key))
        except ValueError:
            return 1


def bump_tags(tenant_id, tags: Iterable[str]) -> None:
    for tag in tags:
        bump_tag(tenant_id, tag)


def _request_signature(request, kwargs: dict) -> str:
    parts = [
        request.path,
        "&".join(f"{k}={v}" for k, v in sorted(request.query_params.items())),
        "&".join(f"{k}={v}" for k, v in sorted((kwargs or {}).items())),
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


def _view_qualname(view_self) -> str:
    cls = view_self.__class__
    return f"{cls.__module__}.{cls.__name__}"


def tenant_cache(ttl: int = DEFAULT_TTL, tag: str = "default"):
    """Cache GETs for the wrapped DRF view method, scoped to the request's tenant.

    Skipped when:
      - request method is not GET
      - request has no resolvable tenant (anon, no-tenant user)
      - upstream view returns a non-200 response (we don't memoize errors)

    Cached payload is `(response.data, status_code, content_type)`. We re-wrap
    into a fresh DRF `Response` on hit so middleware (ETag, Cache-Control) can
    set headers the same way it would on a miss.
    """

    def decorator(view_method):
        @wraps(view_method)
        def wrapper(self, request, *args, **kwargs):
            if request.method != "GET":
                return view_method(self, request, *args, **kwargs)
            tenant = getattr(request.user, "tenant", None)
            if tenant is None:
                return view_method(self, request, *args, **kwargs)

            tag_v = get_tag_version(tenant.id, tag)
            sig = _request_signature(request, kwargs)
            key = f"nbhd:view:{_view_qualname(self)}:{tenant.id}:{tag}:{tag_v}:{sig}"

            cached = cache.get(key)
            if cached is not None:
                response = Response(cached["data"], status=cached["status"])
                response["X-Cache"] = "HIT"
                return response

            response = view_method(self, request, *args, **kwargs)
            if response.status_code == 200:
                try:
                    cache.set(
                        key,
                        {"data": response.data, "status": response.status_code},
                        ttl,
                    )
                except Exception:
                    logger.exception("tenant_cache set failed for %s", key)
            response["X-Cache"] = "MISS"
            return response

        wrapper._tenant_cache_meta = {"ttl": ttl, "tag": tag}
        return wrapper

    return decorator


def ping() -> bool:
    """Smoke-test the cache backend with a 5s key round-trip."""
    cache.set(PING_KEY, "ok", 5)
    return cache.get(PING_KEY) == "ok"
