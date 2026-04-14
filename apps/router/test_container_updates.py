"""Tests for smart container update logic."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.router.container_updates import (
    build_update_prompt,
    check_and_maybe_update,
    handle_update_callback,
    is_container_outdated,
    is_idle_enough_for_silent_update,
)
from apps.tenants.models import Tenant


def create_tenant(**kwargs):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    import uuid

    uid = uuid.uuid4().hex[:8]
    user = User.objects.create_user(
        username=f"test-{uid}",
        email=f"test-{uid}@example.com",
        password="testpass",
    )
    defaults = {
        "user": user,
        "status": Tenant.Status.ACTIVE,
        "container_id": f"oc-test-{uid}",
        "container_fqdn": f"oc-test-{uid}.internal",
        "container_image_tag": "abc123",
    }
    defaults.update(kwargs)
    return Tenant.objects.create(**defaults)


class IsOutdatedTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(container_image_tag="abc123")

    @override_settings(OPENCLAW_IMAGE_TAG="def456")
    def test_outdated_when_tags_differ(self):
        self.assertTrue(is_container_outdated(self.tenant))

    @override_settings(OPENCLAW_IMAGE_TAG="abc123")
    def test_not_outdated_when_tags_match(self):
        self.assertFalse(is_container_outdated(self.tenant))

    @override_settings(OPENCLAW_IMAGE_TAG="latest")
    def test_not_outdated_when_latest(self):
        """Can't compare — should return False."""
        self.assertFalse(is_container_outdated(self.tenant))

    @override_settings(OPENCLAW_IMAGE_TAG="def456")
    def test_outdated_when_no_current_tag(self):
        self.tenant.container_image_tag = ""
        self.assertTrue(is_container_outdated(self.tenant))


class IdleCheckTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant()

    def test_idle_when_no_last_message(self):
        self.tenant.last_message_at = None
        self.assertTrue(is_idle_enough_for_silent_update(self.tenant))

    def test_idle_when_old_message(self):
        self.tenant.last_message_at = timezone.now() - timedelta(hours=3)
        self.assertTrue(is_idle_enough_for_silent_update(self.tenant))

    def test_not_idle_when_recent_message(self):
        self.tenant.last_message_at = timezone.now() - timedelta(minutes=30)
        self.assertFalse(is_idle_enough_for_silent_update(self.tenant))


class CheckAndMaybeUpdateTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(container_image_tag="abc123")

    @override_settings(OPENCLAW_IMAGE_TAG="abc123")
    def test_no_update_needed(self):
        result = check_and_maybe_update(self.tenant)
        self.assertIsNone(result)

    @override_settings(OPENCLAW_IMAGE_TAG="def456")
    @patch("apps.router.container_updates.update_container", return_value=True)
    def test_silent_update_when_idle(self, mock_update):
        self.tenant.last_message_at = timezone.now() - timedelta(hours=3)
        result = check_and_maybe_update(self.tenant)
        self.assertEqual(result["action"], "silent_update")
        mock_update.assert_called_once_with(self.tenant)

    @override_settings(OPENCLAW_IMAGE_TAG="def456")
    def test_ask_user_when_recently_active(self):
        self.tenant.last_message_at = timezone.now() - timedelta(minutes=10)
        result = check_and_maybe_update(self.tenant)
        self.assertEqual(result["action"], "ask_user")
        self.assertIn("reply_markup", result)
        self.assertIn("update", result["text"].lower())


class HandleCallbackTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(container_image_tag="abc123")

    @override_settings(OPENCLAW_IMAGE_TAG="def456")
    @patch("apps.router.container_updates.update_container", return_value=True)
    def test_yes_updates_container(self, mock_update):
        reply = handle_update_callback(self.tenant, "container_update:yes")
        self.assertIn("Updating", reply)
        mock_update.assert_called_once()

    def test_no_returns_later_message(self):
        reply = handle_update_callback(self.tenant, "container_update:no")
        self.assertIn("later", reply.lower())


class BuildPromptTest(TestCase):
    def test_english(self):
        prompt = build_update_prompt("en")
        self.assertIn("update", prompt["text"].lower())
        self.assertIn("inline_keyboard", prompt["reply_markup"])

    def test_japanese(self):
        prompt = build_update_prompt("ja")
        self.assertIn("アップデート", prompt["text"])

    def test_fallback_to_english(self):
        prompt = build_update_prompt("ko")
        self.assertIn("update", prompt["text"].lower())
