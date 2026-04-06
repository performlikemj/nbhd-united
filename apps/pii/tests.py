"""Tests for PII redaction and rehydration."""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.pii.config import TIER_POLICIES
from apps.pii.redactor import RedactionSession, redact_text, rehydrate_text
from apps.tenants.services import create_tenant


class RedactTextPolicyTest(TestCase):
    """Test tier-based policy routing (no Presidio engine needed)."""

    def test_empty_text_returns_unchanged(self):
        self.assertEqual(redact_text(""), "")
        self.assertEqual(redact_text("   "), "   ")

    def test_unknown_tier_falls_back_to_starter(self):
        policy = TIER_POLICIES.get("nonexistent", TIER_POLICIES["starter"])
        self.assertTrue(policy["enabled"])


class RedactTextIntegrationTest(TestCase):
    """Integration tests that run the full Presidio pipeline.

    These tests require spaCy's en_core_web_sm model to be installed.
    They are skipped in environments without it.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            import spacy
            spacy.load("en_core_web_sm")
            cls.has_spacy = True
        except (ImportError, OSError):
            cls.has_spacy = False

    def setUp(self):
        if not self.has_spacy:
            self.skipTest("spaCy en_core_web_sm not installed")

    def test_redacts_email_address(self):
        text = "Send the report to sarah.jones@example.com by Friday."
        result = redact_text(text, tier="starter")
        self.assertNotIn("sarah.jones@example.com", result)
        self.assertIn("[EMAIL_ADDRESS_1]", result)

    def test_redacts_phone_number(self):
        text = "My phone number is 555-867-5309, call anytime."
        result = redact_text(text, tier="starter")
        self.assertNotIn("555-867-5309", result)
        self.assertIn("[PHONE_NUMBER_1]", result)

    def test_redacts_credit_card(self):
        text = "Card number: 4111-1111-1111-1111"
        result = redact_text(text, tier="starter")
        self.assertNotIn("4111-1111-1111-1111", result)
        self.assertIn("[CREDIT_CARD_1]", result)

    def test_redacts_person_name(self):
        text = "I had lunch with David Thompson at the cafe."
        result = redact_text(text, tier="starter")
        self.assertNotIn("David Thompson", result)
        self.assertIn("[PERSON_", result)

    def test_allows_tenant_display_name(self):
        tenant = create_tenant(display_name="Michael Jones", telegram_chat_id=222222)
        text = "Michael mentioned that David Smith should join the meeting."
        result = redact_text(text, tenant=tenant)
        self.assertIn("Michael", result)
        self.assertNotIn("David Smith", result)

    def test_country_names_not_redacted_as_person(self):
        text = "Jordan called me from Georgia about the project."
        result = redact_text(text, tier="starter")
        self.assertIn("Jordan", result)
        self.assertIn("Georgia", result)

    def test_country_as_location_is_redacted_for_starter(self):
        text = "We traveled to France last summer."
        result = redact_text(text, tier="starter")
        self.assertNotIn("France", result)

    def test_multiple_entities_numbered(self):
        text = "Email bob@test.com and alice@test.com about the project."
        result = redact_text(text, tier="starter")
        self.assertIn("[EMAIL_ADDRESS_1]", result)
        self.assertIn("[EMAIL_ADDRESS_2]", result)

    def test_realistic_journal_entry(self):
        text = (
            "# 2026-03-26\n\n"
            "Had a productive meeting with Sarah Chen about the roadmap. "
            "She mentioned that Tom Bradley from engineering will join next week. "
            "Emailed the summary to sarah.chen@acme.com and tom@acme.com. "
            "The client's phone number is 415-555-0199.\n\n"
            "## Reflections\n"
            "Feeling good about the direction. Need to follow up with "
            "the team in Jordan about the deployment timeline."
        )
        result = redact_text(text, tier="starter")

        self.assertNotIn("sarah.chen@acme.com", result)
        self.assertNotIn("tom@acme.com", result)
        self.assertNotIn("415-555-0199", result)
        self.assertNotIn("Jordan", result)
        self.assertIn("# 2026-03-26", result)
        self.assertIn("## Reflections", result)

    def test_redaction_error_returns_original(self):
        text = "Some text with sarah@test.com"
        with patch("apps.pii.engine.get_analyzer", side_effect=RuntimeError("boom")):
            result = redact_text(text, tier="starter")
        self.assertEqual(result, text)


class RedactionSessionTest(TestCase):
    """Test RedactionSession for cross-document entity tracking."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            import spacy
            spacy.load("en_core_web_sm")
            cls.has_spacy = True
        except (ImportError, OSError):
            cls.has_spacy = False

    def setUp(self):
        if not self.has_spacy:
            self.skipTest("spaCy en_core_web_sm not installed")

    def test_cross_document_numbering(self):
        session = RedactionSession(tier="starter")
        doc1 = session.redact("Email from alice@test.com about the project.")
        doc2 = session.redact("Reply to bob@test.com with details.")

        # First doc gets _1, second doc gets _2
        self.assertIn("[EMAIL_ADDRESS_1]", doc1)
        self.assertIn("[EMAIL_ADDRESS_2]", doc2)

    def test_entity_map_populated(self):
        session = RedactionSession(tier="starter")
        session.redact("Contact alice@test.com for info.")

        self.assertIn("[EMAIL_ADDRESS_1]", session.entity_map)
        self.assertEqual(session.entity_map["[EMAIL_ADDRESS_1]"], "alice@test.com")

    def test_entity_map_spans_documents(self):
        session = RedactionSession(tier="starter")
        session.redact("Email alice@test.com about the project.")
        session.redact("Also email bob@test.com with the update.")

        self.assertEqual(session.entity_map["[EMAIL_ADDRESS_1]"], "alice@test.com")
        self.assertEqual(session.entity_map["[EMAIL_ADDRESS_2]"], "bob@test.com")



class RehydrateTextTest(TestCase):
    """Test PII rehydration in outgoing messages."""

    def test_rehydrates_single_placeholder(self):
        entity_map = {"[PERSON_1]": "Sarah Chen"}
        text = "How did your meeting with [PERSON_1] go?"
        result = rehydrate_text(text, entity_map)
        self.assertEqual(result, "How did your meeting with Sarah Chen go?")

    def test_rehydrates_multiple_placeholders(self):
        entity_map = {
            "[PERSON_1]": "Sarah Chen",
            "[EMAIL_ADDRESS_1]": "sarah@acme.com",
        }
        text = "Send the update to [PERSON_1] at [EMAIL_ADDRESS_1]."
        result = rehydrate_text(text, entity_map)
        self.assertEqual(result, "Send the update to Sarah Chen at sarah@acme.com.")

    def test_unknown_placeholder_preserved(self):
        entity_map = {"[PERSON_1]": "Sarah"}
        text = "Ask [PERSON_1] and [PERSON_2] about it."
        result = rehydrate_text(text, entity_map)
        self.assertEqual(result, "Ask Sarah and [PERSON_2] about it.")

    def test_empty_map_returns_unchanged(self):
        self.assertEqual(rehydrate_text("hello [PERSON_1]", {}), "hello [PERSON_1]")
        self.assertEqual(rehydrate_text("hello", {"[PERSON_1]": "x"}), "hello")

    def test_none_text_returns_unchanged(self):
        self.assertEqual(rehydrate_text("", {"[PERSON_1]": "x"}), "")

    def test_no_brackets_skips_regex(self):
        # Fast path: no [ in text means no work to do
        text = "Just a normal message with no placeholders."
        entity_map = {"[PERSON_1]": "Sarah"}
        self.assertEqual(rehydrate_text(text, entity_map), text)

    def test_round_trip_redact_then_rehydrate(self):
        """Redact text, then rehydrate — should recover original PII."""
        session = RedactionSession(tier="starter")
        original = "Contact alice@test.com for help."
        redacted = session.redact(original)

        self.assertNotIn("alice@test.com", redacted)
        self.assertIn("[EMAIL_ADDRESS_1]", redacted)

        # Simulate model response referencing the placeholder
        model_response = "I've noted to contact [EMAIL_ADDRESS_1] for help."
        rehydrated = rehydrate_text(model_response, session.entity_map)

        self.assertIn("alice@test.com", rehydrated)
        self.assertNotIn("[EMAIL_ADDRESS_1]", rehydrated)


class RedactUserMessageTest(TestCase):
    """Test Phase 2: user message redaction with entity map consistency."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            import spacy
            spacy.load("en_core_web_sm")
            cls.has_spacy = True
        except (ImportError, OSError):
            cls.has_spacy = False

    def setUp(self):
        if not self.has_spacy:
            self.skipTest("spaCy en_core_web_sm not installed")
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=333333)

    def test_redacts_email_in_user_message(self):
        from apps.pii.redactor import redact_user_message
        result = redact_user_message("Send it to alice@test.com", self.tenant)
        self.assertNotIn("alice@test.com", result)
        self.assertIn("[EMAIL_ADDRESS_", result)

    def test_reuses_known_entities(self):
        from apps.pii.redactor import redact_user_message
        # Pre-populate entity map (as if Phase 1 workspace sync ran)
        self.tenant.pii_entity_map = {"[EMAIL_ADDRESS_1]": "alice@test.com"}
        self.tenant.save(update_fields=["pii_entity_map"])

        result = redact_user_message("Email alice@test.com about the update.", self.tenant)
        # Should reuse the existing placeholder, not create a new one
        self.assertIn("[EMAIL_ADDRESS_1]", result)
        self.assertNotIn("[EMAIL_ADDRESS_2]", result)

    def test_new_entities_get_next_number(self):
        from apps.pii.redactor import redact_user_message
        # Pre-populate with one entity
        self.tenant.pii_entity_map = {"[EMAIL_ADDRESS_1]": "alice@test.com"}
        self.tenant.save(update_fields=["pii_entity_map"])

        result = redact_user_message("Contact bob@test.com for details.", self.tenant)
        self.assertNotIn("bob@test.com", result)
        # Should be _2 since _1 already exists
        self.assertIn("[EMAIL_ADDRESS_2]", result)

    def test_new_entities_persisted_to_db(self):
        from apps.pii.redactor import redact_user_message
        self.tenant.pii_entity_map = {}
        self.tenant.save(update_fields=["pii_entity_map"])

        redact_user_message("Contact bob@test.com for details.", self.tenant)

        # Reload from DB
        self.tenant.refresh_from_db()
        self.assertTrue(len(self.tenant.pii_entity_map) > 0)
        # Should contain the new email
        self.assertIn("bob@test.com", self.tenant.pii_entity_map.values())


    def test_empty_message_unchanged(self):
        from apps.pii.redactor import redact_user_message
        self.assertEqual(redact_user_message("", self.tenant), "")
        self.assertEqual(redact_user_message("  ", self.tenant), "  ")


class RedactTelegramUpdateTest(TestCase):
    """Test Telegram update redaction for the webhook path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            import spacy
            spacy.load("en_core_web_sm")
            cls.has_spacy = True
        except (ImportError, OSError):
            cls.has_spacy = False

    def setUp(self):
        if not self.has_spacy:
            self.skipTest("spaCy en_core_web_sm not installed")
        self.tenant = create_tenant(display_name="Webhook User", telegram_chat_id=444444)

    def test_redacts_message_text(self):
        from apps.pii.redactor import redact_telegram_update
        update = {
            "update_id": 12345,
            "message": {
                "message_id": 1,
                "text": "Email alice@test.com about the project.",
                "chat": {"id": 444444},
            },
        }
        result = redact_telegram_update(update, self.tenant)
        self.assertNotIn("alice@test.com", result["message"]["text"])

    def test_redacts_edited_message(self):
        from apps.pii.redactor import redact_telegram_update
        update = {
            "update_id": 12345,
            "edited_message": {
                "message_id": 1,
                "text": "Updated: contact bob@test.com instead.",
                "chat": {"id": 444444},
            },
        }
        result = redact_telegram_update(update, self.tenant)
        self.assertNotIn("bob@test.com", result["edited_message"]["text"])

    def test_preserves_non_text_fields(self):
        from apps.pii.redactor import redact_telegram_update
        update = {
            "update_id": 12345,
            "message": {
                "message_id": 1,
                "text": "hello",
                "chat": {"id": 444444},
                "from": {"id": 123, "first_name": "Test"},
            },
        }
        result = redact_telegram_update(update, self.tenant)
        self.assertEqual(result["message"]["chat"]["id"], 444444)
        self.assertEqual(result["message"]["from"]["first_name"], "Test")


class RedactToolResponseTest(TestCase):
    """Test Phase 3: tool response redaction for Gmail, Calendar, Reddit."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            import spacy
            spacy.load("en_core_web_sm")
            cls.has_spacy = True
        except (ImportError, OSError):
            cls.has_spacy = False

    def setUp(self):
        if not self.has_spacy:
            self.skipTest("spaCy en_core_web_sm not installed")
        self.tenant = create_tenant(display_name="Tool User", telegram_chat_id=555555)

    def test_redacts_gmail_from_field(self):
        from apps.pii.redactor import redact_tool_response
        data = {
            "messages": [
                {
                    "id": "msg123",
                    "thread_id": "thread456",
                    "snippet": "Meeting notes from today",
                    "subject": "Quarterly Review",
                    "from": "alice@example.com",
                    "date": "Mon, 25 Mar 2026 10:00:00 -0700",
                    "internal_date": "1711375200000",
                },
            ],
            "result_size_estimate": 1,
        }
        result = redact_tool_response(data, self.tenant)

        # Email in 'from' should be redacted
        self.assertNotIn("alice@example.com", result["messages"][0]["from"])
        # ID fields should be preserved
        self.assertEqual(result["messages"][0]["id"], "msg123")
        self.assertEqual(result["messages"][0]["thread_id"], "thread456")
        # Date preserved
        self.assertEqual(result["messages"][0]["date"], "Mon, 25 Mar 2026 10:00:00 -0700")

    def test_redacts_gmail_detail_body(self):
        from apps.pii.redactor import redact_tool_response
        data = {
            "id": "msg123",
            "thread_id": "thread456",
            "from": "bob@example.com",
            "to": "user@example.com",
            "subject": "Follow-up",
            "body_text": "Hi, please call me at my phone number 555-867-5309.",
            "body_truncated": False,
            "thread_context": [],
        }
        result = redact_tool_response(data, self.tenant)

        # from/to should be redacted
        self.assertNotIn("bob@example.com", result["from"])
        self.assertNotIn("user@example.com", result["to"])
        # ID preserved
        self.assertEqual(result["id"], "msg123")

    def test_redacts_calendar_summary(self):
        from apps.pii.redactor import redact_tool_response
        data = {
            "events": [
                {
                    "id": "evt123",
                    "summary": "Lunch with David Thompson",
                    "status": "confirmed",
                    "html_link": "https://calendar.google.com/event?id=evt123",
                    "start": {"dateTime": "2026-03-26T12:00:00"},
                    "end": {"dateTime": "2026-03-26T13:00:00"},
                },
            ],
        }
        result = redact_tool_response(data, self.tenant)

        # Person name in summary should be redacted
        self.assertNotIn("David Thompson", result["events"][0]["summary"])
        # ID and structural fields preserved
        self.assertEqual(result["events"][0]["id"], "evt123")
        self.assertEqual(result["events"][0]["status"], "confirmed")


    def test_handles_nested_lists(self):
        from apps.pii.redactor import redact_tool_response
        data = {
            "thread_context": [
                {"id": "t1", "from": "alice@test.com", "snippet": "test"},
                {"id": "t2", "from": "bob@test.com", "snippet": "reply"},
            ],
        }
        result = redact_tool_response(data, self.tenant)
        # IDs preserved
        self.assertEqual(result["thread_context"][0]["id"], "t1")
        # Emails redacted
        self.assertNotIn("alice@test.com", result["thread_context"][0]["from"])

    def test_reuses_known_entities_from_map(self):
        from apps.pii.redactor import redact_tool_response
        self.tenant.pii_entity_map = {"[EMAIL_ADDRESS_1]": "alice@test.com"}
        self.tenant.save(update_fields=["pii_entity_map"])

        data = {"from": "alice@test.com", "subject": "Hello"}
        result = redact_tool_response(data, self.tenant)

        # Should reuse the known placeholder
        self.assertIn("[EMAIL_ADDRESS_1]", result["from"])

    def test_user_own_name_redacted_in_tool_response(self):
        """User's own name should be redacted in tool responses to prevent name mixing."""
        from apps.pii.redactor import redact_tool_response
        self.tenant = create_tenant(display_name="Michael Jones", telegram_chat_id=555556)
        data = {
            "from": "Michael Jones <mj@example.com>",
            "to": "alice@example.com",
            "subject": "Hello",
            "body_text": "Hi Alice, this is Michael Jones.",
        }
        result = redact_tool_response(data, self.tenant)

        # User's own name should be redacted in tool responses
        self.assertNotIn("Michael Jones", result["from"])
        self.assertNotIn("Michael Jones", result["body_text"])
        # Should have PERSON placeholders
        self.assertIn("[PERSON_", result["from"])


class AllowNameLastNameTest(TestCase):
    """Test that the user's last name is included in the allow-list."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            import spacy
            spacy.load("en_core_web_sm")
            cls.has_spacy = True
        except (ImportError, OSError):
            cls.has_spacy = False

    def setUp(self):
        if not self.has_spacy:
            self.skipTest("spaCy en_core_web_sm not installed")

    def test_last_name_not_redacted_in_redact_text(self):
        """User's last name alone should not be redacted."""
        tenant = create_tenant(display_name="Michael Jones", telegram_chat_id=700001)
        text = "Email from Jones about the quarterly review."
        result = redact_text(text, tenant=tenant)
        self.assertIn("Jones", result)

    def test_first_name_not_redacted_in_redact_text(self):
        tenant = create_tenant(display_name="Michael Jones", telegram_chat_id=700002)
        text = "Michael mentioned the project timeline."
        result = redact_text(text, tenant=tenant)
        self.assertIn("Michael", result)

    def test_last_name_not_redacted_in_user_message(self):
        from apps.pii.redactor import redact_user_message

        tenant = create_tenant(display_name="Michael Jones", telegram_chat_id=700003)
        text = "Tell Jones I'll be late."
        result = redact_user_message(text, tenant)
        self.assertIn("Jones", result)

    def test_single_name_display_name(self):
        """Single-word display name should still be allowed."""
        tenant = create_tenant(display_name="MJ", telegram_chat_id=700004)
        text = "MJ will handle it."
        result = redact_text(text, tenant=tenant)
        self.assertIn("MJ", result)


class PrivacyRedactionDocTest(TestCase):
    """Test that the privacy-redaction workspace doc is conditionally loaded."""

    def test_starter_tier_includes_privacy_doc(self):
        from apps.orchestrator.personas import render_workspace_files

        tenant = create_tenant(display_name="Doc User", telegram_chat_id=800001)
        tenant.model_tier = "starter"
        tenant.save(update_fields=["model_tier"])

        files = render_workspace_files("neighbor", tenant=tenant)
        self.assertIn("NBHD_DOC_PRIVACY_REDACTION", files)
        self.assertIn("Privacy Placeholders", files["NBHD_DOC_PRIVACY_REDACTION"])

