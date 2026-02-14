"""Tests for user timezone support."""
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.orchestrator.config_generator import generate_openclaw_config
from .middleware import UserTimezoneMiddleware
from .models import Tenant, User
from .serializers import TenantRegistrationSerializer, UserSerializer
from .services import create_tenant


class UserTimezoneFieldTest(TestCase):
    def test_default_timezone_is_utc(self):
        user = User.objects.create_user(username="tz_default", email="tz_default@test.com")
        self.assertEqual(user.timezone, "UTC")

    def test_timezone_can_be_set(self):
        user = User.objects.create_user(
            username="tz_set",
            email="tz_set@test.com",
            timezone="America/New_York",
        )
        user.refresh_from_db()
        self.assertEqual(user.timezone, "America/New_York")

    def test_openclaw_config_defaults_user_timezone_to_utc(self):
        tenant = create_tenant(display_name="TZ Config Default", telegram_chat_id=101010)
        config = generate_openclaw_config(tenant)
        self.assertEqual(config["agents"]["defaults"]["userTimezone"], "UTC")

    def test_openclaw_config_uses_user_timezone(self):
        tenant = create_tenant(display_name="TZ Config", telegram_chat_id=202020)
        tenant.user.timezone = "Europe/Berlin"
        tenant.user.save(update_fields=["timezone"])
        config = generate_openclaw_config(tenant)
        self.assertEqual(config["agents"]["defaults"]["userTimezone"], "Europe/Berlin")


class UserSerializerTimezoneTest(TestCase):
    def test_valid_timezone_accepted(self):
        user = User.objects.create_user(username="serializer_ok", email="serializer_ok@test.com")
        serializer = UserSerializer(user, data={"timezone": "Asia/Tokyo"}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_invalid_timezone_rejected(self):
        user = User.objects.create_user(username="serializer_bad", email="serializer_bad@test.com")
        serializer = UserSerializer(user, data={"timezone": "Mars/Olympus"}, partial=True)
        self.assertFalse(serializer.is_valid())
        self.assertIn("timezone", serializer.errors)


class TenantRegistrationTimezoneTest(TestCase):
    def test_default_timezone_is_utc(self):
        serializer = TenantRegistrationSerializer(data={})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["timezone"], "UTC")

    def test_valid_timezone_accepted(self):
        serializer = TenantRegistrationSerializer(data={"timezone": "America/Chicago"})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_invalid_timezone_rejected(self):
        serializer = TenantRegistrationSerializer(data={"timezone": "Invalid/Zone"})
        self.assertFalse(serializer.is_valid())
        self.assertIn("timezone", serializer.errors)


class UserTimezoneMiddlewareTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = UserTimezoneMiddleware(get_response=lambda r: None)

    def test_activates_user_timezone(self):
        user = User.objects.create_user(
            username="middleware_tz",
            email="middleware_tz@test.com",
            timezone="US/Eastern",
        )
        request = self.factory.get("/")
        request.user = user
        self.middleware.process_request(request)
        current = dj_timezone.get_current_timezone_name()
        self.assertIn(current, ("US/Eastern", "EST", "EDT", "America/New_York"))
        self.middleware.process_response(request, None)

    def test_invalid_timezone_falls_back_to_default(self):
        user = User.objects.create_user(
            username="middleware_bad",
            email="middleware_bad@test.com",
            timezone="Invalid/Zone",
        )
        request = self.factory.get("/")
        request.user = user
        self.middleware.process_request(request)
        self.assertEqual(dj_timezone.get_current_timezone_name(), "UTC")
        self.middleware.process_response(request, None)


class ProfileTimezoneAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="profile_tz",
            email="profile_tz@test.com",
            password="testpass123",
        )
        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_get_profile_includes_timezone(self):
        response = self.client.get("/api/v1/tenants/profile/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["timezone"], "UTC")

    def test_patch_timezone_updates_user(self):
        response = self.client.patch(
            "/api/v1/tenants/profile/",
            {"timezone": "Europe/Berlin"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Europe/Berlin")

    def test_patch_invalid_timezone_returns_400(self):
        response = self.client.patch(
            "/api/v1/tenants/profile/",
            {"timezone": "Nowhere/Place"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_timezone_change_refreshes_active_tenant_config(self, mock_update_tenant_config):
        tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE, container_id="oc-test")
        response = self.client.patch(
            "/api/v1/tenants/profile/",
            {"timezone": "Asia/Tokyo"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        mock_update_tenant_config.assert_called_once_with(str(tenant.id))

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_non_timezone_patch_does_not_refresh_tenant_config(self, mock_update_tenant_config):
        Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE, container_id="oc-test")
        response = self.client.patch(
            "/api/v1/tenants/profile/",
            {"display_name": "New Name"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        mock_update_tenant_config.assert_not_called()


class OnboardTimezoneTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="onboard_tz",
            email="onboard_tz@test.com",
            password="testpass123",
        )
        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_onboard_with_timezone_and_persona(self):
        response = self.client.post(
            "/api/v1/tenants/onboard/",
            {
                "display_name": "TZ User",
                "timezone": "Europe/London",
                "agent_persona": "neighbor",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Europe/London")
        self.assertEqual(self.user.preferences.get("agent_persona"), "neighbor")

    def test_onboard_defaults_timezone(self):
        response = self.client.post(
            "/api/v1/tenants/onboard/",
            {"display_name": "Default TZ"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "UTC")
