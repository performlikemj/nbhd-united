"""Adversarial audit cluster A24 regression tests (FA-0913 follow-up).

Covers the two apology helpers in pending_queue.py that previously echoed
raw PII placeholders (e.g. ``[EMAIL_ADDRESS_1]``) back to the user in the
apology text, because ``rehydrate_for_tenant`` was applied everywhere EXCEPT
these rare error paths:

  - ``_send_apology_for_dropped_pending_message`` (3-attempt cap)
  - ``_send_apology_for_stale_pending_message`` (too-old queue entry)

The at-rest ``PendingMessage.user_text`` is intentionally stored REDACTED
(correct for no-PII-to-LLM goal). The fix rehydrates the excerpt at send
time so the user sees their own words, not a placeholder, in the apology.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.router.pending_queue import PendingMessage
from apps.tenants.models import Tenant, User


def _make_tenant_with_pii_map() -> Tenant:
    user = User.objects.create_user(
        username=f"a24_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        telegram_chat_id=int(secrets.token_hex(3), 16),
        preferred_channel="telegram",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-a24.example.com",
        pii_entity_map={"[EMAIL_ADDRESS_1]": "alice@acme.com"},
    )


def _make_pending_message(tenant: Tenant, user_text: str) -> PendingMessage:
    """Create a PendingMessage with the given (redacted) user_text stored at rest."""
    return PendingMessage.objects.create(
        tenant=tenant,
        channel=PendingMessage.Channel.TELEGRAM,
        channel_user_id=str(tenant.user.telegram_chat_id),
        payload={},
        user_text=user_text,
    )


@override_settings(TELEGRAM_BOT_TOKEN="test-bot-token")
class DroppedApologyRehydrationTest(TestCase):
    """FA-0913: _send_apology_for_dropped_pending_message must rehydrate
    PII placeholders before echoing the excerpt to the user."""

    @patch("apps.router.pending_queue.httpx.post")
    def test_dropped_apology_rehydrates_placeholder(self, mock_post):
        from apps.router.pending_queue import _send_apology_for_dropped_pending_message

        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        mock_post.return_value = ok

        tenant = _make_tenant_with_pii_map()
        # Stored at rest with the PII placeholder (correct — no raw PII to LLM).
        msg = _make_pending_message(tenant, "[EMAIL_ADDRESS_1] about the deal")

        _send_apology_for_dropped_pending_message(tenant, msg)

        # The user must receive their own words, not the placeholder.
        self.assertTrue(mock_post.called)
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("alice@acme.com", sent_text)
        self.assertNotIn("[EMAIL_ADDRESS_1]", sent_text)

    @patch("apps.router.pending_queue.httpx.post")
    def test_dropped_apology_no_pii_map_still_works(self, mock_post):
        """No pii_entity_map → excerpt passes through unchanged (no crash)."""
        from apps.router.pending_queue import _send_apology_for_dropped_pending_message

        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        mock_post.return_value = ok

        user = User.objects.create_user(
            username=f"a24nopii_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=int(secrets.token_hex(3), 16),
        )
        tenant_no_map = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-a24nopii.example.com",
            pii_entity_map={},
        )
        msg = _make_pending_message(tenant_no_map, "plain text with no placeholders")

        _send_apology_for_dropped_pending_message(tenant_no_map, msg)

        self.assertTrue(mock_post.called)
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("plain text with no placeholders", sent_text)

    @patch("apps.router.pending_queue.httpx.post")
    def test_at_rest_stored_text_is_still_redacted(self, _mock_post):
        """Confirms the fix doesn't change the at-rest storage — only send-time."""
        tenant = _make_tenant_with_pii_map()
        msg = _make_pending_message(tenant, "[EMAIL_ADDRESS_1] about the deal")

        # Stored text must remain redacted (no raw PII to LLM).
        self.assertIn("[EMAIL_ADDRESS_1]", msg.user_text)
        self.assertNotIn("alice@acme.com", msg.user_text)


@override_settings(TELEGRAM_BOT_TOKEN="test-bot-token")
class StaleApologyRehydrationTest(TestCase):
    """FA-0913 second path: _send_apology_for_stale_pending_message must
    also rehydrate PII placeholders before echoing the excerpt."""

    @patch("apps.router.pending_queue.httpx.post")
    def test_stale_apology_rehydrates_placeholder(self, mock_post):
        from apps.router.pending_queue import _send_apology_for_stale_pending_message

        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        mock_post.return_value = ok

        tenant = _make_tenant_with_pii_map()
        msg = _make_pending_message(tenant, "[EMAIL_ADDRESS_1] quick question")

        _send_apology_for_stale_pending_message(tenant, msg, age_seconds=900)

        self.assertTrue(mock_post.called)
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("alice@acme.com", sent_text)
        self.assertNotIn("[EMAIL_ADDRESS_1]", sent_text)

    @patch("apps.router.pending_queue.httpx.post")
    def test_stale_apology_no_pii_map_still_works(self, mock_post):
        """No pii_entity_map → stale apology passes through unchanged (no crash)."""
        from apps.router.pending_queue import _send_apology_for_stale_pending_message

        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        mock_post.return_value = ok

        user = User.objects.create_user(
            username=f"a24stale_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=int(secrets.token_hex(3), 16),
        )
        tenant_no_map = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-a24stale.example.com",
            pii_entity_map={},
        )
        msg = _make_pending_message(tenant_no_map, "just checking in")

        _send_apology_for_stale_pending_message(tenant_no_map, msg, age_seconds=300)

        self.assertTrue(mock_post.called)
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("just checking in", sent_text)
