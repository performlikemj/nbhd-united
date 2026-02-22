"""Tests for tenants app."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from unittest.mock import patch

from .models import Tenant, User
from .serializers import TenantSerializer
from .services import create_tenant


class TenantModelTest(TestCase):
    def test_create_tenant(self):
        tenant = create_tenant(
            display_name="Test User",
            telegram_chat_id=123456789,
        )
        self.assertEqual(tenant.status, Tenant.Status.PENDING)
        self.assertEqual(tenant.user.telegram_chat_id, 123456789)
        self.assertEqual(tenant.user.display_name, "Test User")
        self.assertTrue(tenant.key_vault_prefix.startswith("tenants-"))

    def test_tenant_is_active(self):
        tenant = create_tenant(display_name="Test", telegram_chat_id=111)
        self.assertFalse(tenant.is_active)
        tenant.status = Tenant.Status.ACTIVE
        tenant.save()
        self.assertTrue(tenant.is_active)

    def test_tenant_budget(self):
        tenant = create_tenant(display_name="Test", telegram_chat_id=222)
        self.assertFalse(tenant.is_over_budget)
        tenant.tokens_this_month = tenant.monthly_token_budget
        tenant.save()
        self.assertTrue(tenant.is_over_budget)

    def test_unique_chat_id(self):
        create_tenant(display_name="User1", telegram_chat_id=333)
        with self.assertRaises(Exception):
            create_tenant(display_name="User2", telegram_chat_id=333)


class AuthLoginTest(TestCase):
    def setUp(self):
        self.email = "login@example.com"
        self.password = "testpass123"
        User.objects.create_user(
            username=self.email, email=self.email, password=self.password,
        )

    def test_login_with_email_returns_tokens(self):
        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": self.email, "password": self.password},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)

    def test_login_with_wrong_password_returns_401(self):
        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": self.email, "password": "wrongpass"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class AuthLogoutTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="logout@example.com",
            email="logout@example.com",
            password="testpass123",
        )
        refresh = RefreshToken.for_user(self.user)
        self.refresh = str(refresh)
        self.access = str(refresh.access_token)
        self.auth_header = f"Bearer {self.access}"

    def test_logout_blacklists_refresh_token(self):
        response = self.client.post(
            "/api/v1/auth/logout/",
            {"refresh": self.refresh},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )
        self.assertEqual(response.status_code, 204)

        refresh_response = self.client.post(
            "/api/v1/auth/refresh/",
            {"refresh": self.refresh},
            content_type="application/json",
        )
        self.assertEqual(refresh_response.status_code, 401)

    def test_logout_requires_refresh_token(self):
        response = self.client.post(
            "/api/v1/auth/logout/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )
        self.assertEqual(response.status_code, 400)

    def test_logout_rejects_invalid_refresh_token(self):
        response = self.client.post(
            "/api/v1/auth/logout/",
            {"refresh": "not-a-token"},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )
        self.assertEqual(response.status_code, 400)


class AuthSignupTest(TestCase):
    @override_settings(PREVIEW_ACCESS_KEY="test-invite-code")
    def test_signup_with_valid_invite_code(self):
        response = self.client.post(
            "/api/v1/auth/signup/",
            {"email": "new@example.com", "password": "securepass123", "invite_code": "test-invite-code"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)

    @override_settings(PREVIEW_ACCESS_KEY="test-invite-code")
    def test_signup_with_invalid_invite_code(self):
        response = self.client.post(
            "/api/v1/auth/signup/",
            {"email": "new@example.com", "password": "securepass123", "invite_code": "wrong-code"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(PREVIEW_ACCESS_KEY="test-invite-code")
    def test_signup_without_invite_code(self):
        response = self.client.post(
            "/api/v1/auth/signup/",
            {"email": "new@example.com", "password": "securepass123"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(PREVIEW_ACCESS_KEY="")
    def test_signup_open_when_no_key_configured(self):
        response = self.client.post(
            "/api/v1/auth/signup/",
            {"email": "open@example.com", "password": "securepass123"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)


class TenantSerializerTest(TestCase):
    def test_active_with_subscription_returns_true(self):
        tenant = create_tenant(display_name="Sub", telegram_chat_id=500)
        tenant.stripe_subscription_id = "sub_x"
        tenant.status = Tenant.Status.ACTIVE
        tenant.save()
        data = TenantSerializer(tenant).data
        self.assertTrue(data["has_active_subscription"])

    def test_trial_with_valid_end_date_returns_true(self):
        tenant = create_tenant(display_name="Trial", telegram_chat_id=530)
        now = timezone.now()
        tenant.is_trial = True
        tenant.trial_started_at = now - timedelta(hours=1)
        tenant.trial_ends_at = now + timedelta(days=5, hours=1)
        tenant.save(update_fields=["is_trial", "trial_started_at", "trial_ends_at", "updated_at"])

        data = TenantSerializer(tenant).data
        self.assertTrue(data["has_active_subscription"])
        self.assertEqual(data["trial_days_remaining"], 5)

    def test_trial_expired_without_subscription_returns_false(self):
        tenant = create_tenant(display_name="Trial Expired", telegram_chat_id=531)
        tenant.is_trial = True
        tenant.trial_started_at = timezone.now() - timedelta(days=10)
        tenant.trial_ends_at = timezone.now() - timedelta(days=1)
        tenant.save(update_fields=["is_trial", "trial_started_at", "trial_ends_at", "updated_at"])

        data = TenantSerializer(tenant).data
        self.assertFalse(data["has_active_subscription"])
        self.assertEqual(data["trial_days_remaining"], 0)

    def test_deleted_with_subscription_returns_false(self):
        tenant = create_tenant(display_name="Del", telegram_chat_id=501)
        tenant.stripe_subscription_id = "sub_x"
        tenant.status = Tenant.Status.DELETED
        tenant.save()
        data = TenantSerializer(tenant).data
        self.assertFalse(data["has_active_subscription"])

    def test_active_without_subscription_returns_false(self):
        tenant = create_tenant(display_name="NoSub", telegram_chat_id=502)
        tenant.stripe_subscription_id = ""
        tenant.status = Tenant.Status.ACTIVE
        tenant.save()
        data = TenantSerializer(tenant).data
        self.assertFalse(data["has_active_subscription"])


class OnboardTenantViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="onboard_trial",
            email="onboard_trial@example.com",
            password="testpass123",
        )
        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    @patch("apps.tenants.views.publish_task")
    @patch("apps.tenants.views.seed_default_templates_for_tenant")
    def test_onboard_creates_trial_and_triggers_provisioning(self, mock_seed, mock_publish):
        response = self.client.post(
            "/api/v1/tenants/onboard/",
            {
                "display_name": "Trial User",
                "timezone": "UTC",
                "agent_persona": "neighbor",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.user.refresh_from_db()
        tenant = self.user.tenant

        self.assertTrue(tenant.is_trial)
        self.assertIsNotNone(tenant.trial_started_at)
        self.assertIsNotNone(tenant.trial_ends_at)
        self.assertEqual(tenant.model_tier, Tenant.ModelTier.STARTER)
        self.assertEqual(tenant.status, Tenant.Status.PROVISIONING)
        self.assertLessEqual(
            tenant.trial_ends_at - tenant.trial_started_at,
            timedelta(days=7),
        )
        mock_publish.assert_called_once_with("provision_tenant", str(tenant.id))
        mock_seed.assert_called_once_with(tenant=tenant)

    @patch("apps.tenants.views.publish_task", side_effect=RuntimeError("qstash down"))
    @patch("apps.tenants.views.seed_default_templates_for_tenant")
    def test_onboard_returns_503_and_marks_pending_when_publish_fails(self, mock_seed, _mock_publish):
        response = self.client.post(
            "/api/v1/tenants/onboard/",
            {
                "display_name": "Trial User",
                "timezone": "UTC",
                "agent_persona": "neighbor",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("provisioning could not be started", response.data["detail"].lower())

        self.user.refresh_from_db()
        tenant = self.user.tenant
        self.assertEqual(tenant.status, Tenant.Status.PENDING)
        self.assertEqual(response.data["tenant_status"], Tenant.Status.PENDING)
        mock_seed.assert_called_once_with(tenant=tenant)

    def test_onboard_existing_pending_tenant_returns_provisioning_message(self):
        Tenant.objects.create(user=self.user, status=Tenant.Status.PROVISIONING)

        response = self.client.post(
            "/api/v1/tenants/onboard/",
            {
                "display_name": "Trial User",
                "timezone": "UTC",
                "agent_persona": "neighbor",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["tenant_status"], Tenant.Status.PROVISIONING)
        self.assertIn("provisioning", response.data["detail"].lower())


class ProvisioningStatusViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        response = self.client.get("/api/v1/tenants/provisioning-status/")
        self.assertEqual(response.status_code, 401)

    def test_returns_readiness_fields(self):
        tenant = create_tenant(display_name="Provisioning Status", telegram_chat_id=605)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = "oc-ready"
        tenant.container_fqdn = "oc-ready.internal.azurecontainerapps.io"
        tenant.provisioned_at = timezone.now()
        tenant.save(update_fields=["status", "container_id", "container_fqdn", "provisioned_at", "updated_at"])

        self.client.force_authenticate(user=tenant.user)
        response = self.client.get("/api/v1/tenants/provisioning-status/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Tenant.Status.ACTIVE)
        self.assertEqual(response.data["container_id"], "oc-ready")
        self.assertTrue(response.data["has_container_id"])
        self.assertTrue(response.data["has_container_fqdn"])
        self.assertTrue(response.data["ready"])


class RetryProvisioningViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("apps.tenants.views.publish_task")
    def test_retry_provisioning_queues_task(self, mock_publish):
        tenant = create_tenant(display_name="Retry User", telegram_chat_id=606)
        tenant.status = Tenant.Status.PENDING
        tenant.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(user=tenant.user)

        response = self.client.post("/api/v1/tenants/retry-provisioning/")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["tenant_status"], Tenant.Status.PROVISIONING)
        self.assertFalse(response.data["ready"])
        mock_publish.assert_called_once_with("provision_tenant", str(tenant.id))

    @patch("apps.tenants.views.publish_task")
    def test_retry_provisioning_returns_ready_for_active_tenant(self, mock_publish):
        tenant = create_tenant(display_name="Ready User", telegram_chat_id=607)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = "oc-ready"
        tenant.container_fqdn = "ready.internal"
        tenant.save(update_fields=["status", "container_id", "container_fqdn", "updated_at"])
        self.client.force_authenticate(user=tenant.user)

        response = self.client.post("/api/v1/tenants/retry-provisioning/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["ready"])
        mock_publish.assert_not_called()

    @patch("apps.tenants.views.publish_task", side_effect=RuntimeError("qstash down"))
    def test_retry_provisioning_publish_fail_sets_pending(self, _mock_publish):
        tenant = create_tenant(display_name="Retry Fail", telegram_chat_id=608)
        tenant.status = Tenant.Status.PENDING
        tenant.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(user=tenant.user)

        response = self.client.post("/api/v1/tenants/retry-provisioning/")

        self.assertEqual(response.status_code, 503)
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.PENDING)


class RefreshConfigViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _create_user_with_tenant(self, display_name: str, chat_id: int) -> Tenant:
        tenant = create_tenant(display_name=display_name, telegram_chat_id=chat_id)
        return tenant

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_refresh_config_success(self, mock_update):
        tenant = self._create_user_with_tenant("Refresh User", 600)
        tenant.status = Tenant.Status.ACTIVE
        tenant.pending_config_version = 2
        tenant.save(update_fields=["status", "pending_config_version"])
        self.client.force_authenticate(user=tenant.user)

        response = self.client.post("/api/v1/tenants/refresh-config/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["detail"],
            "Configuration refreshed. Your assistant will restart momentarily.",
        )
        self.assertIn("last_refreshed", response.data)
        mock_update.assert_called_once_with(str(tenant.id))

        tenant.refresh_from_db()
        self.assertIsNotNone(tenant.config_refreshed_at)
        self.assertEqual(tenant.config_version, tenant.pending_config_version)

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_refresh_config_cooldown(self, mock_update):
        tenant = self._create_user_with_tenant("Cooldown User", 601)
        tenant.status = Tenant.Status.ACTIVE
        tenant.config_refreshed_at = timezone.now() - timedelta(minutes=1)
        tenant.save(update_fields=["status", "config_refreshed_at"])
        self.client.force_authenticate(user=tenant.user)

        response = self.client.post("/api/v1/tenants/refresh-config/")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["detail"], "Please wait before refreshing again.")
        self.assertEqual(response.data["cooldown_seconds"], 300)
        mock_update.assert_not_called()

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_refresh_config_pending_tenant_returns_provisioning_message(self, mock_update):
        tenant = self._create_user_with_tenant("Pending User", 602)
        tenant.status = Tenant.Status.PENDING
        tenant.save(update_fields=["status"])
        self.client.force_authenticate(user=tenant.user)

        response = self.client.post("/api/v1/tenants/refresh-config/")

        self.assertEqual(response.status_code, 409)
        self.assertIn("provisioning", response.data["detail"].lower())
        self.assertEqual(response.data["tenant_status"], Tenant.Status.PENDING)
        mock_update.assert_not_called()

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_refresh_config_no_tenant(self, mock_update):
        user = User.objects.create_user(
            username="notenant@example.com",
            email="notenant@example.com",
            password="pass1234",
        )
        self.client.force_authenticate(user=user)

        response = self.client.post("/api/v1/tenants/refresh-config/")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["detail"], "No tenant found.")
        mock_update.assert_not_called()

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_refresh_config_get_status(self, mock_update):
        tenant = self._create_user_with_tenant("Status User", 603)
        tenant.status = Tenant.Status.ACTIVE
        tenant.save(update_fields=["status"])
        tenant.config_refreshed_at = timezone.now() - timedelta(minutes=10)
        tenant.save(update_fields=["config_refreshed_at"])
        self.client.force_authenticate(user=tenant.user)

        response = self.client.get("/api/v1/tenants/refresh-config/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cooldown_seconds"], 300)
        self.assertEqual(response.data["status"], tenant.status)
        self.assertTrue(response.data["can_refresh"])
        self.assertFalse(response.data["has_pending_update"])
        mock_update.assert_not_called()

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_refresh_config_get_status_with_pending_update(self, mock_update):
        tenant = self._create_user_with_tenant("Pending Status User", 604)
        tenant.status = Tenant.Status.ACTIVE
        tenant.pending_config_version = 3
        tenant.config_version = 1
        tenant.config_refreshed_at = timezone.now() - timedelta(minutes=10)
        tenant.save(update_fields=["status", "pending_config_version", "config_version", "config_refreshed_at"])
        self.client.force_authenticate(user=tenant.user)

        response = self.client.get("/api/v1/tenants/refresh-config/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_pending_update"])
        mock_update.assert_not_called()
