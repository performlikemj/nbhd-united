from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.router.conversation_capture import record_conversation_turn
from apps.router.cron_delivery import _resolve_delivery_attempt
from apps.router.models import DeliveryAttempt
from apps.router.pending_queue import _clean_assistant_text_for_app
from apps.router.proactive_context import record_proactive_outbound
from apps.tenants.models import Tenant


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class PIIStorageBackstopTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="pii-storage-guard")
        self.tenant = Tenant.objects.create(user=self.user)
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Theo Smith"},
            "[ORG_1]": {"name": "Optiver"},
        }
        self.tenant.save(update_fields=["pii_entity_map"])

    @patch("apps.insights.markers.extract_and_record_insights")
    @patch("apps.router.structured_artifacts.externalize_large_structured_reply")
    def test_ios_cleaner_redacts_before_metadata(self, externalize, _insights):
        externalize.side_effect = lambda **kwargs: type(
            "Result", (), {"stored_text": kwargs["text"], "journal_link": kwargs["journal_link"]}
        )()
        stored, pushed, metadata, _quick, _link = _clean_assistant_text_for_app(
            self.tenant,
            "Theo Smith joined Optiver",
            artifact_dedup_key="turn-1",
        )
        self.assertEqual(stored, "[PERSON_1] joined [ORG_1]")
        self.assertEqual(pushed, "Theo Smith joined Optiver")
        self.assertEqual(
            metadata,
            [
                {"placeholder": "[PERSON_1]", "value": "Theo Smith"},
                {"placeholder": "[ORG_1]", "value": "Optiver"},
            ],
        )

    @patch("apps.router.conversation_capture.schedule_user_md_refresh")
    @patch("apps.router.conversation_capture._maybe_prune")
    def test_conversation_turn_reply_is_placeholder_space(self, _prune, _refresh):
        row = record_conversation_turn(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="42",
            user_text="hello",
            reply_text="Ask Theo Smith at Optiver",
        )
        row.refresh_from_db()
        self.assertEqual(row.reply_text, "Ask [PERSON_1] at [ORG_1]")

    @patch("apps.router.proactive_context._dispatch_ios_push")
    @patch("apps.router.structured_artifacts.externalize_large_structured_reply")
    def test_proactive_outbound_is_placeholder_space(self, externalize, _push):
        externalize.side_effect = lambda **kwargs: type(
            "Result", (), {"stored_text": kwargs["text"], "journal_link": kwargs["journal_link"]}
        )()
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="app",
            channel_user_id="device",
            message_text="Theo Smith at Optiver",
        )
        self.assertEqual(row.message_text, "[PERSON_1] at [ORG_1]")

    def test_cron_delivery_excerpt_is_placeholder_space(self):
        attempt = DeliveryAttempt.objects.create(
            tenant=self.tenant,
            occurrence_key="occurrence-1",
            job_name="test",
            channel="app",
        )
        _resolve_delivery_attempt(attempt, state=DeliveryAttempt.State.FAILED, excerpt="Theo Smith at Optiver")
        attempt.refresh_from_db()
        self.assertEqual(attempt.response_excerpt, "[PERSON_1] at [ORG_1]")
