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
    """Integration tests that run the full PII detection pipeline.

    These tests require the ONNX PII model to be available.
    They are skipped in environments without it.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from apps.pii.engine import get_pii_pipeline

            get_pii_pipeline()
            cls.has_model = True
        except Exception:
            cls.has_model = False

    def setUp(self):
        if not self.has_model:
            self.skipTest("PII detection model not available")

    def test_redacts_email_address(self):
        text = "Send the report to sarah.jones@acme.com by Friday."
        result = redact_text(text, tier="starter")
        self.assertNotIn("sarah.jones@acme.com", result)
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
        text = "Had a productive meeting with Sarah Chen about the roadmap."
        result = redact_text(text, tier="starter")
        self.assertNotIn("Sarah Chen", result)
        self.assertIn("[PERSON_", result)

    def test_allows_tenant_display_name(self):
        tenant = create_tenant(display_name="Michael Jones", telegram_chat_id=222222)
        text = "Michael mentioned that Sarah Chen should join the meeting."
        result = redact_text(text, tenant=tenant)
        self.assertIn("Michael", result)
        self.assertNotIn("Sarah Chen", result)

    def test_ambiguous_name_handled_contextually(self):
        text = "Jordan called me about the project."
        result = redact_text(text, tier="starter")
        # "Jordan" is ambiguous (person vs country) — model handles contextually.
        # Either detection is acceptable; we just verify no crash.
        self.assertIsInstance(result, str)

    def test_multiple_entities_numbered(self):
        text = "Email bob.smith@acme.com and alice.johnson@acme.com about the project."
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
        self.assertIn("# 2026-03-26", result)
        self.assertIn("## Reflections", result)

    def test_redaction_error_returns_original(self):
        # Patch `_redact` itself so the outer try/except in `redact_text`
        # fires. Patching only the DeBERTa pipeline isn't sufficient:
        # `_detect_pii` swallows DeBERTa failures and falls through to
        # Presidio pattern recognizers, which catch emails on their own.
        text = "Some text with sarah@test.com"
        with patch("apps.pii.redactor._redact", side_effect=RuntimeError("boom")):
            result = redact_text(text, tier="starter")
        self.assertEqual(result, text)

    def test_deberta_failure_falls_back_to_pattern_recognizers(self):
        # Documents the resilience behaviour: even if the DeBERTa model
        # fails to load, Presidio's email/CC/IBAN recognizers still run.
        # This is why patching only `get_pii_pipeline` doesn't simulate
        # a full redaction error.
        text = "Some text with sarah@test.com"
        with patch("apps.pii.engine.get_pii_pipeline", side_effect=RuntimeError("boom")):
            result = redact_text(text, tier="starter")
        self.assertNotIn("sarah@test.com", result)
        self.assertIn("[EMAIL_ADDRESS_", result)


class RedactionSessionTest(TestCase):
    """Test RedactionSession for cross-document entity tracking."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from apps.pii.engine import get_pii_pipeline

            get_pii_pipeline()
            cls.has_model = True
        except Exception:
            cls.has_model = False

    def setUp(self):
        if not self.has_model:
            self.skipTest("PII detection model not available")

    def test_cross_document_numbering(self):
        session = RedactionSession(tier="starter")
        doc1 = session.redact("Email from alice.johnson@acme.com about the project.")
        doc2 = session.redact("Reply to bob.smith@acme.com with details.")

        # First doc gets _1, second doc gets _2
        self.assertIn("[EMAIL_ADDRESS_1]", doc1)
        self.assertIn("[EMAIL_ADDRESS_2]", doc2)

    def test_entity_map_populated(self):
        session = RedactionSession(tier="starter")
        session.redact("Contact alice.johnson@acme.com for info.")

        self.assertIn("[EMAIL_ADDRESS_1]", session.entity_map)
        self.assertEqual(session.entity_map["[EMAIL_ADDRESS_1]"], "alice.johnson@acme.com")

    def test_entity_map_spans_documents(self):
        session = RedactionSession(tier="starter")
        session.redact("Email alice.johnson@acme.com about the project.")
        session.redact("Also email bob.smith@acme.com with the update.")

        self.assertEqual(session.entity_map["[EMAIL_ADDRESS_1]"], "alice.johnson@acme.com")
        self.assertEqual(session.entity_map["[EMAIL_ADDRESS_2]"], "bob.smith@acme.com")


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
        try:
            from apps.pii.engine import get_pii_pipeline

            get_pii_pipeline()
        except Exception:
            self.skipTest("PII detection model not available")

        session = RedactionSession(tier="starter")
        original = "Emailed alice.johnson@acme.com for help."
        redacted = session.redact(original)

        self.assertNotIn("alice.johnson@acme.com", redacted)
        self.assertIn("[EMAIL_ADDRESS_1]", redacted)

        # Simulate model response referencing the placeholder
        model_response = "I've noted to contact [EMAIL_ADDRESS_1] for help."
        rehydrated = rehydrate_text(model_response, session.entity_map)

        self.assertIn("alice.johnson@acme.com", rehydrated)
        self.assertNotIn("[EMAIL_ADDRESS_1]", rehydrated)


class RedactUserMessageTest(TestCase):
    """Test Phase 2: user message redaction with entity map consistency."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from apps.pii.engine import get_pii_pipeline

            get_pii_pipeline()
            cls.has_model = True
        except Exception:
            cls.has_model = False

    def setUp(self):
        if not self.has_model:
            self.skipTest("PII detection model not available")
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=333333)

    def test_redacts_email_in_user_message(self):
        from apps.pii.redactor import redact_user_message

        result = redact_user_message("Send it to alice.johnson@acme.com", self.tenant)
        self.assertNotIn("alice.johnson@acme.com", result)
        self.assertIn("[EMAIL_ADDRESS_", result)

    def test_reuses_known_entities(self):
        from apps.pii.redactor import redact_user_message

        # Pre-populate entity map (as if Phase 1 workspace sync ran)
        self.tenant.pii_entity_map = {"[EMAIL_ADDRESS_1]": "alice.johnson@acme.com"}
        self.tenant.save(update_fields=["pii_entity_map"])

        result = redact_user_message("Email alice.johnson@acme.com about the update.", self.tenant)
        # Should reuse the existing placeholder, not create a new one
        self.assertIn("[EMAIL_ADDRESS_1]", result)
        self.assertNotIn("[EMAIL_ADDRESS_2]", result)

    def test_new_entities_get_next_number(self):
        from apps.pii.redactor import redact_user_message

        # Pre-populate with one entity
        self.tenant.pii_entity_map = {"[EMAIL_ADDRESS_1]": "alice.johnson@acme.com"}
        self.tenant.save(update_fields=["pii_entity_map"])

        result = redact_user_message("Contact bob.smith@acme.com for details.", self.tenant)
        self.assertNotIn("bob.smith@acme.com", result)
        # Should be _2 since _1 already exists
        self.assertIn("[EMAIL_ADDRESS_2]", result)

    def test_new_entities_persisted_to_db(self):
        from apps.pii.entity_registry import get_name
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {}
        self.tenant.save(update_fields=["pii_entity_map"])

        redact_user_message("Contact bob.smith@acme.com for details.", self.tenant)

        # Reload from DB
        self.tenant.refresh_from_db()
        self.assertTrue(len(self.tenant.pii_entity_map) > 0)
        # Should contain the new email — read via registry helper so the
        # assertion is shape-agnostic (entries are now dicts with a
        # ``name`` field; legacy string entries still readable).
        names = {get_name(v) for v in self.tenant.pii_entity_map.values()}
        self.assertIn("bob.smith@acme.com", names)

    def test_empty_message_unchanged(self):
        from apps.pii.redactor import redact_user_message

        self.assertEqual(redact_user_message("", self.tenant), "")
        self.assertEqual(redact_user_message("  ", self.tenant), "  ")


class CaseInsensitiveMergeTests(TestCase):
    """Bug from canary audit (2026-05-21): 826-entry pii_entity_map had
    "sautai" stored 59 times under different case-variant placeholders.
    The Step 1 regex pass + post-NER lookup were case-sensitive, so user
    typing "sautai" after "Sautai" was already in the map silently
    minted a fresh placeholder every time.

    These tests don't require the ONNX PII model — they exercise the
    Step 1 known-entity pass and the RedactionSession seed logic, both
    of which run before NER.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=555555)

    def test_case_variant_in_message_reuses_known_placeholder(self):
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[PERSON_5]": "Sautai"}
        self.tenant.save(update_fields=["pii_entity_map"])

        result = redact_user_message("hi sautai", self.tenant)
        self.assertIn("[PERSON_5]", result)
        self.assertNotIn("sautai", result.lower().replace("[person_5]", ""))

        # Map must not have grown — no fresh mint for the case variant.
        self.tenant.refresh_from_db()
        self.assertEqual(list(self.tenant.pii_entity_map.keys()), ["[PERSON_5]"])

    def test_multiple_case_variants_in_one_message_all_collapse(self):
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[PERSON_5]": "Sautai"}
        self.tenant.save(update_fields=["pii_entity_map"])

        result = redact_user_message("met Sautai and sautai and SAUTAI today", self.tenant)
        # All three occurrences become the same placeholder.
        self.assertEqual(result.count("[PERSON_5]"), 3)
        self.assertNotIn("[PERSON_6]", result)

    def test_whitespace_padded_entry_still_matches(self):
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[PERSON_5]": "  Sautai  "}
        self.tenant.save(update_fields=["pii_entity_map"])

        result = redact_user_message("hi Sautai", self.tenant)
        self.assertIn("[PERSON_5]", result)

    def test_empty_value_in_map_does_not_crash_regex_pass(self):
        # Empty originals would explode the regex pass: re.escape("") is
        # "", and re.sub("", X, text) inserts X between every character.
        # The redactor must defend against that.
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {
            "[PERSON_1]": "",
            "[PERSON_2]": "Sautai",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        result = redact_user_message("hi Sautai", self.tenant)
        # Sautai still gets caught; empty entry just gets skipped silently.
        self.assertIn("[PERSON_2]", result)
        # And no garbled splatter from the empty regex.
        self.assertNotIn("[PERSON_1]hi", result)

    def test_legacy_duplicate_placeholders_both_rehydrate(self):
        # Backwards-compat: tenants out there have maps like the canary's,
        # with [PERSON_5] AND [PERSON_408] both pointing to "sautai". We
        # don't compact the map (that needs a separate audit of every
        # storage location holding placeholder text), so both must keep
        # rehydrating correctly.
        m = {
            "[PERSON_5]": "Sautai",
            "[PERSON_408]": "sautai",
        }
        self.assertEqual(rehydrate_text("[PERSON_5] and [PERSON_408]", m), "Sautai and sautai")

    def test_session_seeds_counters_from_tenant_map(self):
        # The latent collision bug: RedactionSession starts counters at
        # 0, so first mint becomes [PERSON_1] regardless of what the
        # tenant map already holds. memory_sync then does dict-union,
        # which clobbers the existing [PERSON_1] -> whoever with the
        # new entity. Seeding fixes this side effect.
        self.tenant.pii_entity_map = {
            "[PERSON_1]": "Alice",
            "[PERSON_3]": "Bob",
            "[EMAIL_ADDRESS_2]": "x@y.com",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        session = RedactionSession(tenant=self.tenant)
        self.assertEqual(session._type_counters.get("PERSON"), 3)
        self.assertEqual(session._type_counters.get("EMAIL_ADDRESS"), 2)

    def test_session_seeds_inverted_ci_from_tenant_map(self):
        self.tenant.pii_entity_map = {"[PERSON_5]": "Sautai"}
        self.tenant.save(update_fields=["pii_entity_map"])

        session = RedactionSession(tenant=self.tenant)
        self.assertIn("sautai", session._inverted_ci)
        self.assertEqual(session._inverted_ci["sautai"][1], "[PERSON_5]")


class DenylistTests(TestCase):
    """Tenant-level deny lever for the NER over-detection class
    (Issue #660). Users mark "goal" / "calendar" / "🏆 wins" as
    not-PII; the redactor stops substituting placeholders for them
    on both new detections AND legacy entity_map entries.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=777777)

    def test_legacy_entity_map_entry_skipped_when_denylisted(self):
        # The canary scenario: a false-positive "goal" was already in
        # the map as [PERSON_408] from before this fix. User denylists
        # it. New messages containing "goal" should NOT get redacted.
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[PERSON_408]": "goal"}
        self.tenant.pii_denylist = {"goal": {"reason": "manual"}}
        self.tenant.save(update_fields=["pii_entity_map", "pii_denylist"])

        result = redact_user_message("My goal is to run 5k", self.tenant)
        self.assertNotIn("[PERSON_408]", result)
        self.assertIn("goal", result)

    def test_denylist_match_is_case_insensitive(self):
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[PERSON_408]": "goal"}
        self.tenant.pii_denylist = {"goal": {}}
        self.tenant.save(update_fields=["pii_entity_map", "pii_denylist"])

        # Variant casings should all bypass redaction.
        for variant in ["goal", "Goal", "GOAL"]:
            result = redact_user_message(f"Today's {variant} is a 5k", self.tenant)
            self.assertNotIn("[PERSON_", result, f"failed for variant {variant!r}")

    def test_legacy_entry_still_rehydrates(self):
        # Critical safety: denylisting an entry stops it from driving
        # redaction but does NOT remove it from the map. Stored text
        # in workspace files / chat history that still references the
        # placeholder must rehydrate correctly.
        m = {"[PERSON_408]": "goal"}
        denylist = {"goal": {}}
        self.tenant.pii_entity_map = m
        self.tenant.pii_denylist = denylist
        self.tenant.save(update_fields=["pii_entity_map", "pii_denylist"])

        # Outgoing path: rehydrate_text doesn't consult denylist; the
        # entry still maps placeholder -> name, so the user sees the
        # original word in any old text that referenced [PERSON_408].
        self.assertEqual(
            rehydrate_text("Old message about [PERSON_408]", m),
            "Old message about goal",
        )

    def test_empty_denylist_preserves_today_behavior(self):
        # Backwards-compat guard: a tenant with no denylist must
        # behave exactly as before this PR. The "goal" entry should
        # still drive Step 1 regex redaction.
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[PERSON_408]": "goal"}
        self.tenant.pii_denylist = {}
        self.tenant.save(update_fields=["pii_entity_map", "pii_denylist"])

        result = redact_user_message("My goal is to run", self.tenant)
        self.assertIn("[PERSON_408]", result)

    def test_session_inherits_denylist(self):
        # Workspace memory sync runs through RedactionSession. The
        # denylist must propagate so workspace doc redaction matches
        # inbound-message redaction.
        self.tenant.pii_denylist = {"goal": {}}
        self.tenant.save(update_fields=["pii_denylist"])

        session = RedactionSession(tenant=self.tenant)
        self.assertEqual(session._denylist, {"goal": {}})

    def test_new_mint_suppressed_when_denylisted(self):
        # Forces a synthetic NER hit on "goal" to verify the post-NER
        # filter path drops denylisted spans before they reach mint.
        # Doesn't require the actual ONNX model.
        from apps.pii.redactor import DetectedEntity, _filter_results

        self.tenant.pii_denylist = {"goal": {}}
        results = [DetectedEntity("PERSON", 0, 4, 0.95)]
        text = "goal"

        filtered = _filter_results(results, text, set(), denylist=self.tenant.pii_denylist)
        self.assertEqual(filtered, [])


class RedactTelegramUpdateTest(TestCase):
    """Test Telegram update redaction for the webhook path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from apps.pii.engine import get_pii_pipeline

            get_pii_pipeline()
            cls.has_model = True
        except Exception:
            cls.has_model = False

    def setUp(self):
        if not self.has_model:
            self.skipTest("PII detection model not available")
        self.tenant = create_tenant(display_name="Webhook User", telegram_chat_id=444444)

    def test_redacts_message_text(self):
        from apps.pii.redactor import redact_telegram_update

        update = {
            "update_id": 12345,
            "message": {
                "message_id": 1,
                "text": "Email alice.johnson@acme.com about the project.",
                "chat": {"id": 444444},
            },
        }
        result = redact_telegram_update(update, self.tenant)
        self.assertNotIn("alice.johnson@acme.com", result["message"]["text"])

    def test_redacts_edited_message(self):
        from apps.pii.redactor import redact_telegram_update

        update = {
            "update_id": 12345,
            "edited_message": {
                "message_id": 1,
                "text": "Updated: contact bob.smith@acme.com instead.",
                "chat": {"id": 444444},
            },
        }
        result = redact_telegram_update(update, self.tenant)
        self.assertNotIn("bob.smith@acme.com", result["edited_message"]["text"])

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
            from apps.pii.engine import get_pii_pipeline

            get_pii_pipeline()
            cls.has_model = True
        except Exception:
            cls.has_model = False

    def setUp(self):
        if not self.has_model:
            self.skipTest("PII detection model not available")
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
                    "from": "alice@acme.com",
                    "date": "Mon, 25 Mar 2026 10:00:00 -0700",
                    "internal_date": "1711375200000",
                },
            ],
            "result_size_estimate": 1,
        }
        result = redact_tool_response(data, self.tenant)

        # Email in 'from' should be redacted
        self.assertNotIn("alice@acme.com", result["messages"][0]["from"])
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
            "from": "bob@acme.com",
            "to": "user@acme.com",
            "subject": "Follow-up",
            "body_text": "Hi, please call me at my phone number 555-867-5309.",
            "body_truncated": False,
            "thread_context": [],
        }
        result = redact_tool_response(data, self.tenant)

        # from/to should be redacted
        self.assertNotIn("bob@acme.com", result["from"])
        self.assertNotIn("user@acme.com", result["to"])
        # ID preserved
        self.assertEqual(result["id"], "msg123")

    def test_redacts_calendar_summary(self):
        from apps.pii.redactor import redact_tool_response

        data = {
            "events": [
                {
                    "id": "evt123",
                    "summary": "Meeting with Sarah Chen",
                    "status": "confirmed",
                    "html_link": "https://calendar.google.com/event?id=evt123",
                    "start": {"dateTime": "2026-03-26T12:00:00"},
                    "end": {"dateTime": "2026-03-26T13:00:00"},
                },
            ],
        }
        result = redact_tool_response(data, self.tenant)

        # Person name in summary should be redacted
        self.assertNotIn("Sarah Chen", result["events"][0]["summary"])
        # ID and structural fields preserved
        self.assertEqual(result["events"][0]["id"], "evt123")
        self.assertEqual(result["events"][0]["status"], "confirmed")

    def test_handles_nested_lists(self):
        from apps.pii.redactor import redact_tool_response

        data = {
            "thread_context": [
                {"id": "t1", "from": "alice.johnson@acme.com", "snippet": "test"},
                {"id": "t2", "from": "bob.smith@acme.com", "snippet": "reply"},
            ],
        }
        result = redact_tool_response(data, self.tenant)
        # IDs preserved
        self.assertEqual(result["thread_context"][0]["id"], "t1")
        # Emails redacted
        self.assertNotIn("alice.johnson@acme.com", result["thread_context"][0]["from"])

    def test_reuses_known_entities_from_map(self):
        from apps.pii.redactor import redact_tool_response

        self.tenant.pii_entity_map = {"[EMAIL_ADDRESS_1]": "alice.johnson@acme.com"}
        self.tenant.save(update_fields=["pii_entity_map"])

        data = {"from": "alice.johnson@acme.com", "subject": "Hello"}
        result = redact_tool_response(data, self.tenant)

        # Should reuse the known placeholder
        self.assertIn("[EMAIL_ADDRESS_1]", result["from"])

    def test_user_own_name_redacted_in_tool_response(self):
        """User's own name should be redacted in tool responses to prevent name mixing."""
        from apps.pii.redactor import redact_tool_response

        self.tenant = create_tenant(display_name="Michael Jones", telegram_chat_id=555556)
        data = {
            "from": "Michael Jones <mj@acme.com>",
            "to": "alice@acme.com",
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
            from apps.pii.engine import get_pii_pipeline

            get_pii_pipeline()
            cls.has_model = True
        except Exception:
            cls.has_model = False

    def setUp(self):
        if not self.has_model:
            self.skipTest("PII detection model not available")

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
