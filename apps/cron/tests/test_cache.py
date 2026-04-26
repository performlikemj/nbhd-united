"""Tests for the Phase 1 cron Postgres cache + read-fallback path."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.cron.cache import (
    is_container_unavailable_error,
    read_jobs_from_cache,
    upsert_jobs_to_cache,
)
from apps.cron.gateway_client import GatewayError
from apps.cron.models import CronJob
from apps.tenants.models import Tenant, User


def _make_tenant(*, hibernated=False):
    user = User.objects.create_user(username="cacheuser", password="testpass123")
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_id="oc-cache",
        container_fqdn="oc-cache.internal.azurecontainerapps.io",
        hibernated_at=timezone.now() if hibernated else None,
    )
    return user, tenant


class IsContainerUnavailableErrorTest(TestCase):
    def test_404_treated_as_unavailable(self):
        self.assertTrue(is_container_unavailable_error(GatewayError("nope", status_code=404)))

    def test_502_503_504_treated_as_unavailable(self):
        for code in (502, 503, 504):
            self.assertTrue(is_container_unavailable_error(GatewayError("err", status_code=code)))

    def test_no_status_code_treated_as_unavailable(self):
        # Connection / timeout errors raise GatewayError without a status code.
        self.assertTrue(is_container_unavailable_error(GatewayError("Gateway request failed: ...")))

    def test_azure_splash_body_treated_as_unavailable(self):
        self.assertTrue(
            is_container_unavailable_error(
                GatewayError("Gateway returned 404: <title>Container App - Unavailable</title>", status_code=404)
            )
        )

    def test_400_treated_as_real_error(self):
        self.assertFalse(is_container_unavailable_error(GatewayError("bad request", status_code=400)))

    def test_500_treated_as_real_error(self):
        self.assertFalse(is_container_unavailable_error(GatewayError("server error", status_code=500)))


class UpsertJobsToCacheTest(TestCase):
    def setUp(self):
        _, self.tenant = _make_tenant()

    def test_inserts_new_jobs(self):
        jobs = [
            {"name": "A", "id": "a-1", "enabled": True},
            {"name": "B", "jobId": "b-1", "enabled": False},
        ]
        upsert_jobs_to_cache(self.tenant, jobs)
        rows = list(CronJob.objects.filter(tenant=self.tenant).order_by("name"))
        self.assertEqual([r.name for r in rows], ["A", "B"])
        self.assertEqual(rows[0].gateway_job_id, "a-1")
        self.assertEqual(rows[1].gateway_job_id, "b-1")
        self.assertIsNotNone(rows[0].last_synced_at)

    def test_updates_existing_jobs(self):
        upsert_jobs_to_cache(self.tenant, [{"name": "A", "id": "a-1", "enabled": True}])
        upsert_jobs_to_cache(self.tenant, [{"name": "A", "id": "a-2", "enabled": False}])
        row = CronJob.objects.get(tenant=self.tenant, name="A")
        self.assertEqual(row.gateway_job_id, "a-2")
        self.assertEqual(row.data["enabled"], False)

    def test_removes_stale_jobs(self):
        upsert_jobs_to_cache(self.tenant, [{"name": "A"}, {"name": "B"}])
        upsert_jobs_to_cache(self.tenant, [{"name": "A"}])
        names = list(CronJob.objects.filter(tenant=self.tenant).values_list("name", flat=True))
        self.assertEqual(names, ["A"])

    def test_dedups_by_name_keeping_newest_createdAt(self):
        jobs = [
            {"name": "A", "id": "old", "createdAt": "2025-01-01"},
            {"name": "A", "id": "new", "createdAt": "2025-06-01"},
        ]
        upsert_jobs_to_cache(self.tenant, jobs)
        row = CronJob.objects.get(tenant=self.tenant, name="A")
        self.assertEqual(row.gateway_job_id, "new")

    def test_skips_jobs_without_name(self):
        upsert_jobs_to_cache(self.tenant, [{"name": ""}, {"id": "x"}, {"name": "ok"}])
        names = list(CronJob.objects.filter(tenant=self.tenant).values_list("name", flat=True))
        self.assertEqual(names, ["ok"])


class ReadJobsFromCacheTest(TestCase):
    def setUp(self):
        _, self.tenant = _make_tenant()

    def test_returns_rows_when_present(self):
        upsert_jobs_to_cache(self.tenant, [{"name": "A"}, {"name": "B"}])
        jobs = read_jobs_from_cache(self.tenant)
        self.assertEqual(sorted(j["name"] for j in jobs), ["A", "B"])

    def test_falls_back_to_legacy_snapshot(self):
        self.tenant.cron_jobs_snapshot = {"jobs": [{"name": "Legacy"}]}
        self.tenant.save(update_fields=["cron_jobs_snapshot"])
        jobs = read_jobs_from_cache(self.tenant)
        self.assertEqual([j["name"] for j in jobs], ["Legacy"])

    def test_empty_when_no_cache_and_no_snapshot(self):
        self.assertEqual(read_jobs_from_cache(self.tenant), [])


class CronJobListReadFallbackTest(TestCase):
    def _build_client(self, *, hibernated=False):
        user, tenant = _make_tenant(hibernated=hibernated)
        client = APIClient()
        client.force_authenticate(user=user)
        return client, tenant

    def test_hibernated_tenant_serves_from_cache_without_calling_gateway(self):
        client, tenant = self._build_client(hibernated=True)
        upsert_jobs_to_cache(tenant, [{"name": "Cached", "enabled": True}])

        with patch("apps.cron.tenant_views.invoke_gateway_tool") as mock_invoke:
            resp = client.get("/api/v1/cron-jobs/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([j["name"] for j in resp.json()["jobs"]], ["Cached"])
        mock_invoke.assert_not_called()

    def test_falls_back_to_cache_on_azure_splash_404(self):
        client, tenant = self._build_client()
        upsert_jobs_to_cache(tenant, [{"name": "Cached", "enabled": True}])

        with patch(
            "apps.cron.tenant_views.invoke_gateway_tool",
            side_effect=GatewayError(
                "Gateway returned 404: <title>Azure Container App - Unavailable</title>",
                status_code=404,
            ),
        ):
            resp = client.get("/api/v1/cron-jobs/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([j["name"] for j in resp.json()["jobs"]], ["Cached"])

    def test_falls_back_to_cache_on_502(self):
        client, tenant = self._build_client()
        upsert_jobs_to_cache(tenant, [{"name": "Cached"}])
        with patch(
            "apps.cron.tenant_views.invoke_gateway_tool",
            side_effect=GatewayError("bad gateway", status_code=502),
        ):
            resp = client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["jobs"]), 1)

    def test_filters_hidden_system_crons_in_cache_response(self):
        client, tenant = self._build_client(hibernated=True)
        upsert_jobs_to_cache(
            tenant,
            [
                {"name": "Background Tasks"},  # HIDDEN_SYSTEM_CRONS
                {"name": "_sync:Foo"},  # hidden prefix
                {"name": "User Job"},
            ],
        )
        resp = client.get("/api/v1/cron-jobs/")
        self.assertEqual([j["name"] for j in resp.json()["jobs"]], ["User Job"])

    def test_real_gateway_error_still_502(self):
        client, _ = self._build_client()
        with patch(
            "apps.cron.tenant_views.invoke_gateway_tool",
            side_effect=GatewayError("validation failed", status_code=400),
        ):
            resp = client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 502)

    def test_successful_read_populates_cache(self):
        client, tenant = self._build_client()
        with patch(
            "apps.cron.tenant_views.invoke_gateway_tool",
            return_value={"jobs": [{"name": "Fresh", "id": "f1", "enabled": True}]},
        ):
            resp = client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(CronJob.objects.filter(tenant=tenant, name="Fresh").count(), 1)
