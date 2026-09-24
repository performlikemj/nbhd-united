"""Owner context, meditation-local cooldown, and prompt/content boundaries."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core import compose, services
from apps.core.models import MeditationSession, MeditationStatus
from apps.core.test_utils import ComposeSchemaCacheMixin
from apps.core.tests import _valid_manifest
from apps.crypto.nolog import RedactedStr
from apps.journal.models import Document
from apps.lessons.agent_context import recent_active_stars
from apps.lessons.models import Lesson
from apps.pii.testsupport import neural_ran
from apps.router import enc_columns
from apps.router.models import AppChatMessage, ChatThread, ConversationTurn
from apps.tenants.services import create_tenant


class UserWordsTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Owner", telegram_chat_id=906001)
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])
        self.now = datetime(2026, 9, 24, 3, tzinfo=UTC)
        self.tenant.recall_capture_enabled = True
        self.tenant.recall_capture_birthday = self.now - timedelta(days=10)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.tenant.user, is_main=True)

    def app(self, text, *, at=None, **kwargs):
        row = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.tenant.user,
            thread=self.thread,
            client_msg_id=str(uuid4()),
            user_text=text,
            reply_text="ASSISTANT must never reach meditation",
            **kwargs,
        )
        AppChatMessage.objects.filter(pk=row.pk).update(created_at=at or self.now)
        return row

    def turn(self, text, *, at=None):
        row = ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="906001",
            local_date=self.now.date(),
            user_text=text,
            reply_text="ASSISTANT relay response must never reach meditation",
        )
        ConversationTurn.objects.filter(pk=row.pk).update(created_at=at or self.now)
        return row

    def words(self, **kwargs):
        with patch.object(services.timezone, "now", return_value=self.now):
            return services._recent_user_words(self.tenant, **kwargs)

    def test_only_user_text_newest_first_and_filters_noise(self):
        self.app("I finally finished the garden today.")
        self.turn("I enjoyed lunch with my sister.", at=self.now - timedelta(hours=1))
        for text in ("ok", "thanks", "thank you!!!!", "/remind me to eat lunch", "[PERSON_12345]", "[Photo attached]"):
            self.app(text)
        self.app("A private on-device conversation.", source=AppChatMessage.Source.ON_DEVICE)
        words = self.words()
        self.assertEqual(len(words), 2)
        self.assertIn("garden", words[0])
        self.assertIn("sister", words[1])
        self.assertNotIn("ASSISTANT", str(words))
        other = create_tenant(display_name="Other", telegram_chat_id=906002)
        ConversationTurn.objects.create(
            tenant=other, channel="line", local_date=self.now.date(), user_text="Foreign tenant private words"
        )
        self.assertEqual(self.words(), words)

    def test_recall_requires_opt_in_and_birthday_before_reading(self):
        self.app("I went swimming in the sea today.")
        for enabled, birthday in ((False, self.now), (True, None)):
            self.tenant.recall_capture_enabled = enabled
            self.tenant.recall_capture_birthday = birthday
            with patch("apps.router.enc_read.read_values_bulk") as read:
                self.assertEqual(self.words(), [])
            read.assert_not_called()
        self.tenant.recall_capture_enabled = True
        self.tenant.recall_capture_birthday = self.now - timedelta(minutes=30)
        self.turn("Before consent, keep this private.", at=self.now - timedelta(hours=1))
        self.assertEqual(len(self.words()), 1)

    def test_three_local_days_include_boundary_and_exclude_future(self):
        # Sept 22 midnight Tokyo is Sept 21 15:00 UTC, not UTC midnight or 72h ago.
        boundary = datetime(2026, 9, 21, 15, tzinfo=UTC)
        self.app("Inside at the local day boundary.", at=boundary)
        self.turn("Outside before the local boundary.", at=boundary - timedelta(microseconds=1))
        self.app("Future words should be excluded.", at=self.now + timedelta(seconds=1))
        self.assertEqual(self.words(), ["2026-09-22: Inside at the local day boundary."])

    def test_encrypted_app_read_uses_system_principal_and_sidecar(self):
        self.tenant.read_encrypted_chat = True
        self.app("Legacy text must not be used.", user_text_enc=b"sealed")
        with patch(
            "apps.crypto.box.decrypt_bulk", return_value=[RedactedStr("Decrypted user words about the garden.")]
        ) as read:
            words = self.words()
        read.assert_called_once_with(
            self.tenant.id, *enc_columns.APP_CHAT_MESSAGE_USER_TEXT, [b"sealed"], principal="system"
        )
        self.assertIn("Decrypted user words", words[0])
        self.assertNotIn("Legacy", str(words))

    def test_quick_logs_require_exact_owner_and_stop_at_any_heading(self):
        self.tenant.recall_capture_enabled = False
        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug="2026-09-24",
            markdown=(
                "# Daily\nMorning Report: 170 open tasks tracked\n"
                "### 09:00 — Owner\nI loved seeing the ocean today.\n"
                "### Overnight maintenance\nAzure/Sentry alerts\n"
                "### 10:00 — Assistant\nI think you should adapt your plan.\n"
                "### 11:00 — Owner\nI baked bread with my family.\n"
                "## Assistant summary\nLet go of the plan.\n"
                "### 11:30 — Someone Else\nNot this owner's words.\n"
            ),
        )
        words = self.words()
        self.assertEqual(
            words, ["2026-09-24: I baked bread with my family.", "2026-09-24: I loved seeing the ocean today."]
        )
        with patch.object(services.timezone, "now", return_value=self.now):
            signals = services.gather_meditation_signals(self.tenant)
        self.assertNotIn("recent_notes", signals)
        self.assertEqual(signals["recent_user_words"], words)

    def test_caps_count_text_and_scan(self):
        for i in range(12):
            self.app(f"Message {i}: " + "a" * 240, at=self.now - timedelta(minutes=i))
        words = self.words(limit=3, cap=40)
        self.assertEqual(len(words), 3)
        self.assertTrue(all(len(word.split(": ", 1)[1]) <= 40 for word in words))
        self.assertIn("Message 0", words[0])


class StarCooldownTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Stars", telegram_chat_id=906003)
        Lesson.objects.filter(tenant=self.tenant).delete()

    def star(self):
        return Lesson.objects.create(
            tenant=self.tenant, text="A saved lesson", status="approved", galaxy_note="Remember this"
        )

    def sit(self, ids, *, days=0, status=MeditationStatus.READY):
        return MeditationSession.objects.create(
            tenant=self.tenant,
            date=timezone.now().date() - timedelta(days=days),
            status=status,
            lesson={"context_star_ids": ids},
        )

    def test_sole_pinned_star_cools_to_none_without_changing_other_callers(self):
        star = self.star()
        self.assertEqual(services._meditation_stars(self.tenant)[0]["id"], star.id)
        self.sit([star.id])
        self.assertEqual(services._meditation_stars(self.tenant), [])
        self.assertEqual(recent_active_stars(self.tenant), [star])

    def test_rotation_filters_before_limit_and_only_last_three_playable_sits(self):
        stars = [self.star() for _ in range(4)]
        for index, status in enumerate((MeditationStatus.READY, MeditationStatus.DELIVERED, MeditationStatus.DONE)):
            self.sit([stars[3 - index].id], days=index, status=status)
        self.sit([stars[0].id], days=3)
        self.sit([stars[0].id], status=MeditationStatus.FAILED)
        self.sit([stars[0].id], status=MeditationStatus.PENDING)
        self.assertEqual([s["id"] for s in services._meditation_stars(self.tenant)], [stars[0].id])

    def test_old_pinned_note_is_not_revisiting(self):
        star = self.star()
        Lesson.objects.filter(pk=star.pk).update(created_at=timezone.now() - timedelta(days=90))
        context = services._meditation_stars(self.tenant)[0]
        self.assertFalse(context["recent_activity"])
        text = compose._format_signals({"constellation_stars": [context]})
        self.assertIn("a saved insight", text)
        self.assertNotIn("revisiting", text)
        self.assertIn("MAY gently shape one moment", text)

    def test_service_persists_supplied_id_outside_llm_schema_and_preserves_prose_scrub(self):
        star = self.star()
        session = self.sit([], status=MeditationStatus.PENDING)
        manifest = _valid_manifest()
        manifest["lesson"]["core_teaching"] = "Listen to Alice."
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["pii_entity_map", "layer1_placeholder_writes"])
        session.tenant = self.tenant
        with (
            patch.object(compose, "author_manifest", return_value=manifest),
            patch("apps.cron.publish.publish_task"),
            patch("apps.pii.authoring._detect_pii", side_effect=neural_ran([])),
        ):
            services.compose_meditation(session)
        session.refresh_from_db()
        self.assertEqual(session.lesson["context_star_ids"], [star.id])
        self.assertEqual(session.lesson["core_teaching"], "Listen to [PERSON_1].")
        self.assertNotIn("context_star_ids", compose.MeditationLesson.model_json_schema()["properties"])
        session.status = MeditationStatus.READY
        session.save(update_fields=["status"])
        self.assertEqual(services._meditation_stars(self.tenant), [])


@override_settings(OPENROUTER_API_KEY="test-key")
class PromptBoundaryTests(ComposeSchemaCacheMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.tenant = create_tenant(display_name="Prompt", telegram_chat_id=906004)
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Alice", "relationship": "friend"},
            "[PERSON_855]": {"name": "gently"},
            "[PERSON_856]": {"name": "shape"},
            "[PERSON_857]": {"name": "Return"},
            "[PERSON_858]": {"name": "Choose"},
        }
        self.tenant.save(update_fields=["pii_entity_map"])

    def reply(self, manifest):
        return ({"choices": [{"message": {"content": json.dumps(manifest)}}]}, "test/model")

    @patch("apps.core.compose.chat_completion")
    def test_content_masked_but_prompt_and_retry_templates_survive(self, completion):
        invalid = _valid_manifest()
        invalid["title"] = "Alice gently shape"
        invalid["phases"][0]["segments"][0]["type"] = "Alice"
        completion.side_effect = [self.reply(invalid), self.reply(_valid_manifest())]
        signals = {
            "recent_user_words": ["Alice and I planted a garden."],
            "active_goals": ["Visit Alice"],
            "additional_context": "Alice",
            "constellation_stars": [
                {
                    "text": "Alice",
                    "galaxy_note": "Alice",
                    "recent_activity": True,
                    "tutoring_insights": [{"restated_accurately": False}],
                }
            ],
            "recent_meditations": [{"title": "Alice", "theme": "Alice", "lesson": {"core_teaching": "Alice"}}],
        }
        compose.author_manifest(signals, tenant=self.tenant, model="test/model")
        messages = completion.call_args_list[0].args[1]
        prompt = messages[1]["content"]
        self.assertIn("MAY gently shape one moment", prompt)
        self.assertIn("something still taking shape for them", prompt)
        self.assertIn("hold these gently", prompt)
        self.assertIn("In their own words recently", prompt)
        self.assertIn("[PERSON_1] and I planted", prompt)
        self.assertNotIn("Alice", prompt)
        self.assertIn("Entity legend", prompt)
        retry = completion.call_args_list[1].args[1][-2:]
        self.assertNotIn("Alice", str(retry))
        self.assertIn("[PERSON_855]", retry[0]["content"])
        self.assertIn("Return the corrected JSON object only.", retry[1]["content"])

    @patch("apps.core.compose.chat_completion")
    def test_variety_correction_masks_echoes_only(self, completion):
        first = _valid_manifest()
        first["lesson"]["teaching_slug"] = "alice"
        second = _valid_manifest()
        second["lesson"].update(tradition="zen", intention="joy")
        completion.side_effect = [self.reply(first), self.reply(second)]
        compose.author_manifest(
            {"recent_meditations": [{"title": "Previous", "lesson": {"teaching_slug": "alice"}}]},
            tenant=self.tenant,
            model="test/model",
        )
        correction = completion.call_args_list[1].args[1][-1]["content"]
        self.assertIn("Choose a different teaching", correction)
        self.assertIn("[PERSON_1]", correction)
        self.assertNotIn("alice", correction.lower())
