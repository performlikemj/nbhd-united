"""Tests for the heartbeat window feature."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from apps.tenants.serializers import HeartbeatConfigSerializer
from apps.tenants.services import create_tenant
from apps.orchestrator.config_generator import (
    _build_heartbeat_cron,
    _heartbeat_cron_expr,
    build_cron_seed_jobs,
    HEARTBEAT_MODEL,
)


class HeartbeatCronExprTest(TestCase):
    """Test cron expression generation for heartbeat windows."""

    def test_normal_window(self):
        expr = _heartbeat_cron_expr(8, 6)
        self.assertEqual(expr, "0 8,9,10,11,12,13 * * *")

    def test_single_hour(self):
        expr = _heartbeat_cron_expr(14, 1)
        self.assertEqual(expr, "0 14 * * *")

    def test_midnight_wrap(self):
        expr = _heartbeat_cron_expr(22, 6)
        # Hours: 22, 23, 0, 1, 2, 3 → sorted: 0, 1, 2, 3, 22, 23
        self.assertEqual(expr, "0 0,1,2,3,22,23 * * *")

    def test_start_at_midnight(self):
        expr = _heartbeat_cron_expr(0, 4)
        self.assertEqual(expr, "0 0,1,2,3 * * *")

    def test_end_at_midnight(self):
        expr = _heartbeat_cron_expr(20, 4)
        self.assertEqual(expr, "0 20,21,22,23 * * *")

    def test_wrap_single_past_midnight(self):
        expr = _heartbeat_cron_expr(23, 3)
        # Hours: 23, 0, 1 → sorted: 0, 1, 23
        self.assertEqual(expr, "0 0,1,23 * * *")

    def test_max_window(self):
        expr = _heartbeat_cron_expr(6, 6)
        self.assertEqual(expr, "0 6,7,8,9,10,11 * * *")


class HeartbeatCronBuildTest(TestCase):
    """Test _build_heartbeat_cron returns correct job dicts."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="HB Test",
            telegram_chat_id=111222333,
        )

    def test_returns_none_when_disabled(self):
        self.tenant.heartbeat_enabled = False
        result = _build_heartbeat_cron(self.tenant)
        self.assertIsNone(result)

    def test_returns_job_when_enabled(self):
        self.tenant.heartbeat_enabled = True
        self.tenant.heartbeat_start_hour = 9
        self.tenant.heartbeat_window_hours = 4
        result = _build_heartbeat_cron(self.tenant)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Heartbeat Check-in")
        self.assertEqual(result["model"], HEARTBEAT_MODEL)
        self.assertEqual(result["schedule"]["expr"], "0 9,10,11,12 * * *")
        self.assertEqual(result["schedule"]["tz"], "UTC")  # default tz

    def test_uses_user_timezone(self):
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])
        result = _build_heartbeat_cron(self.tenant)
        self.assertEqual(result["schedule"]["tz"], "Asia/Tokyo")

    def test_model_is_always_openrouter_minimax(self):
        for tier in ["starter", "premium", "byok"]:
            self.tenant.model_tier = tier
            result = _build_heartbeat_cron(self.tenant)
            self.assertEqual(result["model"], "openrouter/minimax/minimax-m2.7")

    def test_included_in_cron_seed_jobs(self):
        self.tenant.heartbeat_enabled = True
        jobs = build_cron_seed_jobs(self.tenant)
        names = [j["name"] for j in jobs]
        self.assertIn("Heartbeat Check-in", names)

    def test_excluded_from_cron_seed_jobs_when_disabled(self):
        self.tenant.heartbeat_enabled = False
        jobs = build_cron_seed_jobs(self.tenant)
        names = [j["name"] for j in jobs]
        self.assertNotIn("Heartbeat Check-in", names)


class HeartbeatModelValidationTest(TestCase):
    """Test model-level validation of heartbeat fields."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Validation Test",
            telegram_chat_id=444555666,
        )

    def test_default_values(self):
        self.assertTrue(self.tenant.heartbeat_enabled)
        self.assertEqual(self.tenant.heartbeat_start_hour, 8)
        self.assertEqual(self.tenant.heartbeat_window_hours, 6)

    def test_clean_rejects_window_over_6(self):
        self.tenant.heartbeat_window_hours = 7
        with self.assertRaises(ValidationError) as ctx:
            self.tenant.full_clean()
        self.assertIn("heartbeat_window_hours", ctx.exception.message_dict)

    def test_clean_accepts_window_at_6(self):
        self.tenant.heartbeat_window_hours = 6
        # Should not raise
        self.tenant.full_clean()

    def test_clean_accepts_window_at_1(self):
        self.tenant.heartbeat_window_hours = 1
        self.tenant.full_clean()

    def test_valid_start_hours(self):
        for hour in [0, 12, 23]:
            self.tenant.heartbeat_start_hour = hour
            self.tenant.full_clean()


class HeartbeatSerializerTest(TestCase):
    """Test HeartbeatConfigSerializer validation."""

    def test_valid_data(self):
        s = HeartbeatConfigSerializer(data={
            "enabled": True,
            "start_hour": 7,
            "window_hours": 5,
        })
        self.assertTrue(s.is_valid())

    def test_window_hours_max_6(self):
        s = HeartbeatConfigSerializer(data={"window_hours": 7})
        self.assertFalse(s.is_valid())
        self.assertIn("window_hours", s.errors)

    def test_window_hours_min_1(self):
        s = HeartbeatConfigSerializer(data={"window_hours": 0})
        self.assertFalse(s.is_valid())
        self.assertIn("window_hours", s.errors)

    def test_start_hour_max_23(self):
        s = HeartbeatConfigSerializer(data={"start_hour": 24})
        self.assertFalse(s.is_valid())
        self.assertIn("start_hour", s.errors)

    def test_start_hour_min_0(self):
        s = HeartbeatConfigSerializer(data={"start_hour": -1})
        self.assertFalse(s.is_valid())
        self.assertIn("start_hour", s.errors)

    def test_partial_update(self):
        """Only some fields provided — should be valid."""
        s = HeartbeatConfigSerializer(data={"enabled": False})
        self.assertTrue(s.is_valid())

    def test_empty_data(self):
        """Empty payload is valid (no-op)."""
        s = HeartbeatConfigSerializer(data={})
        self.assertTrue(s.is_valid())


class HeartbeatAPITest(TestCase):
    """Test the heartbeat config API endpoint."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="API Test",
            telegram_chat_id=777888999,
        )
        self.user = self.tenant.user
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = "/api/v1/tenants/heartbeat/"

    def test_get_returns_defaults(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["enabled"])
        self.assertEqual(resp.data["start_hour"], 8)
        self.assertEqual(resp.data["window_hours"], 6)

    def test_patch_update_window_hours_ignored(self):
        """PATCH ignores window_hours (locked to 6) but still applies start_hour."""
        resp = self.client.patch(
            self.url,
            {"start_hour": 10, "window_hours": 4},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["start_hour"], 10)
        self.assertEqual(resp.data["window_hours"], 6)  # locked
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.heartbeat_start_hour, 10)
        self.assertEqual(self.tenant.heartbeat_window_hours, 6)  # unchanged

    def test_patch_disable(self):
        resp = self.client.patch(
            self.url,
            {"enabled": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["enabled"])
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.heartbeat_enabled)

    def test_patch_rejects_window_over_6(self):
        resp = self.client.patch(
            self.url,
            {"window_hours": 8},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_rejects_invalid_start_hour(self):
        resp = self.client.patch(
            self.url,
            {"start_hour": 25},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 401)

    def test_no_tenant_returns_404(self):
        from apps.tenants.models import User
        user_no_tenant = User.objects.create_user(
            username="no_tenant_user",
            display_name="No Tenant",
        )
        client = APIClient()
        client.force_authenticate(user=user_no_tenant)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 404)
