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


class WordBoundarySubstitutionTests(TestCase):
    """Prod bug (2026-07-03, owner's tenant): the Step 1 known-entity pass
    substituted stored names by naked substring, so legacy mis-minted 3+ char
    fragments in the entity map ("don", "end", "open", "Rest") rewrote the
    INTERIOR of longer words. "Mark the task ... as done." was delivered as
    "[PERSON_335] the task ... as [PERSON_467]e." because "don" ⊂ "done".

    The fix makes the Step 1 pattern word-boundary aware (``\\b`` on each
    alphanumeric/underscore edge). These tests patch ``_detect_pii`` to []
    so only the Step 1 known-entity pass runs — the exact code path the fix
    touches — and need no ONNX model.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=606060)

    def test_prod_repro_interior_don_in_done_not_rewritten(self):
        # Exact production repro. Map holds the mis-minted "don" fragment plus
        # a standalone "Mark". Only the leading standalone "Mark" redacts;
        # "done." at the end must be delivered byte-for-byte.
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {
            "[PERSON_335]": "Mark",
            "[PERSON_467]": "don",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            result = redact_user_message('Mark the task "What task are there" as done.', self.tenant)

        self.assertEqual(result, '[PERSON_335] the task "What task are there" as done.')
        self.assertIn("done.", result)
        self.assertNotIn("[PERSON_467]", result)

    def test_interior_fragments_never_rewrite_longer_words(self):
        # Each of these fragments is a legacy false-positive in the tenant map.
        # None may touch the interior of the word that contains it.
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {
            "[PERSON_1]": "end",
            "[PERSON_2]": "open",
            "[PERSON_3]": "main",
            "[PERSON_4]": "Rest",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            for phrase in ("the weekend", "reopen the domain", "restaurant", "the mainframe"):
                result = redact_user_message(phrase, self.tenant)
                self.assertEqual(result, phrase, f"interior match corrupted {phrase!r}")
                self.assertNotIn("[PERSON_", result)

    def test_standalone_occurrence_still_redacts(self):
        # Boundary-awareness must not break the intended behavior: a stored
        # name that appears as a whole word still redacts.
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {
            "[PERSON_1]": "end",
            "[PERSON_2]": "open",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            result = redact_user_message("open the door at the end", self.tenant)

        self.assertIn("[PERSON_2]", result)  # standalone "open"
        self.assertIn("[PERSON_1]", result)  # standalone "end"
        self.assertNotIn("open", result)
        self.assertNotIn(" end", result.replace("[PERSON_1]", ""))

    def test_punctuation_edge_span_email_still_substitutes(self):
        # An entity whose edges are alnum but body contains punctuation
        # (email) must still substitute — \b sits on the alnum edges and the
        # dots/@ are literal.
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[EMAIL_ADDRESS_1]": "jane.doe84@example.com"}
        self.tenant.save(update_fields=["pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            result = redact_user_message("write to jane.doe84@example.com today", self.tenant)

        self.assertIn("[EMAIL_ADDRESS_1]", result)
        self.assertNotIn("jane.doe84", result)

    def test_non_alnum_edge_name_no_boundary_breakage(self):
        # A stored name ending in "." — the right edge is punctuation, so no
        # \b is anchored there (a \b adjacent to a non-word char would never
        # match). Substitution must still work.
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[PERSON_1]": "Dr."}
        self.tenant.save(update_fields=["pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            result = redact_user_message("saw Dr. today", self.tenant)

        self.assertIn("[PERSON_1]", result)
        self.assertNotIn("Dr.", result)

    def test_hyphenated_and_apostrophe_names_redact_standalone_only(self):
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {
            "[PERSON_1]": "O'Brien",
            "[PERSON_2]": "Jean-Luc",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            standalone = redact_user_message("met O'Brien and Jean-Luc", self.tenant)
            self.assertIn("[PERSON_1]", standalone)
            self.assertIn("[PERSON_2]", standalone)

            # "Jean-Luc" must not rewrite the interior of "Jean-Luca".
            interior = redact_user_message("this is Jean-Luca", self.tenant)
            self.assertEqual(interior, "this is Jean-Luca")
            self.assertNotIn("[PERSON_2]", interior)

    def test_case_insensitive_standalone_redacts_interior_untouched(self):
        # "DON" as a whole word redacts (IGNORECASE); "DONE" is untouched
        # because the boundary after "don" fails against the trailing "e".
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {"[PERSON_1]": "don"}
        self.tenant.save(update_fields=["pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.assertIn("[PERSON_1]", redact_user_message("DON called", self.tenant))
            self.assertEqual(redact_user_message("I am DONE", self.tenant), "I am DONE")

    def test_round_trip_redact_then_rehydrate_preserves_original(self):
        # redact (Step 1 only) → rehydrate must return the original text for
        # all the boundary cases: the interior word survives untouched and the
        # standalone name round-trips through its placeholder.
        from apps.pii.redactor import redact_user_message

        entity_map = {"[PERSON_335]": "Mark", "[PERSON_467]": "don"}
        self.tenant.pii_entity_map = entity_map
        self.tenant.save(update_fields=["pii_entity_map"])

        original = 'Mark the task "What task are there" as done.'
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            redacted = redact_user_message(original, self.tenant)
        self.assertEqual(rehydrate_text(redacted, entity_map), original)


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


class PIIEngineImportSmokeTest(TestCase):
    """Verify the PII engine's import surface — neither the engine module
    nor the transformers pipeline factory should fail at module load time.

    Issue #695 was caused by ``optimum 1.17``'s `input_generators.py`
    referencing ``transformers.utils.is_tf_available`` which had been
    removed; the cold-start ImportError logged for months. We've since
    swapped the model and dropped ``optimum`` entirely (vanilla PyTorch
    CPU on lakshyakh93/deberta_finetuned_pii), so these smoke tests
    catch any regression in the simpler dependency surface.
    """

    def test_transformers_pipeline_factory_imports(self):
        from transformers import AutoTokenizer, pipeline  # noqa: F401

    def test_pii_engine_module_imports(self):
        # Module-level imports only; the heavy model load is gated behind
        # ``get_pii_pipeline()`` so import-time work stays cheap.
        from apps.pii.engine import get_pii_pipeline  # noqa: F401


# ---------------------------------------------------------------------------
# PII false-positive fixes (Bug A garbling + Bug B fitness false positives)
# ---------------------------------------------------------------------------


def _fake_pipeline(hits):
    """Build a stand-in for the HF token-classification pipeline.

    ``hits`` is a list of ``(raw_label, word, score)``. Positions are resolved
    by locating ``word`` in the text so tests read naturally. Returns the dict
    shape the real pipeline emits (``entity_group``/``word``/``score``/
    ``start``/``end``) so ``_detect_pii`` exercises the real label-map +
    threshold + score-override logic.
    """

    def _run(text):
        out = []
        for raw_label, word, score in hits:
            idx = text.find(word)
            if idx < 0:
                continue
            out.append(
                {
                    "entity_group": raw_label,
                    "word": word,
                    "score": score,
                    "start": idx,
                    "end": idx + len(word),
                }
            )
        return out

    return _run


def _assert_clean_placeholders(testcase, text):
    """Every ``[`` in ``text`` must open a well-formed ``[TYPE_N]`` token.

    Catches the Bug A nested-explosion class (``[CRYPTO_ADDRESS_16]'m`` /
    ``[[PERSON_2]]``) where a substitution rewrote a placeholder's interior.
    """
    from apps.pii.redactor import _PLACEHOLDER_RE

    # Number of well-formed placeholders must equal the number of '[' chars,
    # so there are no stray/partial brackets left behind.
    testcase.assertEqual(text.count("["), len(_PLACEHOLDER_RE.findall(text)), f"garbled placeholders in {text!r}")
    testcase.assertNotIn("[[", text)
    testcase.assertNotIn("]]", text)


class BugAGarbleRegressionTest(TestCase):
    """Degenerate entity-map rows (single letters, ``_``, ``[``, ``az``) must
    not garble messages. Bug A: Step 1 substituted these everywhere, including
    inside placeholders it had just emitted, exploding a message into dozens of
    nested tokens. No ONNX model needed — NER is mocked to return nothing so we
    isolate the Step-1 entity-map pass.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=910001)

    def test_all_degenerate_rows_leave_message_untouched(self):
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {
            "[CRYPTO_ADDRESS_16]": "I",
            "[ACCOUNT_18]": "_",
            "[EMAIL_ADDRESS_2]": "[",
            "[CRYPTO_ADDRESS_19]": "u",
            "[IBAN_CODE_1]": "az",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        message = "I'm at the gym. I want you to update my workout"
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            result = redact_user_message(message, self.tenant)

        # With NER returning nothing and every stored row degenerate, the
        # message must come back byte-for-byte identical.
        self.assertEqual(result, message)

    def test_legit_row_survives_alongside_degenerate_rows(self):
        from apps.pii.redactor import redact_user_message

        self.tenant.pii_entity_map = {
            "[PERSON_5]": "Sautai",
            "[CRYPTO_ADDRESS_16]": "I",
            "[ACCOUNT_18]": "_",
            "[CRYPTO_ADDRESS_19]": "u",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            result = redact_user_message("I met Sautai at the gym", self.tenant)

        # Legit entity still redacts; degenerate rows are inert; no nesting.
        self.assertEqual(result, "I met [PERSON_5] at the gym")
        _assert_clean_placeholders(self, result)


class Step1PlaceholderProtectionTest(TestCase):
    """Step 1 must never rewrite the interior of an existing placeholder, even
    when a (non-degenerate) stored name coincides with placeholder text.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=910002)

    def test_stored_name_matching_placeholder_text_does_not_corrupt(self):
        from apps.pii.redactor import redact_user_message

        # Stored name "PERSON" (6 chars, not degenerate) would, without the
        # split-on-placeholder guard, rewrite the "PERSON" inside [PERSON_1].
        self.tenant.pii_entity_map = {
            "[LOCATION_9]": "PERSON",
            "[PERSON_1]": "Sarah",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

        message = "Meeting with [PERSON_1] about the plan"
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            result = redact_user_message(message, self.tenant)

        # The pre-existing placeholder is preserved verbatim.
        self.assertIn("[PERSON_1]", result)
        _assert_clean_placeholders(self, result)


class MintGuardUnitTest(TestCase):
    """Unit coverage for the mint-time guards in ``_filter_results``:
    degenerate span, numeric/unit, fitness vocab (exact + token), common-word
    stoplist (always + sentence-start name-collision), and date/ISO-week.
    """

    def test_degenerate_guard_drops_short_and_punctuation_spans(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        for text, etype in [("I", "CRYPTO_ADDRESS"), ("az", "IBAN_CODE"), ("_", "ACCOUNT"), ("[", "PERSON")]:
            results = [DetectedEntity(etype, 0, len(text), 0.99)]
            self.assertEqual(_filter_results(results, text, set()), [], f"{text!r} should be dropped")

    def test_degenerate_guard_keeps_checksummed_financial_spans(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        text = "4111111111111111"  # 16-digit, ≥3 chars, has digits
        results = [DetectedEntity("CREDIT_CARD", 0, len(text), 1.0)]
        self.assertEqual(len(_filter_results(results, text, set())), 1)

    def test_numeric_guard_drops_bare_numbers_and_units_for_location_person(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        for text, etype in [
            ("225", "LOCATION"),
            ("140kg", "LOCATION"),
            ("82", "PERSON"),
            ("5x5", "LOCATION"),
            ("315 lbs", "LOCATION"),
            ("x10", "PERSON"),
        ]:
            results = [DetectedEntity(etype, 0, len(text), 0.8)]
            self.assertEqual(_filter_results(results, text, set()), [], f"{text!r} should be dropped")

    def test_numeric_guard_does_not_apply_to_financial_types(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        # A digit run typed PHONE_NUMBER/etc. must survive — only LOCATION and
        # PERSON get the numeric guard.
        text = "5551234"
        results = [DetectedEntity("PHONE_NUMBER", 0, len(text), 0.9)]
        self.assertEqual(len(_filter_results(results, text, set())), 1)

    def test_numeric_guard_keeps_real_place_names(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        text = "Tokyo"
        results = [DetectedEntity("LOCATION", 0, len(text), 0.9)]
        self.assertEqual(len(_filter_results(results, text, set())), 1)

    def test_fitness_vocab_guard_drops_exercise_terms(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        for text in ["deadlift", "Romanian Deadlifts", "Spider Curls", "bench press", "creatine"]:
            results = [DetectedEntity("PERSON", 0, len(text), 0.9)]
            self.assertEqual(_filter_results(results, text, set()), [], f"{text!r} should be dropped")

    def test_fitness_vocab_guard_keeps_real_names(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        text = "Sarah"
        results = [DetectedEntity("PERSON", 0, len(text), 0.9)]
        self.assertEqual(len(_filter_results(results, text, set())), 1)

    def test_fitness_token_guard_drops_partial_and_multiword_spans(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        # Spans where the whole string never matches ``_FITNESS_VOCAB`` exactly
        # but a single token gives it away as an exercise note.
        for text in ["inyasa flow", "glute bridge march", "pallof hold", "pec deck flys", "max bench"]:
            results = [DetectedEntity("LOCATION", 0, len(text), 0.97)]
            self.assertEqual(_filter_results(results, text, set()), [], f"{text!r} should be dropped")

    def test_fitness_token_guard_keeps_non_fitness_spans(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        # No fitness token → a real place name survives the token check.
        text = "Baker Street"
        results = [DetectedEntity("LOCATION", 0, len(text), 0.9)]
        self.assertEqual(len(_filter_results(results, text, set())), 1)

    def test_common_word_guard_drops_imperatives_abbrevs_and_ops_nouns(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        for text, etype in [
            ("Rename", "PERSON"),
            ("Felt", "PERSON"),
            ("Steady", "PERSON"),
            ("Mindful", "PERSON"),
            ("JST", "PERSON"),  # timezone
            ("Mar", "PERSON"),  # month abbrev
            ("cron", "PERSON"),
            ("canary", "LOCATION"),
            ("staging", "PERSON"),
        ]:
            results = [DetectedEntity(etype, 0, len(text), 0.95)]
            self.assertEqual(_filter_results(results, text, set()), [], f"{text!r} should be dropped")

    def test_common_word_guard_drops_mark_task_imperative(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        # "Mark task" opens the text: mark (name-collision, at start) + task
        # (always) are both stoplisted → dropped.
        text = 'Mark task "info gathering" as blocked.'
        results = [DetectedEntity("LOCATION", 0, len("Mark task"), 0.83)]
        self.assertEqual(_filter_results(results, text, set()), [])

    def test_name_collision_guard_suppresses_only_at_sentence_start(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        # "Max" opening the text is an imperative ("Max effort…") → dropped.
        start_text = "Max effort on the last rep."
        results = [DetectedEntity("PERSON", 0, 3, 0.98)]
        self.assertEqual(_filter_results(results, start_text, set()), [])

        # "Max" mid-sentence could be a first name → kept.
        mid_text = "Tell Max about the plan."
        idx = mid_text.find("Max")
        results = [DetectedEntity("PERSON", idx, idx + 3, 0.98)]
        self.assertEqual(len(_filter_results(results, mid_text, set())), 1)

    def test_name_collision_guard_keeps_full_name_at_start(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        # A collision word followed by a real surname is a genuine name → kept
        # even at sentence start (not every token is stoplisted).
        text = "Max Verstappen won the race."
        results = [DetectedEntity("PERSON", 0, len("Max Verstappen"), 0.98)]
        self.assertEqual(len(_filter_results(results, text, set())), 1)

    def test_date_like_guard_drops_iso_week_and_dates(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        for text in ["2026-W25", "2026-06-30", "2026-06", "6/30/2026"]:
            results = [DetectedEntity("LOCATION", 0, len(text), 0.99)]
            self.assertEqual(_filter_results(results, text, set()), [], f"{text!r} should be dropped")

    def test_date_like_guard_keeps_street_address(self):
        from apps.pii.redactor import DetectedEntity, _filter_results

        text = "221B Baker Street"
        results = [DetectedEntity("LOCATION", 0, len(text), 0.9)]
        self.assertEqual(len(_filter_results(results, text, set())), 1)


class FitnessFalsePositiveTest(TestCase):
    """Bug B: lift numbers / exercise names must not redact, but real PII in
    the same message still does. NER is mocked at the pipeline level so the
    label-map (BUILDINGNUMBER dropped) and the guards both run for real.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=910003)

    def _redact(self, message, hits):
        from apps.pii.redactor import redact_user_message

        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=_fake_pipeline(hits)),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            return redact_user_message(message, self.tenant)

    def test_lift_numbers_not_redacted(self):
        # BUILDINGNUMBER is now dropped by the label map entirely.
        result = self._redact("I benched 225", [("BUILDINGNUMBER", "225", 0.707)])
        self.assertEqual(result, "I benched 225")

    def test_weight_with_unit_not_redacted(self):
        # STREET → LOCATION, then the numeric+unit guard drops "140kg".
        result = self._redact("squatted 140kg today", [("STREET", "140kg", 0.764)])
        self.assertEqual(result, "squatted 140kg today")

    def test_marginal_pin_number_not_redacted(self):
        result = self._redact("my weight is 82kg", [("PIN", "82", 0.542)])
        self.assertEqual(result, "my weight is 82kg")

    def test_exercise_name_and_rep_scheme_not_redacted(self):
        # FULLNAME is a real DeBERTa label that maps to PERSON — the vocab
        # guard, not the label map, is what suppresses "Romanian Deadlifts".
        result = self._redact(
            "did Romanian Deadlifts 5x5 at 315 lbs",
            [("FULLNAME", "Romanian Deadlifts", 0.85), ("STREET", "315 lbs", 0.7)],
        )
        self.assertEqual(result, "did Romanian Deadlifts 5x5 at 315 lbs")

    def test_real_name_still_redacts_alongside_lift_number(self):
        result = self._redact(
            "tell John Smith I benched 225",
            [("FULLNAME", "John Smith", 0.9), ("BUILDINGNUMBER", "225", 0.707)],
        )
        self.assertNotIn("John Smith", result)
        self.assertIn("[PERSON_1]", result)
        self.assertIn("225", result)  # lift number untouched


class PinScoreOverrideTest(TestCase):
    """PIN keeps redacting, but only at ≥0.7 (the LABEL_SCORE_OVERRIDES entry),
    so marginal ~0.5 hits on lift numbers no longer fire.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=910004)

    def _redact(self, message, hits):
        from apps.pii.redactor import redact_user_message

        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=_fake_pipeline(hits)),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            return redact_user_message(message, self.tenant)

    def test_pin_below_override_not_redacted(self):
        result = self._redact("my PIN is 4821", [("PIN", "4821", 0.6)])
        self.assertEqual(result, "my PIN is 4821")

    def test_pin_above_override_redacted_as_password(self):
        result = self._redact("my PIN is 4821", [("PIN", "4821", 0.8)])
        self.assertNotIn("4821", result)
        self.assertIn("[PASSWORD_1]", result)


class PhoneRecognizerTest(TestCase):
    """The Presidio PhoneRecognizer backstop: the DeBERTa model detects phones
    only inconsistently, so common formats leaked. libphonenumber VALID
    validation catches them without firing on lift/rep/PIN digit-runs.

    The neural model is mocked to empty so only the pattern recognizers run —
    fast and hermetic, and it isolates the phone recognizer's behaviour.
    """

    def _phone_hits(self, text):
        from apps.pii.config import TIER_POLICIES
        from apps.pii.redactor import _detect_pii

        policy = TIER_POLICIES["starter"]
        with patch("apps.pii.engine.get_pii_pipeline", return_value=_fake_pipeline([])):
            hits = _detect_pii(text, policy["entities"], policy["score_threshold"])
        return [h for h in hits if h.entity_type == "PHONE_NUMBER"]

    def test_phone_recognizer_is_registered(self):
        from apps.pii.engine import get_pattern_recognizers

        self.assertIn("PHONE_NUMBER", get_pattern_recognizers())

    def test_common_phone_formats_detected_above_threshold(self):
        for text in [
            "My trainer's cell is (212) 555-0173, text before 8am.",
            "My number is 415-555-0188 if the app logs me out.",
            "Ring the front desk on +44 20 7946 0958.",
            "Text +81 90-1234-5678 when the class is confirmed.",
            "Call the studio at +1-415-555-0142 to book the class.",
        ]:
            self.assertTrue(self._phone_hits(text), f"phone not detected in {text!r}")

    def test_phone_recognizer_ignores_lift_numbers_and_codes(self):
        # The libphonenumber validator must reject fitness/code digit-runs so
        # the backstop never re-introduces the numeric false positives.
        for text in [
            "5x5 at 315 today",
            "The gym door code is 7391 after hours.",
            "my weight is 82kg",
            "Safe combination is 0724 if you need my passport.",
            "3x12 romanian deadlifts at 60kg",
        ]:
            self.assertEqual(self._phone_hits(text), [], f"false phone hit in {text!r}")


class DenylistDegenerateCommandTest(TestCase):
    """``denylist_degenerate_pii`` durably denylists ONLY the Bug-A culprits
    (single-char + punctuation-only spans), keeps all entity-map rows for
    rehydration, and never denylists 2-char alnum spans or legit names.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=910005)
        self.tenant.pii_entity_map = {
            "[CRYPTO_ADDRESS_16]": "I",  # single char → culprit
            "[ACCOUNT_18]": "_",  # punctuation-only → culprit
            "[EMAIL_ADDRESS_2]": "[",  # punctuation-only → culprit
            "[IBAN_CODE_1]": "az",  # 2-char alnum → NOT durably denylisted
            "[PERSON_7]": "Li",  # legit 2-char surname → NOT denylisted
            "[PERSON_5]": "Sautai",  # legit name → untouched
        }
        self.tenant.pii_denylist = {}
        self.tenant.save(update_fields=["pii_entity_map", "pii_denylist"])

    def test_dry_run_writes_nothing(self):
        import io

        from django.core.management import call_command

        out = io.StringIO()
        call_command("denylist_degenerate_pii", stdout=out)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_denylist, {})
        self.assertIn("DRY-RUN", out.getvalue())

    def test_apply_denylists_only_bug_a_culprits_and_keeps_map(self):
        import io

        from django.core.management import call_command

        from apps.pii.entity_registry import get_name

        out = io.StringIO()
        call_command("denylist_degenerate_pii", "--apply", stdout=out)

        self.tenant.refresh_from_db()
        # Single-char + punctuation-only culprits ARE denylisted.
        self.assertIn("i", self.tenant.pii_denylist)
        self.assertIn("_", self.tenant.pii_denylist)
        self.assertIn("[", self.tenant.pii_denylist)
        self.assertEqual(self.tenant.pii_denylist["i"]["reason"], "degenerate")
        # 2-char alnum ("az") and legit 2-char surname ("Li") must NOT be
        # durably denylisted — that would suppress the name forever.
        self.assertNotIn("az", self.tenant.pii_denylist)
        self.assertNotIn("li", self.tenant.pii_denylist)
        self.assertNotIn("sautai", self.tenant.pii_denylist)
        # entity_map rows are ALL KEPT so historical placeholders rehydrate.
        self.assertEqual(len(self.tenant.pii_entity_map), 6)
        self.assertEqual(get_name(self.tenant.pii_entity_map["[PERSON_5]"]), "Sautai")
        self.assertEqual(get_name(self.tenant.pii_entity_map["[PERSON_7]"]), "Li")


class PiiReuseTelemetryTest(TestCase):
    """Reuse of an EXISTING placeholder for a newly DETECTED span (the row-locked
    mint-path branch) must emit a PII-safe ``pii_reuse`` line so same-name fusion
    — two different people collapsing onto one placeholder, silently and
    permanently — becomes measurable. A fresh mint must NOT emit it, and the line
    must never carry the raw detected span (these logs ship to Log Analytics in
    cleartext). ``_detect_pii`` is mocked, so no ONNX model is needed; the tests
    drive the mint/reuse branch directly. The routine Step-1 regex rewrites of
    already-known names are deliberately NOT instrumented (they fire on every
    message and would flood), so they are not exercised here.
    """

    LOGGER = "apps.pii.redactor"
    NAME = "Marcus Delgado"  # clean two-word PERSON; survives _filter_results

    def setUp(self):
        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=920100)
        self.tenant.pii_entity_map = {}

    def _person_spans(self, text):
        # PERSON spans at the real offsets of every NAME occurrence, so the
        # mocked _detect_pii lines up with the (unchanged) post-Step-1 text.
        from apps.pii.redactor import DetectedEntity

        spans = []
        idx = text.find(self.NAME)
        while idx != -1:
            spans.append(DetectedEntity("PERSON", idx, idx + len(self.NAME), 0.95))
            idx = text.find(self.NAME, idx + 1)
        return spans

    def test_same_message_duplicate_logs_pii_reuse(self):
        # Same brand-new name twice in one message: the first occurrence mints,
        # the second reuses the just-minted placeholder within the SAME call.
        from apps.pii.redactor import redact_user_message

        text = f"Tell {self.NAME} and {self.NAME} about it"
        spans = self._person_spans(text)
        self.assertEqual(len(spans), 2)

        with (
            patch("apps.pii.redactor._detect_pii", return_value=spans),
            self.assertLogs(self.LOGGER, level="INFO") as cm,
        ):
            result = redact_user_message(text, self.tenant)

        # Both occurrences collapse onto the single minted placeholder.
        self.assertEqual(result.count("[PERSON_1]"), 2)
        self.assertNotIn("[PERSON_2]", result)

        reuse = [ln for ln in cm.output if "pii_reuse" in ln]
        mint = [ln for ln in cm.output if "pii_mint" in ln]
        self.assertEqual(len(mint), 1, cm.output)
        self.assertEqual(len(reuse), 1, cm.output)
        self.assertIn("type=PERSON", reuse[0])
        self.assertIn("placeholder=[PERSON_1]", reuse[0])
        self.assertIn("source=same_message", reuse[0])
        # The raw detected span must never appear in ANY log line.
        for ln in cm.output:
            self.assertNotIn(self.NAME, ln)

    def test_reuse_from_persisted_map_logs_source_concurrent(self):
        # Placeholder already in the DB row but NOT in this tenant object's
        # in-memory map (a stale snapshot / concurrent-mint race): Step 1 and the
        # function-start known check both miss it, so the freshly detected span
        # reaches the mint path and reuses the row-locked placeholder.
        from apps.pii.redactor import redact_user_message

        Tenant = type(self.tenant)
        Tenant.objects.filter(pk=self.tenant.pk).update(pii_entity_map={"[PERSON_1]": self.NAME})
        self.tenant.pii_entity_map = {}  # in-memory view is intentionally stale

        text = f"Ping {self.NAME} tonight"
        spans = self._person_spans(text)

        with (
            patch("apps.pii.redactor._detect_pii", return_value=spans),
            self.assertLogs(self.LOGGER, level="INFO") as cm,
        ):
            result = redact_user_message(text, self.tenant)

        self.assertIn("[PERSON_1]", result)
        self.assertNotIn(self.NAME, result)

        reuse = [ln for ln in cm.output if "pii_reuse" in ln]
        mint = [ln for ln in cm.output if "pii_mint" in ln]
        self.assertEqual(len(reuse), 1, cm.output)
        self.assertEqual(len(mint), 0, cm.output)  # nothing new was minted
        self.assertIn("source=concurrent", reuse[0])
        self.assertIn("placeholder=[PERSON_1]", reuse[0])
        for ln in cm.output:
            self.assertNotIn(self.NAME, ln)

    def test_fresh_mint_does_not_log_pii_reuse(self):
        # A single brand-new name mints and must NOT emit pii_reuse.
        from apps.pii.redactor import redact_user_message

        text = f"Ping {self.NAME} tonight"
        spans = self._person_spans(text)

        with (
            patch("apps.pii.redactor._detect_pii", return_value=spans),
            self.assertLogs(self.LOGGER, level="INFO") as cm,
        ):
            redact_user_message(text, self.tenant)

        self.assertEqual([ln for ln in cm.output if "pii_reuse" in ln], [], cm.output)
        self.assertEqual(len([ln for ln in cm.output if "pii_mint" in ln]), 1, cm.output)
