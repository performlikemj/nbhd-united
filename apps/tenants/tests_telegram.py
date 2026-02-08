"""Tests for Telegram QR code / deep link onboarding flow."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import User, Tenant
from apps.tenants.telegram_models import TelegramLinkToken
from apps.tenants import telegram_service as svc


class TelegramLinkTokenModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_is_valid_true(self):
        token = TelegramLinkToken.objects.create(
            user=self.user,
            token="abc123",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.assertTrue(token.is_valid)

    def test_is_valid_expired(self):
        token = TelegramLinkToken.objects.create(
            user=self.user,
            token="expired",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertFalse(token.is_valid)

    def test_is_valid_used(self):
        token = TelegramLinkToken.objects.create(
            user=self.user,
            token="used",
            expires_at=timezone.now() + timedelta(minutes=10),
            used=True,
        )
        self.assertFalse(token.is_valid)


class TelegramServiceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_generate_link_token(self):
        token = svc.generate_link_token(self.user)
        self.assertIsNotNone(token.token)
        self.assertFalse(token.used)
        self.assertTrue(token.is_valid)

    def test_get_deep_link(self):
        link = svc.get_deep_link("mytoken123")
        self.assertIn("mytoken123", link)
        self.assertIn("t.me/", link)

    def test_process_start_token_success(self):
        token = svc.generate_link_token(self.user)
        success, msg = svc.process_start_token(
            telegram_user_id=12345,
            telegram_chat_id=12345,
            telegram_username="testbot",
            telegram_first_name="Test",
            token=token.token,
        )
        self.assertTrue(success)

        self.user.refresh_from_db()
        self.assertEqual(self.user.telegram_user_id, 12345)
        self.assertEqual(self.user.telegram_chat_id, 12345)
        self.assertEqual(self.user.telegram_username, "testbot")

        token.refresh_from_db()
        self.assertTrue(token.used)

    def test_process_start_token_invalid(self):
        success, msg = svc.process_start_token(
            telegram_user_id=12345,
            telegram_chat_id=12345,
            telegram_username="",
            telegram_first_name="",
            token="nonexistent",
        )
        self.assertFalse(success)

    def test_process_start_token_expired(self):
        token = TelegramLinkToken.objects.create(
            user=self.user,
            token="expiredtoken",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        success, msg = svc.process_start_token(
            telegram_user_id=12345,
            telegram_chat_id=12345,
            telegram_username="",
            telegram_first_name="",
            token=token.token,
        )
        self.assertFalse(success)

    def test_process_start_token_already_linked_to_other(self):
        other_user = User.objects.create_user(
            username="other", password="pass", telegram_user_id=12345
        )
        token = svc.generate_link_token(self.user)
        success, msg = svc.process_start_token(
            telegram_user_id=12345,
            telegram_chat_id=12345,
            telegram_username="",
            telegram_first_name="",
            token=token.token,
        )
        self.assertFalse(success)
        self.assertIn("already linked", msg)

    def test_unlink_telegram(self):
        self.user.telegram_user_id = 12345
        self.user.telegram_chat_id = 12345
        self.user.save()

        success = svc.unlink_telegram(self.user)
        self.assertTrue(success)

        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_user_id)

    def test_unlink_not_linked(self):
        self.assertFalse(svc.unlink_telegram(self.user))

    def test_get_telegram_status_linked(self):
        self.user.telegram_user_id = 12345
        self.user.telegram_chat_id = 12345
        self.user.telegram_username = "myuser"
        self.user.save()

        status = svc.get_telegram_status(self.user)
        self.assertTrue(status["linked"])
        self.assertEqual(status["telegram_username"], "myuser")

    def test_get_telegram_status_not_linked(self):
        status = svc.get_telegram_status(self.user)
        self.assertFalse(status["linked"])


class TelegramViewsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.tenants.telegram_service.get_qr_code_data_url", return_value="data:image/png;base64,fake")
    def test_generate_link(self, mock_qr):
        resp = self.client.post("/api/v1/tenants/telegram/generate-link/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deep_link", resp.data)
        self.assertIn("qr_code", resp.data)
        self.assertIn("expires_at", resp.data)

    @patch("apps.tenants.telegram_service.get_qr_code_data_url", return_value="data:image/png;base64,fake")
    def test_generate_link_already_linked(self, mock_qr):
        self.user.telegram_user_id = 12345
        self.user.save()
        resp = self.client.post("/api/v1/tenants/telegram/generate-link/")
        self.assertEqual(resp.status_code, 400)

    def test_status_not_linked(self):
        resp = self.client.get("/api/v1/tenants/telegram/status/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["linked"])

    def test_status_linked(self):
        self.user.telegram_user_id = 12345
        self.user.telegram_chat_id = 12345
        self.user.telegram_username = "bot"
        self.user.save()
        resp = self.client.get("/api/v1/tenants/telegram/status/")
        self.assertTrue(resp.data["linked"])

    def test_unlink(self):
        self.user.telegram_user_id = 12345
        self.user.telegram_chat_id = 12345
        self.user.save()
        resp = self.client.post("/api/v1/tenants/telegram/unlink/")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_user_id)

    def test_unlink_not_linked(self):
        resp = self.client.post("/api/v1/tenants/telegram/unlink/")
        self.assertEqual(resp.status_code, 400)


class RouterStartCommandTest(TestCase):
    """Test that /start TOKEN in the webhook triggers linking."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_handle_start_command_links_account(self):
        from apps.router.services import handle_start_command

        token = svc.generate_link_token(self.user)
        update = {
            "message": {
                "text": f"/start {token.token}",
                "from": {"id": 99999, "username": "tguser", "first_name": "TG"},
                "chat": {"id": 99999},
            }
        }
        result = handle_start_command(update)
        self.assertIsNotNone(result)
        self.assertIn("✅", result["text"])

        self.user.refresh_from_db()
        self.assertEqual(self.user.telegram_user_id, 99999)
        self.assertEqual(self.user.telegram_chat_id, 99999)

    def test_handle_start_command_no_token(self):
        from apps.router.services import handle_start_command

        update = {"message": {"text": "/help", "from": {"id": 1}, "chat": {"id": 1}}}
        self.assertIsNone(handle_start_command(update))
