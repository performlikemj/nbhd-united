"""Tests for user timezone support."""
from django.test import TestCase, RequestFactory, override_settings
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .middleware import UserTimezoneMiddleware
from .models import User, Tenant
from .serializers import UserSerializer, TenantRegistrationSerializer
from .services import create_tenant


class UserTimezoneFieldTest(TestCase):
    """Test the timezone field on User model."""

    def test_default_timezone_is_utc(self):
        user = User.objects.create_user(username="tz_test", email="tz@test.com")
        self.assertEqual(user.timezone, "UTC")

    def test_timezone_can_be_set(self):
        user = User.objects.create_user(
            username="tz_set", email="tz_set@test.com", timezone="America/New_York"
        )
        user.refresh_from_db()
        self.assertEqual(user.timezone, "America/New_York")

    def test_create_tenant_preserves_timezone(self):
        """Tenants created via services inherit default UTC."""
        tenant = create_tenant(display_name="TZ User", telegram_chat_id=999000)
        self.assertEqual(tenant.user.timezone, "UTC")


class UserSerializerTimezoneTest(TestCase):
    """Test timezone validation in serializers."""

    def test_valid_timezone_accepted(self):
        user = User.objects.create_user(username="ser_tz", email="ser@tz.com")
        serializer = UserSerializer(user, data={"timezone": "Asia/Tokyo"}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_invalid_timezone_rejected(self):
        user = User.objects.create_user(username="ser_bad", email="bad@tz.com")
        serializer = UserSerializer(user, data={"timezone": "Mars/Olympus"}, partial=True)
        self.assertFalse(serializer.is_valid())
        self.assertIn("timezone", serializer.errors)

    def test_timezone_in_serialized_output(self):
        user = User.objects.create_user(
            username="ser_out", email="out@tz.com", timezone="Europe/London"
        )
        data = UserSerializer(user).data
        self.assertEqual(data["timezone"], "Europe/London")


class TenantRegistrationTimezoneTest(TestCase):
    """Test timezone in onboarding serializer."""

    def test_default_timezone_is_utc(self):
        serializer = TenantRegistrationSerializer(data={})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["timezone"], "UTC")

    def test_valid_timezone(self):
        serializer = TenantRegistrationSerializer(
            data={"timezone": "America/Chicago"}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_invalid_timezone(self):
        serializer = TenantRegistrationSerializer(
            data={"timezone": "Fake/Zone"}
        )
        self.assertFalse(serializer.is_valid())


class UserTimezoneMiddlewareTest(TestCase):
    """Test timezone activation middleware."""

    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = UserTimezoneMiddleware(get_response=lambda r: None)

    def test_activates_user_timezone(self):
        user = User.objects.create_user(
            username="mw_tz", email="mw@tz.com", timezone="US/Eastern"
        )
        request = self.factory.get("/")
        request.user = user
        self.middleware.process_request(request)
        current = dj_timezone.get_current_timezone_name()
        self.assertIn(current, ("US/Eastern", "EST", "EDT", "America/New_York"))
        self.middleware.process_response(request, None)

    def test_anonymous_deactivates(self):
        from django.contrib.auth.models import AnonymousUser
        request = self.factory.get("/")
        request.user = AnonymousUser()
        self.middleware.process_request(request)
        # Should be UTC (deactivated = default)
        current = dj_timezone.get_current_timezone_name()
        self.assertEqual(current, "UTC")

    def test_invalid_timezone_deactivates(self):
        user = User.objects.create_user(
            username="mw_bad", email="mw_bad@tz.com", timezone="Invalid/Zone"
        )
        request = self.factory.get("/")
        request.user = user
        self.middleware.process_request(request)
        current = dj_timezone.get_current_timezone_name()
        self.assertEqual(current, "UTC")


class ProfileAPITimezoneTest(TestCase):
    """Test profile API endpoint with timezone."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="api_tz", email="api@tz.com", password="testpass123"
        )
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_get_profile_includes_timezone(self):
        resp = self.client.get("/api/v1/tenants/profile/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["timezone"], "UTC")

    def test_patch_timezone(self):
        resp = self.client.patch(
            "/api/v1/tenants/profile/",
            {"timezone": "Asia/Tokyo"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["timezone"], "Asia/Tokyo")
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Asia/Tokyo")

    def test_patch_invalid_timezone_rejected(self):
        resp = self.client.patch(
            "/api/v1/tenants/profile/",
            {"timezone": "Nowhere/Place"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


class OnboardTimezoneTest(TestCase):
    """Test onboarding with timezone."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="onboard_tz", email="onboard@tz.com", password="testpass123"
        )
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_onboard_with_timezone(self):
        resp = self.client.post(
            "/api/v1/tenants/onboard/",
            {"display_name": "TZ User", "timezone": "Europe/Berlin"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Europe/Berlin")

    def test_onboard_default_timezone(self):
        resp = self.client.post(
            "/api/v1/tenants/onboard/",
            {"display_name": "Default TZ"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "UTC")
