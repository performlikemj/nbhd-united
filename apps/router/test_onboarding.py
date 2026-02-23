"""Tests for the Telegram onboarding flow."""
from unittest.mock import patch

from django.test import TestCase

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.router.onboarding import (
    get_onboarding_response,
    needs_reintroduction,
    parse_language,
    parse_name,
    parse_timezone,
)


class ParseNameTest(TestCase):
    def test_simple_name(self):
        self.assertEqual(parse_name("Alex"), "Alex")

    def test_strips_prefix_my_name_is(self):
        self.assertEqual(parse_name("my name is Alex"), "Alex")

    def test_strips_prefix_call_me(self):
        self.assertEqual(parse_name("call me Alex"), "Alex")

    def test_strips_prefix_im(self):
        self.assertEqual(parse_name("I'm Sarah"), "Sarah")

    def test_strips_punctuation(self):
        self.assertEqual(parse_name("Alex!"), "Alex")

    def test_title_cases_lowercase(self):
        self.assertEqual(parse_name("alex"), "Alex")

    def test_preserves_existing_case(self):
        self.assertEqual(parse_name("MJ"), "MJ")

    def test_empty_returns_friend(self):
        self.assertEqual(parse_name(""), "Friend")


class ParseLanguageTest(TestCase):
    def test_english(self):
        self.assertEqual(parse_language("English"), ("en", "English"))

    def test_spanish(self):
        self.assertEqual(parse_language("Spanish"), ("es", "Spanish"))

    def test_japanese(self):
        self.assertEqual(parse_language("Japanese"), ("ja", "Japanese"))

    def test_japanese_native(self):
        self.assertEqual(parse_language("日本語"), ("ja", "Japanese"))

    def test_code(self):
        self.assertEqual(parse_language("fr"), ("fr", "French"))

    def test_sentence(self):
        self.assertEqual(parse_language("I speak Spanish"), ("es", "Spanish"))

    def test_unknown_defaults_english(self):
        self.assertEqual(parse_language("Klingon"), ("en", "English"))

    def test_case_insensitive(self):
        self.assertEqual(parse_language("GERMAN"), ("de", "German"))


class ParseTimezoneTest(TestCase):
    def test_est(self):
        self.assertEqual(parse_timezone("EST"), "America/New_York")

    def test_pacific(self):
        self.assertEqual(parse_timezone("Pacific"), "America/Los_Angeles")

    def test_jst(self):
        self.assertEqual(parse_timezone("JST"), "Asia/Tokyo")

    def test_city_tokyo(self):
        self.assertEqual(parse_timezone("Tokyo"), "Asia/Tokyo")

    def test_city_new_york(self):
        self.assertEqual(parse_timezone("New York"), "America/New_York")

    def test_utc_plus(self):
        self.assertEqual(parse_timezone("UTC+9"), "Etc/GMT-9")

    def test_utc_minus(self):
        self.assertEqual(parse_timezone("UTC-5"), "Etc/GMT+5")

    def test_gmt_offset(self):
        self.assertEqual(parse_timezone("GMT+2"), "Etc/GMT-2")

    def test_unknown_returns_utc(self):
        self.assertEqual(parse_timezone("xyzzy"), "UTC")

    def test_sentence_with_timezone(self):
        self.assertEqual(parse_timezone("I'm in Tokyo"), "Asia/Tokyo")

    def test_jamaica(self):
        self.assertEqual(parse_timezone("Jamaica"), "America/Jamaica")


class OnboardingFlowTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="New User", telegram_chat_id=999999
        )
        self.tenant.onboarding_complete = False
        self.tenant.onboarding_step = 0
        self.tenant.save(update_fields=["onboarding_complete", "onboarding_step"])

    def test_step_0_sends_welcome(self):
        """First message triggers welcome + name question."""
        reply = get_onboarding_response(self.tenant, "hello")
        self.assertIn("call you", reply.text.lower())
        self.assertIn("Welcome", reply.text)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 1)

    def test_step_0_sends_japanese_welcome_if_telegram_lang_ja(self):
        """Japanese user gets welcome in Japanese."""
        reply = get_onboarding_response(self.tenant, "hello", telegram_lang="ja")
        self.assertIn("ようこそ", reply.text)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 1)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.language, "ja")

    def test_step_1_captures_name_asks_language_for_english(self):
        """English user: name captured, then asks language preference."""
        self.tenant.onboarding_step = 1
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "Alex")
        self.assertIn("Alex", reply.text)
        self.assertIn("language", reply.text.lower())
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 2)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.display_name, "Alex")

    def test_step_1_skips_language_if_auto_detected(self):
        """Non-English Telegram user: language auto-detected, skips to timezone."""
        self.tenant.onboarding_step = 1
        self.tenant.user.language = "ja"
        self.tenant.user.save(update_fields=["language"])
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "太郎", telegram_lang="ja")
        self.assertIn("太郎", reply.text)
        self.assertIn("国", reply.text)  # Asks country in Japanese
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 3)  # Skipped step 2

    def test_step_2_captures_language_asks_timezone(self):
        """Language captured, then asks timezone."""
        self.tenant.onboarding_step = 2
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "Japanese")
        self.assertIn("Japanese", reply.text)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 3)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.language, "ja")

    def test_step_3_single_tz_country(self):
        """Single-timezone country resolves directly."""
        self.tenant.onboarding_step = 3
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "Japan")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 4)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.timezone, "Asia/Tokyo")

    def test_step_3_multi_tz_country_shows_zones(self):
        """Multi-timezone country shows zone buttons."""
        self.tenant.onboarding_step = 3
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "United States")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 35)  # Sub-step
        self.assertIsNotNone(reply.keyboard)  # Has zone buttons

    def test_step_3_text_fallback(self):
        """Unknown country falls back to timezone parser."""
        self.tenant.onboarding_step = 3
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "PST")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 4)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.timezone, "America/Los_Angeles")

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_step_4_completes_onboarding(self, mock_upload):
        """Interests captured, onboarding complete, USER.md written."""
        self.tenant.onboarding_step = 4
        self.tenant.user.display_name = "Alex"
        self.tenant.user.timezone = "America/Los_Angeles"
        self.tenant.user.language = "en"
        self.tenant.user.save(update_fields=["display_name", "timezone", "language"])
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "Help me with work stuff")
        self.assertIn("ready to go", reply.text)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.onboarding_complete)
        self.assertEqual(self.tenant.onboarding_step, 5)

        mock_upload.assert_called_once()
        content = mock_upload.call_args[0][2]
        self.assertIn("Alex", content)
        self.assertIn("Los_Angeles", content)

        self.tenant.user.refresh_from_db()
        self.assertEqual(
            self.tenant.user.preferences["onboarding_interests"],
            "Help me with work stuff",
        )

    def test_completed_returns_none(self):
        """After onboarding, returns None for agent handoff."""
        self.tenant.onboarding_complete = True
        self.tenant.onboarding_step = 5
        self.tenant.save(update_fields=["onboarding_complete", "onboarding_step"])

        reply = get_onboarding_response(self.tenant, "Hello agent!")
        self.assertIsNone(reply)

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_full_flow_english(self, mock_upload):
        """Walk through full onboarding flow for English user."""
        # Message 1: triggers welcome
        r1 = get_onboarding_response(self.tenant, "hi")
        self.assertIn("call you", r1.text.lower())

        # Message 2: name → asks language (English, no auto-detect)
        self.tenant.refresh_from_db()
        r2 = get_onboarding_response(self.tenant, "I'm Jordan")
        self.assertIn("Jordan", r2.text)
        self.assertIn("language", r2.text.lower())

        # Message 3: language → shows country buttons
        self.tenant.refresh_from_db()
        r3 = get_onboarding_response(self.tenant, "Spanish")
        self.assertIn("Spanish", r3.text)
        self.assertIsNotNone(r3.keyboard)  # Country buttons

        # Message 4: country → asks interests
        self.tenant.refresh_from_db()
        r4 = get_onboarding_response(self.tenant, "Jamaica")

        # Message 5: interests → complete
        self.tenant.refresh_from_db()
        r5 = get_onboarding_response(self.tenant, "productivity and fitness tracking")

        # Verify final state
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.onboarding_complete)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.display_name, "Jordan")
        self.assertEqual(self.tenant.user.language, "es")
        self.assertEqual(self.tenant.user.timezone, "America/Jamaica")

        # Next message should return None
        r6 = get_onboarding_response(self.tenant, "Hello agent!")
        self.assertIsNone(r6)

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_full_flow_japanese_auto_detect(self, mock_upload):
        """Japanese user: language auto-detected, skips language question."""
        # Message 1: welcome in Japanese
        r1 = get_onboarding_response(self.tenant, "こんにちは", telegram_lang="ja")
        self.assertIn("ようこそ", r1.text)

        # Message 2: name → skips language, asks timezone in Japanese
        self.tenant.refresh_from_db()
        r2 = get_onboarding_response(self.tenant, "太郎", telegram_lang="ja")
        self.assertIn("太郎", r2.text)
        self.assertIn("国", r2.text)  # Asks country in Japanese
        self.assertIsNotNone(r2.keyboard)  # Has country buttons
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 3)  # Skipped step 2

        # Message 3: country → resolves timezone, asks interests
        self.tenant.refresh_from_db()
        r3 = get_onboarding_response(self.tenant, "Japan")
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.timezone, "Asia/Tokyo")

        # Message 4: interests → complete in Japanese
        self.tenant.refresh_from_db()
        r4 = get_onboarding_response(self.tenant, "仕事の整理を手伝ってほしい")
        self.assertIn("準備完了", r4.text)

        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.onboarding_complete)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.language, "ja")


class CallbackTest(TestCase):
    """Tests for inline button callback handling."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Test", telegram_chat_id=777777
        )
        self.tenant.onboarding_complete = False
        self.tenant.onboarding_step = 3  # Waiting for country
        self.tenant.save(update_fields=["onboarding_complete", "onboarding_step"])

    def test_single_tz_country_callback(self):
        """Tapping a single-tz country resolves timezone immediately."""
        from apps.router.onboarding import handle_onboarding_callback
        reply = handle_onboarding_callback(self.tenant, "tz_country:Japan")
        self.assertIsNotNone(reply)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.timezone, "Asia/Tokyo")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 4)

    def test_multi_tz_country_callback(self):
        """Tapping a multi-tz country shows zone buttons."""
        from apps.router.onboarding import handle_onboarding_callback
        reply = handle_onboarding_callback(self.tenant, "tz_country:United States")
        self.assertIsNotNone(reply)
        self.assertIsNotNone(reply.keyboard)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 35)

    def test_zone_callback(self):
        """Tapping a zone button sets timezone."""
        self.tenant.onboarding_step = 35
        self.tenant.save(update_fields=["onboarding_step"])

        from apps.router.onboarding import handle_onboarding_callback
        reply = handle_onboarding_callback(self.tenant, "tz_zone:America/New_York")
        self.assertIsNotNone(reply)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.timezone, "America/New_York")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 4)

    def test_other_country_callback(self):
        """Tapping 'Other' asks user to type country name."""
        from apps.router.onboarding import handle_onboarding_callback
        reply = handle_onboarding_callback(self.tenant, "tz_country:OTHER")
        self.assertIsNotNone(reply)
        self.assertIn("type", reply.text.lower())


class ReintroductionTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Friend", telegram_chat_id=888888
        )
        # Simulate a backfilled user
        self.tenant.onboarding_complete = True
        self.tenant.onboarding_step = 4
        self.tenant.save(update_fields=["onboarding_complete", "onboarding_step"])

    def test_needs_reintro_for_default_profile(self):
        """Backfilled user with all defaults needs re-intro."""
        self.assertTrue(needs_reintroduction(self.tenant))

    def test_no_reintro_for_filled_profile(self):
        """User with real data doesn't need re-intro."""
        self.tenant.user.display_name = "Alex"
        self.tenant.user.timezone = "America/New_York"
        self.tenant.user.preferences = {"onboarding_interests": "coding"}
        self.tenant.user.save(update_fields=["display_name", "timezone", "preferences"])
        self.assertFalse(needs_reintroduction(self.tenant))

    def test_no_reintro_during_onboarding(self):
        """User currently in onboarding flow shouldn't be flagged."""
        self.tenant.onboarding_complete = False
        self.tenant.onboarding_step = 2
        self.tenant.save(update_fields=["onboarding_complete", "onboarding_step"])
        self.assertFalse(needs_reintroduction(self.tenant))

    def test_reintro_uses_different_message(self):
        """Re-intro sends 'I never properly introduced myself' instead of welcome."""
        self.tenant.onboarding_step = 0
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "hello")
        self.assertIn("never properly introduced", reply.text)
        self.assertNotIn("Welcome", reply.text)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.onboarding_complete)  # Reset for flow
        self.assertEqual(self.tenant.onboarding_step, 1)
