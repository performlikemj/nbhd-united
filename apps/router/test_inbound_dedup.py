"""Tests for the inbound-event idempotency gate.

Covers the helper contract and the wiring at all three channel entry
points (LINE webhook, Telegram webhook, Telegram poller) per the
project rule that message-routing changes must cover every channel.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.router.inbound_dedup import claim_inbound_event
from apps.router.models import ProcessedInboundEvent


class ClaimInboundEventTest(TestCase):
    def test_first_claim_processes_duplicate_skips(self):
        self.assertTrue(claim_inbound_event("line:evt-1"))
        self.assertFalse(claim_inbound_event("line:evt-1"))
        self.assertEqual(ProcessedInboundEvent.objects.filter(event_key="line:evt-1").count(), 1)

    def test_distinct_keys_both_process(self):
        self.assertTrue(claim_inbound_event("tg:1"))
        self.assertTrue(claim_inbound_event("tg:2"))

    def test_blank_key_fails_open(self):
        # No stable id → never drop a real message.
        self.assertTrue(claim_inbound_event(None))
        self.assertTrue(claim_inbound_event(""))
        self.assertEqual(ProcessedInboundEvent.objects.count(), 0)

    def test_store_error_fails_open(self):
        with patch(
            "apps.router.models.ProcessedInboundEvent.objects.get_or_create",
            side_effect=RuntimeError("db down"),
        ):
            # Degrades to a possible duplicate, never a lost message.
            self.assertTrue(claim_inbound_event("tg:99"))

    def test_prune_removes_only_expired_rows(self):
        fresh = ProcessedInboundEvent.objects.create(event_key="line:fresh")
        old = ProcessedInboundEvent.objects.create(event_key="line:old")
        ProcessedInboundEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=4))
        # Force the probabilistic prune to run deterministically.
        with patch("apps.router.inbound_dedup.random.random", return_value=0.0):
            self.assertTrue(claim_inbound_event("line:trigger-prune"))
        self.assertTrue(ProcessedInboundEvent.objects.filter(pk=fresh.pk).exists())
        self.assertFalse(ProcessedInboundEvent.objects.filter(pk=old.pk).exists())


class LineWebhookDedupTest(TestCase):
    """A redelivered LINE event (same webhookEventId) is handled once."""

    def test_redelivery_handled_once(self):
        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()
        event = {
            "type": "message",
            "webhookEventId": "01HXAMPLE",
            "deliveryContext": {"isRedelivery": True},
            "message": {"type": "text", "text": "hi"},
            "source": {"userId": "U_dedup"},
        }
        with patch.object(view, "_handle_message") as mock_handle:
            view._handle_event(event)  # first sighting
            view._handle_event(event)  # LINE redelivery
        mock_handle.assert_called_once()

    def test_distinct_events_both_handled(self):
        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()
        base = {
            "type": "message",
            "message": {"type": "text", "text": "hi"},
            "source": {"userId": "U_dedup2"},
        }
        with patch.object(view, "_handle_message") as mock_handle:
            view._handle_event({**base, "webhookEventId": "A"})
            view._handle_event({**base, "webhookEventId": "B"})
        self.assertEqual(mock_handle.call_count, 2)


@override_settings(TELEGRAM_WEBHOOK_SECRET="test-secret")
class TelegramWebhookDedupTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, update: dict):
        from apps.router.views import telegram_webhook

        return telegram_webhook(
            self.factory.post(
                "/api/v1/telegram/webhook/",
                data=json.dumps(update),
                content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-secret",
            )
        )

    def test_duplicate_update_id_short_circuits(self):
        update = {"update_id": 555, "message": {"text": "hi", "chat": {"id": 1}}}
        with patch("apps.router.views.handle_start_command", return_value=None) as mock_start:
            self._post(update)
            resp = self._post(update)  # Telegram retry of the same update
        self.assertEqual(resp.status_code, 200)
        # handle_start_command is the first downstream side effect — it
        # must run for the first delivery only.
        mock_start.assert_called_once()


class PollerDedupTest(TestCase):
    def test_restart_replay_processed_once(self):
        from apps.router.poller import TelegramPoller

        poller = TelegramPoller()
        update = {"update_id": 4242, "message": {"text": "hi", "chat": {"id": 7}}}
        with patch.object(poller, "_handle_update") as mock_handle:
            poller._process_update(update)  # original delivery
            poller._process_update(update)  # poller restart re-fetches it
        mock_handle.assert_called_once()
