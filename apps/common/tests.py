"""Tests for tenant_cache decorator + ETag middleware."""

from __future__ import annotations

import gc
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.utils.inspect import _get_func_parameters
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from apps.common.cache import (
    PING_KEY,
    bump_tag,
    get_tag_version,
    ping,
    tag_version_key,
    tenant_cache,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from config.cache_middleware import ETagMiddleware

User = get_user_model()


class CachePrimitivesTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_ping_round_trips(self):
        self.assertTrue(ping())
        self.assertEqual(cache.get(PING_KEY), "ok")

    def test_get_tag_version_seeds_to_one(self):
        tid = uuid.uuid4()
        self.assertEqual(get_tag_version(tid, "fuel"), 1)
        self.assertEqual(cache.get(tag_version_key(tid, "fuel")), 1)

    def test_bump_tag_advances_version(self):
        tid = uuid.uuid4()
        get_tag_version(tid, "fuel")
        v2 = bump_tag(tid, "fuel")
        v3 = bump_tag(tid, "fuel")
        self.assertEqual(v2, 2)
        self.assertEqual(v3, 3)

    def test_bump_tag_seeds_when_missing(self):
        tid = uuid.uuid4()
        # No prior seed; bump should still succeed.
        self.assertGreaterEqual(bump_tag(tid, "fresh-tag"), 1)


class _CountingView(APIView):
    """Test view: counts calls, returns the count + a request-supplied label."""

    permission_classes = [IsAuthenticated]
    calls = 0

    @tenant_cache(ttl=60, tag="fuel")
    def get(self, request):
        _CountingView.calls += 1
        return Response({"calls": _CountingView.calls, "label": request.query_params.get("label", "x")})


class TenantCacheDecoratorTest(TestCase):
    def setUp(self):
        cache.clear()
        _CountingView.calls = 0
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            username=f"u-{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            password="x",
        )
        self.tenant = Tenant.objects.create(user=self.user)
        # Refresh so request.user.tenant resolves via the OneToOne reverse FK.
        self.user.refresh_from_db()

    def _authed_request(self, path):
        request = self.factory.get(path)
        force_authenticate(request, user=self.user)
        return request

    def test_second_get_hits_cache(self):
        view = _CountingView.as_view()
        r1 = view(self._authed_request("/_test/?label=a"))
        r2 = view(self._authed_request("/_test/?label=a"))
        self.assertEqual(r1.data["calls"], 1)
        self.assertEqual(r2.data["calls"], 1, "second GET should not invoke view body")
        self.assertEqual(r2["X-Cache"], "HIT")
        self.assertEqual(r1["X-Cache"], "MISS")

    def test_different_query_params_dont_collide(self):
        view = _CountingView.as_view()
        r_a = view(self._authed_request("/_test/?label=a"))
        r_b = view(self._authed_request("/_test/?label=b"))
        self.assertEqual(r_a.data["label"], "a")
        self.assertEqual(r_b.data["label"], "b")
        self.assertEqual(_CountingView.calls, 2)

    def test_bump_tag_invalidates(self):
        view = _CountingView.as_view()
        view(self._authed_request("/_test/?label=a"))
        bump_tag(self.tenant.id, "fuel")
        r2 = view(self._authed_request("/_test/?label=a"))
        self.assertEqual(_CountingView.calls, 2)
        self.assertEqual(r2["X-Cache"], "MISS")

    def test_non_200_not_cached(self):
        class FailView(APIView):
            permission_classes = [IsAuthenticated]
            calls = 0

            @tenant_cache(ttl=60, tag="fuel")
            def get(self, request):
                FailView.calls += 1
                return Response({"err": "x"}, status=500)

        view = FailView.as_view()
        view(self._authed_request("/_fail/"))
        view(self._authed_request("/_fail/"))
        self.assertEqual(FailView.calls, 2, "500s must not be memoized")

    def test_post_not_cached(self):
        view = _CountingView.as_view()
        request = self.factory.post("/_test/")
        force_authenticate(request, user=self.user)
        view(request)  # 405 from DRF since _CountingView only defines get()
        self.assertEqual(_CountingView.calls, 0)


class ETagMiddlewareTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _mw(self, response):
        def get_response(_request):
            return response

        return ETagMiddleware(get_response)

    def test_sets_etag_on_200_get(self):
        from django.http import JsonResponse

        response = JsonResponse({"a": 1})
        mw = self._mw(response)
        result = mw(self.factory.get("/x/"))
        self.assertIn("ETag", result)
        self.assertTrue(result["ETag"].startswith('"'))
        self.assertEqual(result["Cache-Control"], "private, max-age=10, stale-while-revalidate=60")
        self.assertIn("Authorization", result["Vary"])

    def test_returns_304_on_match(self):
        from django.http import JsonResponse

        response = JsonResponse({"a": 1})
        mw = self._mw(response)
        # First request — get the ETag.
        first = mw(self.factory.get("/x/"))
        etag = first["ETag"]
        # Second request with If-None-Match should yield 304.
        # Re-build middleware because the response object is single-use after render.
        response2 = JsonResponse({"a": 1})
        mw2 = self._mw(response2)
        second = mw2(self.factory.get("/x/", HTTP_IF_NONE_MATCH=etag))
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second["ETag"], etag)

    def test_skips_non_200(self):
        from django.http import JsonResponse

        response = JsonResponse({"err": 1}, status=500)
        mw = self._mw(response)
        result = mw(self.factory.get("/x/"))
        self.assertNotIn("ETag", result)


class CacheSignalReceiverLivenessTest(TestCase):
    """Regression test for the dead cache-invalidation-receiver bug.

    ``apps/common/cache_signals.py`` registers its post_save/post_delete
    receivers as LOCAL functions inside ``_register()``, connected with
    Django's default ``weak=True``. Once ``_register()`` returns, nothing
    holds a strong reference to those closures, so in any process that runs
    with ``settings.DEBUG=False`` (every real deployment — see
    ``config/settings/production.py``) they are garbage collected
    immediately and NEVER fire again.

    Why this doesn't fail on a normal local/test run: with
    ``settings.DEBUG=True`` (``config/settings/development.py``,
    ``config/settings/test.py``), ``Signal.connect()`` runs an extra
    ``func_accepts_kwargs()`` validity check that calls
    ``django.utils.inspect._get_func_parameters`` — an ``@lru_cache``'d
    function whose cache key includes the receiver function itself. That
    incidentally keeps the "weak" receiver alive for the life of the process,
    masking this bug in every local/test run. A real production process
    never takes that code path (it's gated on ``DEBUG``), so it gets no such
    accidental protection. We clear that cache below to strip the incidental
    protection and reproduce the real (production) behavior.
    """

    def setUp(self):
        cache.clear()
        _get_func_parameters.cache_clear()
        gc.collect()

    def test_tenant_save_bumps_tenant_cache_tag(self):
        tenant = create_tenant(display_name="Cache Signal Regression", telegram_chat_id=918273645)

        before = get_tag_version(tenant.id, "tenant")
        tenant.status = Tenant.Status.ACTIVE
        tenant.save(update_fields=["status", "updated_at"])
        after = get_tag_version(tenant.id, "tenant")

        self.assertGreater(
            after,
            before,
            "Tenant post_save did not bump the 'tenant' cache tag — the "
            "cache_signals receiver is dead (see class docstring).",
        )


class TenantCacheSignalFunctionalTest(TestCase):
    """End-to-end: GET /api/v1/tenants/me/ (tenant_cache tag='tenant') must
    reflect a tenant write immediately, not after the 60s TTL. This is the
    literal production symptom: PATCH a tenant setting (200), then GET
    /tenants/me/ shortly after still serves the pre-write value.
    """

    def setUp(self):
        cache.clear()
        _get_func_parameters.cache_clear()
        gc.collect()
        self.tenant = create_tenant(display_name="Cache Signal Functional", telegram_chat_id=918273646)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def test_me_reflects_fresh_data_after_tenant_write(self):
        r1 = self.client.get("/api/v1/tenants/me/")
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(r1.data["core_enabled"])

        self.tenant.core_enabled = True
        self.tenant.save(update_fields=["core_enabled"])

        r2 = self.client.get("/api/v1/tenants/me/")
        self.assertTrue(
            r2.data["core_enabled"],
            "GET /tenants/me/ served a stale cached response after a tenant "
            "write — the 'tenant' cache tag was not bumped.",
        )
