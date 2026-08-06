"""Tests for handle_hibernated_message — buffering + wake watchdog.

Regression coverage for the 2026-04-28 incident: when wake_hibernated_tenant
silently partial-failed, the queue accumulated buffered messages forever
because handle_hibernated_message saw "already waking" and never re-fired
the wake. The watchdog is meant to detect that stalled state and force a
fresh wake (plus defensively clear hibernated_at so the live path takes
over even if the wake mechanism is broken)."""

from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.pii.redactor import RedactionOutcome
from apps.router.models import BufferedMessage
from apps.router.wake_on_message import (
    ACK_FRESH,
    ACK_RECONNECT,
    SILENT,
    handle_hibernated_message,
)
from apps.tenants.models import Tenant, User


def _make_user(line_user_id: str) -> User:
    return User.objects.create_user(
        username=f"wake_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        line_user_id=line_user_id,
        preferred_channel="line",
    )


def _make_hibernated_tenant(user: User, hibernated_minutes_ago: int = 15) -> Tenant:
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-test.example.com",
        hibernated_at=timezone.now() - timedelta(minutes=hibernated_minutes_ago),
    )


class HandleHibernatedMessageTest(TestCase):
    def test_returns_none_when_tenant_not_hibernated(self):
        user = _make_user(line_user_id="U_live")
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-test.example.com",
            hibernated_at=None,
        )
        result = handle_hibernated_message(tenant, "line", {"events": []}, "hello")
        self.assertIsNone(result)
        # No buffering should occur for a non-hibernated tenant.
        self.assertEqual(BufferedMessage.objects.filter(tenant=tenant).count(), 0)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    def test_first_message_buffers_and_triggers_wake(self, mock_wake):
        user = _make_user(line_user_id="U_first")
        tenant = _make_hibernated_tenant(user)

        result = handle_hibernated_message(tenant, "line", {"events": []}, "first message")

        self.assertEqual(result, ACK_FRESH)  # caller should send waking-up ack
        self.assertEqual(BufferedMessage.objects.filter(tenant=tenant, delivered=False).count(), 1)
        mock_wake.assert_called_once_with(tenant)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    def test_additional_message_during_active_wake_does_not_re_fire(self, mock_wake):
        user = _make_user(line_user_id="U_additional")
        tenant = _make_hibernated_tenant(user)

        # An existing recent buffered message — still inside the wake stall threshold.
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="earlier",
            created_at=timezone.now() - timedelta(seconds=30),
        )

        result = handle_hibernated_message(tenant, "line", {"events": []}, "second message")

        self.assertEqual(result, SILENT)  # caller should stay silent
        # Wake was NOT re-fired since the prior wake is still considered in flight.
        mock_wake.assert_not_called()
        # Both messages buffered.
        self.assertEqual(BufferedMessage.objects.filter(tenant=tenant, delivered=False).count(), 2)


class WakeWatchdogTest(TestCase):
    """The watchdog catches the stall pattern that caused the 2026-04-28
    incident: wake_hibernated_tenant fired once, silently partial-failed,
    and subsequent messages just kept buffering with no recovery."""

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    def test_old_buffered_message_triggers_fresh_wake(self, mock_wake):
        user = _make_user(line_user_id="U_stuck")
        tenant = _make_hibernated_tenant(user, hibernated_minutes_ago=30)

        # An "abandoned" buffered message older than the stall threshold.
        old_msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="abandoned",
        )
        BufferedMessage.objects.filter(id=old_msg.id).update(
            created_at=timezone.now() - timedelta(minutes=10),
        )

        result = handle_hibernated_message(tenant, "line", {"events": []}, "follow up")

        # Watchdog should re-fire wake.
        mock_wake.assert_called_once_with(tenant)
        # The prior wake silently failed so the user almost certainly never
        # saw the original "waking up" ack — send a "reconnecting" ack now.
        self.assertEqual(result, ACK_RECONNECT)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    def test_old_buffered_message_clears_hibernated_at_defensively(self, mock_wake):
        """If wake_hibernated_tenant itself silently partial-fails, future
        messages must be able to take the live path. Clearing hibernated_at
        ensures handle_hibernated_message returns None on the next call,
        even if mock_wake never actually woke anything."""
        user = _make_user(line_user_id="U_defensive_clear")
        tenant = _make_hibernated_tenant(user, hibernated_minutes_ago=30)

        old_msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="abandoned",
        )
        BufferedMessage.objects.filter(id=old_msg.id).update(
            created_at=timezone.now() - timedelta(minutes=10),
        )

        handle_hibernated_message(tenant, "line", {"events": []}, "follow up")

        tenant.refresh_from_db()
        self.assertIsNone(tenant.hibernated_at)
        mock_wake.assert_called_once()

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    def test_recent_buffered_message_does_not_trigger_watchdog(self, mock_wake):
        """A wake that's only a minute old is still in flight — don't
        force a re-wake (would create a thrash loop)."""
        user = _make_user(line_user_id="U_recent")
        tenant = _make_hibernated_tenant(user, hibernated_minutes_ago=2)

        # Very recent buffered message — well under the 5 min stall threshold.
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="just sent",
            created_at=timezone.now() - timedelta(seconds=15),
        )

        handle_hibernated_message(tenant, "line", {"events": []}, "follow-up")

        mock_wake.assert_not_called()
        tenant.refresh_from_db()
        self.assertIsNotNone(tenant.hibernated_at)  # flag preserved


class BufferWriteRedactsAndMinimizesTest(TestCase):
    """Phase 0 (docs/encryption-at-rest-directive.md §7): the row a hibernated
    message writes must hold a MINIMAL envelope (never the raw webhook) and a
    REDACTED user_text (never the raw name the user typed). We patch
    redact_user_message to identity so the assertion is model-independent — the
    reuse-only second pass still masks the pre-seeded known entity."""

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    def test_line_write_stores_minimal_envelope_and_redacted_text(self, _wake):
        user = _make_user(line_user_id="U_secret_line_id")
        tenant = _make_hibernated_tenant(user)
        tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        tenant.save(update_fields=["pii_entity_map"])

        raw_event = {
            "type": "message",
            "replyToken": "tok",
            "source": {"userId": "U_secret_line_id", "type": "user"},
            "message": {"type": "text", "id": "m1", "text": "hi Alice"},
        }
        with patch(
            "apps.pii.redactor.redact_user_message_checked",
            return_value=RedactionOutcome("hi Alice", False, "test-noop"),
        ):
            handle_hibernated_message(tenant, "line", raw_event, "hi Alice")

        row = BufferedMessage.objects.filter(tenant=tenant).latest("created_at")
        # Only the minimal envelope — no raw source/message fields.
        self.assertEqual(
            row.payload,
            {
                "schema": "min-v1",
                "channel": "line",
                "is_voice": False,
                "redaction": {"confirmed": False, "reason": "test-noop"},
            },
        )
        self.assertNotIn("U_secret_line_id", str(row.payload))
        # user_text redacted: raw name gone, placeholder present.
        self.assertNotIn("Alice", row.user_text)
        self.assertIn("[PERSON_1]", row.user_text)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    def test_telegram_write_drops_raw_pii_metadata(self, _wake):
        user = User.objects.create_user(
            username=f"wake_tg_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=44556677,
            preferred_channel="telegram",
        )
        tenant = _make_hibernated_tenant(user)

        raw_update = {
            "update_id": 5,
            "message": {
                "message_id": 9,
                "text": "call me",
                "chat": {"id": 44556677, "type": "private", "first_name": "Bob", "username": "bob_x"},
                "from": {"id": 44556677, "first_name": "Bob", "last_name": "Lee", "username": "bob_x"},
            },
        }
        with patch(
            "apps.pii.redactor.redact_user_message_checked",
            return_value=RedactionOutcome("call me", False, "test-noop"),
        ):
            handle_hibernated_message(tenant, "telegram", raw_update, "call me")

        row = BufferedMessage.objects.filter(tenant=tenant).latest("created_at")
        self.assertEqual(
            row.payload,
            {
                "schema": "min-v1",
                "channel": "telegram",
                "is_voice": False,
                "is_image": False,
                "provider_event_id": 5,
                "chat_id": 44556677,
                "redaction": {"confirmed": False, "reason": "test-noop"},
            },
        )
        for pii in ("Bob", "Lee", "bob_x"):
            self.assertNotIn(pii, str(row.payload))
