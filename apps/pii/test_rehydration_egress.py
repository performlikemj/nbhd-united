"""Egress rehydration contract for user-facing send paths.

Every path that delivers agent-authored text to a user must rehydrate
``[TYPE_N]`` PII placeholders first, otherwise the user sees a raw
``[PERSON_1]`` token instead of the real value. These tests guard the
three gap classes fixed alongside ``rehydrate_for_tenant``:

* gate confirmations (``apps.actions.messaging``)
* lesson notifications + approve/dismiss echoes (``apps.lessons.notifications``)
* the nightly extraction summary (``apps.journal.extraction``)

All tests use ``SimpleNamespace`` fakes + mocked transport so they need
no database rows or network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.pii.redactor import rehydrate_for_tenant

ENTITY_MAP = {"[PERSON_1]": "Sarah", "[EMAIL_ADDRESS_1]": "sarah@example.com"}


def _tenant(channel: str = "telegram") -> SimpleNamespace:
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        pii_entity_map=dict(ENTITY_MAP),
        user=SimpleNamespace(
            telegram_chat_id=4242,
            line_user_id="Uline",
            preferred_channel=channel,
        ),
    )


class _FakeResp:
    status_code = 200
    text = "ok"

    @staticmethod
    def json() -> dict:
        return {"result": {"message_id": "100"}}


class RehydrateForTenantTests(SimpleTestCase):
    def test_none_tenant_returns_text(self):
        self.assertEqual(rehydrate_for_tenant(None, "hi [PERSON_1]"), "hi [PERSON_1]")

    def test_empty_map_returns_text(self):
        t = SimpleNamespace(pii_entity_map={})
        self.assertEqual(rehydrate_for_tenant(t, "hi [PERSON_1]"), "hi [PERSON_1]")

    def test_missing_map_attr_returns_text(self):
        self.assertEqual(rehydrate_for_tenant(SimpleNamespace(), "hi [PERSON_1]"), "hi [PERSON_1]")

    def test_rehydrates_known_placeholder(self):
        t = SimpleNamespace(pii_entity_map=dict(ENTITY_MAP))
        self.assertEqual(rehydrate_for_tenant(t, "hi [PERSON_1]"), "hi Sarah")

    def test_empty_text_is_noop(self):
        t = SimpleNamespace(pii_entity_map=dict(ENTITY_MAP))
        self.assertEqual(rehydrate_for_tenant(t, ""), "")


@override_settings(TELEGRAM_BOT_TOKEN="testtoken", LINE_CHANNEL_ACCESS_TOKEN="linetoken")
class GateConfirmationRehydrationTests(SimpleTestCase):
    def test_telegram_gate_confirmation_rehydrates(self):
        from apps.actions import messaging

        action = SimpleNamespace(id=7, display_summary="Wire money to [PERSON_1]")
        with patch("httpx.post", return_value=_FakeResp()) as post:
            messaging._send_telegram_confirmation(_tenant(), action)
        body = post.call_args.kwargs["json"]["text"]
        self.assertIn("Sarah", body)
        self.assertNotIn("PERSON_1", body)

    def test_line_gate_confirmation_rehydrates(self):
        from apps.actions import messaging

        action = SimpleNamespace(id=8, display_summary="Wire money to [PERSON_1]")
        with patch("httpx.post", return_value=_FakeResp()) as post:
            messaging._send_line_confirmation(_tenant("line"), action)
        blob = str(post.call_args.kwargs["json"])
        self.assertIn("Sarah", blob)
        self.assertNotIn("PERSON_1", blob)


@override_settings(TELEGRAM_BOT_TOKEN="testtoken", LINE_CHANNEL_ACCESS_TOKEN="linetoken")
class LessonNotificationRehydrationTests(SimpleTestCase):
    def test_telegram_lesson_rehydrates(self):
        from apps.lessons import notifications

        lesson = SimpleNamespace(id=3, text="Remember to thank [PERSON_1]")
        with patch("httpx.post", return_value=_FakeResp()) as post:
            ok = notifications._send_telegram_lesson(_tenant(), lesson)
        self.assertTrue(ok)
        body = post.call_args.kwargs["json"]["text"]
        self.assertIn("Sarah", body)
        self.assertNotIn("PERSON_1", body)

    def test_line_lesson_rehydrates(self):
        from apps.lessons import notifications

        lesson = SimpleNamespace(id=4, text="Remember to thank [PERSON_1]")
        with patch("httpx.post", return_value=_FakeResp()) as post:
            ok = notifications._send_line_lesson(_tenant("line"), lesson)
        self.assertTrue(ok)
        blob = str(post.call_args.kwargs["json"])
        self.assertIn("Sarah", blob)
        self.assertNotIn("PERSON_1", blob)


class ExtractionSummaryRehydrationTests(SimpleTestCase):
    def test_format_task_action_line_rehydrates_title(self):
        from apps.journal import extraction

        action = SimpleNamespace(
            kind="task_complete",
            task_id=1,
            task=SimpleNamespace(title="Call [PERSON_1]"),
            goal_id=None,
        )
        line = extraction._format_task_action_line(action, ENTITY_MAP)
        self.assertIn("Sarah", line)
        self.assertNotIn("PERSON_1", line)

    def test_telegram_summary_rehydrates_items_and_titles(self):
        from apps.journal import extraction

        item = SimpleNamespace(kind="lesson", text="Had coffee with [PERSON_1]", id=11)
        action = SimpleNamespace(
            kind="task_complete",
            task_id=1,
            task=SimpleNamespace(title="Email [PERSON_1]"),
            goal_id=None,
            id=22,
        )
        # Return None so the post-send bulk_update (DB) is skipped.
        with patch.object(extraction, "_send_telegram_with_buttons", return_value=None) as send:
            extraction._deliver_summary_telegram("tok", 123, [item], task_actions=[action], entity_map=ENTITY_MAP)
        text = send.call_args.args[2]
        self.assertIn("Sarah", text)
        self.assertNotIn("PERSON_1", text)

    def test_line_summary_rehydrates(self):
        from apps.journal import extraction

        item = SimpleNamespace(kind="lesson", text="Lunch with [PERSON_1]", id=11)
        with patch("apps.journal.extraction.requests.post", return_value=_FakeResp()) as post:
            ok = extraction._deliver_summary_line("tok", "Uline", [item], entity_map=ENTITY_MAP)
        self.assertTrue(ok)
        blob = str(post.call_args.kwargs["json"])
        self.assertIn("Sarah", blob)
        self.assertNotIn("PERSON_1", blob)

    def test_no_entity_map_is_noop(self):
        from apps.journal import extraction

        item = SimpleNamespace(kind="lesson", text="a plain note with no entities", id=11)
        with patch.object(extraction, "_send_telegram_with_buttons", return_value=None) as send:
            extraction._deliver_summary_telegram("tok", 123, [item], entity_map=None)
        self.assertIn("a plain note with no entities", send.call_args.args[2])
