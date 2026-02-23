"""Tests for the Telegram onboarding flow."""
from unittest.mock import patch

from django.test import TestCase

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.router.onboarding import (
    get_onboarding_response,
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
        self.assertIn("what should I call you", reply)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 1)

    def test_step_1_captures_name(self):
        """Second message captures name, asks timezone."""
        self.tenant.onboarding_step = 1
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "Alex")
        self.assertIn("Alex", reply)
        self.assertIn("timezone", reply.lower())
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 2)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.display_name, "Alex")

    def test_step_2_captures_timezone(self):
        """Third message captures timezone, asks interests."""
        self.tenant.onboarding_step = 2
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "PST")
        self.assertIn("help with", reply.lower())
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 3)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.timezone, "America/Los_Angeles")

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_step_3_completes_onboarding(self, mock_upload):
        """Fourth message completes onboarding, writes USER.md."""
        self.tenant.onboarding_step = 3
        self.tenant.user.display_name = "Alex"
        self.tenant.user.timezone = "America/Los_Angeles"
        self.tenant.user.save(update_fields=["display_name", "timezone"])
        self.tenant.save(update_fields=["onboarding_step"])

        reply = get_onboarding_response(self.tenant, "Help me with work and personal stuff")
        self.assertIn("ready to go", reply)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.onboarding_complete)
        self.assertEqual(self.tenant.onboarding_step, 4)

        # USER.md should be written
        mock_upload.assert_called_once()
        call_args = mock_upload.call_args
        self.assertEqual(call_args[0][0], str(self.tenant.id))
        self.assertIn("USER.md", call_args[0][1])
        self.assertIn("Alex", call_args[0][2])
        self.assertIn("Los_Angeles", call_args[0][2])

        # Interests stored in preferences
        self.tenant.user.refresh_from_db()
        self.assertEqual(
            self.tenant.user.preferences["onboarding_interests"],
            "Help me with work and personal stuff",
        )

    def test_completed_returns_none(self):
        """After onboarding, returns None to signal agent handoff."""
        self.tenant.onboarding_complete = True
        self.tenant.onboarding_step = 4
        self.tenant.save(update_fields=["onboarding_complete", "onboarding_step"])

        reply = get_onboarding_response(self.tenant, "Hello agent!")
        self.assertIsNone(reply)

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_full_flow(self, mock_upload):
        """Walk through the entire onboarding flow."""
        # Message 1: triggers welcome
        r1 = get_onboarding_response(self.tenant, "hi")
        self.assertIn("call you", r1)

        # Message 2: name
        self.tenant.refresh_from_db()
        r2 = get_onboarding_response(self.tenant, "I'm Jordan")
        self.assertIn("Jordan", r2)

        # Message 3: timezone
        self.tenant.refresh_from_db()
        r3 = get_onboarding_response(self.tenant, "Eastern")
        self.assertIn("help with", r3.lower())

        # Message 4: interests → complete
        self.tenant.refresh_from_db()
        r4 = get_onboarding_response(self.tenant, "productivity and fitness tracking")
        self.assertIn("ready to go", r4)

        # Verify final state
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.onboarding_complete)
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.display_name, "Jordan")
        self.assertEqual(self.tenant.user.timezone, "America/New_York")

        # Next message should return None (forward to agent)
        r5 = get_onboarding_response(self.tenant, "Hello agent!")
        self.assertIsNone(r5)
