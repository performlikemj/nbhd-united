"""Tests for the deterministic PII span-hygiene layer (apps/pii/hygiene.py).

Table-driven over the production audit's junk taxonomy (979/1103 bindings were
junk). Neutral stand-ins replace real names/values; no real PII appears here.

The hygiene functions are pure stdlib, so the unit tests need no DB. The final
class drives ``redact_user_message`` with a stubbed detector to prove the wiring
drops junk BEFORE it mints — mirroring the ``_detect_pii`` patch pattern in
apps/pii/tests.py.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.pii.hygiene import (
    is_junk_span,
    mask_placeholders,
    snap_to_word_boundaries,
    validate_structured,
)

# A zero-width space and a word joiner — the invisible runs the audit found
# minted from newsletter HTML in tool payloads. Built from code points so the
# source file carries no literal invisible characters.
_ZWSP = chr(0x200B)
_WORD_JOINER = chr(0x2060)


class IsJunkSpanTest(SimpleTestCase):
    """The junk taxonomy — every class from the audit with real-shaped examples."""

    # (label, text, entity_type, expected_reason_substring)
    JUNK_CASES = [
        # (a) markdown / structure fragments from agent-authored memory-sync notes
        ("table_divider", "|----|----|", "LOCATION", "structure"),
        ("heading_time", "### 08:05 — Neighbor", "PERSON", "structure"),
        ("list_bold_timestamp", "- **06:02**", "PERSON", "structure"),
        ("heading_quick_wins", "## Quick Wins", "PERSON", "structure"),
        ("multiline", "Quick Wins\n- something", "PERSON", "structure"),
        # The exact fleet-audit fragment shape: 239 live "quick wins\n-"
        # PERSON bindings across tenants. Newline and list bullet each drop
        # the span on their own, so this class dies regardless of vocabulary.
        ("quick_wins_bullet_reply", "Quick Wins\n- reply", "PERSON", "structure"),
        ("leading_bullet", "- reply to the note", "PERSON", "structure"),
        ("bold_marker", "**Neighbor**", "PERSON", "structure"),
        ("hr_rule", "Section ----", "LOCATION", "structure"),
        ("pipe_row", "Mon | Tue | Wed", "LOCATION", "structure"),
        # (b) invisible / zero-width runs + HTML entities from raw tool responses
        ("zero_width", "John" + _ZWSP + "Doe", "PERSON", "invisible"),
        ("word_joiner", _WORD_JOINER + "Newsletter", "PERSON", "invisible"),
        ("html_entity_apos", "Tom &#39;s Weekly", "PERSON", "invisible"),
        ("html_entity_lt", "&lt;sender&gt;", "PERSON", "invisible"),
        # (c) neural financial labels with no validation → dropped by validate,
        #     but the code-identifier ones are also structural junk on PERSON.
        # (d) word-boundary fragments + self-redaction of placeholder text
        ("placeholder_close", "CODE_1]ADDRESS", "PERSON", "placeholder_fragment"),
        ("placeholder_open", "[CRYP", "LOCATION", "placeholder_fragment"),
        ("bare_placeholder", "PERSON_44", "LOCATION", "placeholder_fragment"),
        # (e) dates / times / bare numbers mislabeled as structured types
        ("iso_date_account", "2026-05-30", "ACCOUNT", "numeric_datelike"),
        ("iso_week_location", "2026-W25", "LOCATION", "numeric_datelike"),
        ("clock_password", "08:05", "PASSWORD", "numeric_datelike"),
        ("clock_ip", "18:29:00", "IP_ADDRESS", "numeric_datelike"),
        ("slash_date_person", "5/30/2026", "PERSON", "numeric_datelike"),
        ("bare_number_location", "82", "LOCATION", "numeric_datelike"),
        ("range_temp_location", "18–29°C", "LOCATION", "numeric_datelike"),
        ("measurement_person", "140kg", "PERSON", "numeric_datelike"),
        ("sets_person", "5x5", "PERSON", "numeric_datelike"),
        # code identifiers mislabeled as names/places (agent notes / tool output)
        ("filename_person", "config.py", "PERSON", "identifier"),
        ("dotted_module_location", "apps.pii.redactor", "LOCATION", "identifier"),
        ("kebab_person", "site-publishing", "PERSON", "identifier"),
        ("snake_person", "get_display_name", "PERSON", "identifier"),
        ("commit_sha_person", "2430d3d3", "PERSON", "identifier"),
        ("url_location", "https://example.com/x", "LOCATION", "identifier"),
        # too-short / featureless
        ("single_char", "J", "PERSON", "too_short"),
        ("pure_punct", "--", "PERSON", "too_short"),
    ]

    NOT_JUNK_CASES = [
        # Real PII that MUST survive — false-junk here is the failure mode.
        ("location_country", "Jamaica", "LOCATION"),
        ("location_multiword", "Nottingham Forest", "LOCATION"),
        ("person_first", "Sarah", "PERSON"),
        ("person_full", "Sarah Chen", "PERSON"),
        ("person_hyphenated", "Jean-Luc", "PERSON"),
        ("person_apostrophe", "O'Brien", "PERSON"),
        ("email", "sarah.jones@example.com", "EMAIL_ADDRESS"),
        ("credit_card_digits", "4111111111111111", "CREDIT_CARD"),
        ("phone_formatted", "(818) 555-0134", "PHONE_NUMBER"),
        ("zip_five", "94103", "LOCATION"),
        ("zip_plus_four", "94103-1234", "LOCATION"),
        ("location_abbrev", "U.S.", "LOCATION"),
        ("location_hyphen_region", "Baden-Württemberg", "LOCATION"),
        ("crypto_address", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "CRYPTO_ADDRESS"),
        ("two_letter_name", "Bo", "PERSON"),
    ]

    def test_junk_spans_flagged_with_reason(self):
        for label, text, etype, reason in self.JUNK_CASES:
            with self.subTest(case=label):
                junk, got_reason = is_junk_span(text, etype)
                self.assertTrue(junk, f"{label!r} should be junk")
                self.assertEqual(got_reason, reason, f"{label!r} wrong reason")

    def test_real_pii_not_flagged(self):
        for label, text, etype in self.NOT_JUNK_CASES:
            with self.subTest(case=label):
                junk, reason = is_junk_span(text, etype)
                self.assertFalse(junk, f"{label!r} wrongly flagged junk ({reason})")

    def test_empty_and_whitespace_are_too_short(self):
        for text in ("", "   ", "\n"):
            junk, reason = is_junk_span(text, "PERSON")
            self.assertTrue(junk)
            self.assertEqual(reason, "too_short")

    def test_digit_run_not_junk_for_structured_types(self):
        # A long digit run is a valid shape for a card/account — the bare-number
        # rule must NOT apply outside PERSON/LOCATION (validate_structured gates
        # those types instead).
        for etype in ("CREDIT_CARD", "ACCOUNT", "PHONE_NUMBER"):
            junk, _ = is_junk_span("4111111111111111", etype)
            self.assertFalse(junk, f"digit run wrongly junked for {etype}")

    def test_hyphenated_phone_not_datelike(self):
        # "555-1234" looks like a range but is a real phone — the range rule is
        # scoped to PERSON/LOCATION so it never reaches this type.
        junk, _ = is_junk_span("555-1234", "PHONE_NUMBER")
        self.assertFalse(junk)


class ValidateStructuredTest(SimpleTestCase):
    """Checksum / shape validation for the structured types."""

    VALID = [
        ("email_simple", "EMAIL_ADDRESS", "bob@example.com"),
        # Relay-obfuscated real address (duck.com forwarders rewrite the sender
        # as local_at_domain) — the labeled prod eval's single false-junk; must
        # validate so a real contact email can't be swept as junk.
        ("email_relay_at", "EMAIL_ADDRESS", "jane.doe_at_example.com"),
        ("email_relay_full", "EMAIL_ADDRESS", "jane.doe_at_example.com_alias@duck.com"),
        ("luhn_visa", "CREDIT_CARD", "4111111111111111"),
        ("luhn_spaced", "CREDIT_CARD", "4111 1111 1111 1111"),
        ("iban_gb", "IBAN_CODE", "GB82 WEST 1234 5698 7654 32"),
        ("phone_us", "PHONE_NUMBER", "(818) 555-0134"),
        ("phone_intl", "PHONE_NUMBER", "+44 20 7946 0018"),
        ("account_iban_like", "ACCOUNT", "GB29NWBK60161331926819"),
        ("password_mixed", "PASSWORD", "hunter2"),
        ("password_pin", "PASSWORD", "4821"),  # a 4-digit PIN is a valid PASSWORD
        ("crypto_btc", "CRYPTO_ADDRESS", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"),
        ("ipv4", "IP_ADDRESS", "192.168.1.1"),
        ("id_vin", "ID_DOCUMENT", "1HGCM82633A004352"),
    ]

    INVALID = [
        # (c) the exact audit false positives
        ("django_card", "CREDIT_CARD", "django"),
        ("usermd_card", "CREDIT_CARD", "USER.md"),
        ("temp_range_account", "ACCOUNT", "18–29°C"),
        ("date_account", "ACCOUNT", "2026-05-30"),
        ("bad_luhn", "CREDIT_CARD", "4111111111111112"),
        ("short_digits_card", "CREDIT_CARD", "123"),
        ("bad_iban_checksum", "IBAN_CODE", "GB00WEST12345698765432"),
        ("not_email", "EMAIL_ADDRESS", "just some words"),
        ("phone_too_short", "PHONE_NUMBER", "12345"),
        ("phone_is_date", "PHONE_NUMBER", "2026-05-30"),
        ("account_too_short", "ACCOUNT", "ab12"),
        ("password_too_short", "PASSWORD", "12"),  # below the PIN floor
        ("account_no_digit", "ACCOUNT", "django"),
        ("bad_ip_octet", "IP_ADDRESS", "999.1.1.1"),
    ]

    def test_valid_structured_pass(self):
        for label, etype, text in self.VALID:
            with self.subTest(case=label):
                self.assertTrue(validate_structured(etype, text), f"{label!r} should pass")

    def test_invalid_structured_fail(self):
        for label, etype, text in self.INVALID:
            with self.subTest(case=label):
                self.assertFalse(validate_structured(etype, text), f"{label!r} should fail")

    def test_person_location_return_false_by_contract(self):
        # Free-form types have no structural shape. validate_structured returns
        # False so the redactor mint gate (_should_mint_new, mint='validated')
        # never mints a tool-response neural name. The FILTER never calls this on
        # PERSON/LOCATION, so this does not suppress names on the chat path.
        self.assertFalse(validate_structured("PERSON", "Sarah Chen"))
        self.assertFalse(validate_structured("LOCATION", "Tokyo"))

    def test_empty_is_invalid(self):
        self.assertFalse(validate_structured("CREDIT_CARD", ""))
        self.assertFalse(validate_structured("ACCOUNT", "   "))


class MaskPlaceholdersTest(SimpleTestCase):
    """Masking must preserve length (so offsets stay valid) and erase brackets."""

    def test_length_preserved(self):
        for text in (
            "Send it to [EMAIL_ADDRESS_3] tomorrow",
            "call [PERSON_1] and [PERSON_2] about [ACCOUNT_10]",
            r"journal says \[PERSON_444\] verbatim",
            "no placeholders here at all",
            "",
        ):
            with self.subTest(text=text):
                self.assertEqual(len(mask_placeholders(text)), len(text))

    def test_brackets_and_placeholder_text_removed(self):
        masked = mask_placeholders("hi [PERSON_1] there")
        self.assertNotIn("[", masked)
        self.assertNotIn("]", masked)
        self.assertNotIn("PERSON_1", masked)
        # Surrounding real text is untouched.
        self.assertTrue(masked.startswith("hi "))
        self.assertTrue(masked.endswith(" there"))

    def test_escaped_variant_masked(self):
        masked = mask_placeholders(r"see \[EMAIL_ADDRESS_2\] now")
        self.assertNotIn("EMAIL_ADDRESS_2", masked)
        self.assertNotIn("[", masked)
        self.assertNotIn("]", masked)

    def test_offset_of_trailing_word_stable(self):
        # The word after a masked placeholder keeps its exact index — the whole
        # point of same-length filler.
        text = "x [PERSON_1] Chen"
        masked = mask_placeholders(text)
        self.assertEqual(text.index("Chen"), masked.index("Chen"))


class SnapToWordBoundariesTest(SimpleTestCase):
    """Mid-word offsets expand to the full word (the 'amaica' -> 'Jamaica' class)."""

    def test_truncated_start_expands_left(self):
        text = "met Jamaica today"
        # "amaica" — model dropped the leading J.
        start, end = snap_to_word_boundaries(text, 5, 11)
        self.assertEqual(text[start:end], "Jamaica")

    def test_truncated_head_expands_left_to_full_word(self):
        text = "Nottingham Forest"
        # "tingham" — model dropped "Not".
        start, end = snap_to_word_boundaries(text, 3, 10)
        self.assertEqual(text[start:end], "Nottingham")

    def test_truncated_end_expands_right(self):
        text = "I love Jamaica here"
        # "Jamaic" — model dropped the trailing 'a'.
        start, end = snap_to_word_boundaries(text, 7, 13)
        self.assertEqual(text[start:end], "Jamaica")

    def test_already_bounded_is_noop(self):
        text = "Sarah Chen called"
        self.assertEqual(snap_to_word_boundaries(text, 0, 5), (0, 5))  # "Sarah"
        self.assertEqual(snap_to_word_boundaries(text, 6, 10), (6, 10))  # "Chen"

    def test_does_not_cross_whitespace(self):
        text = "met Jamaica"
        start, end = snap_to_word_boundaries(text, 5, 11)  # "amaica"
        # Must not swallow the preceding "met " word.
        self.assertEqual(text[start:end], "Jamaica")

    def test_degenerate_offsets_returned_unchanged(self):
        self.assertEqual(snap_to_word_boundaries("", 0, 0), (0, 0))
        self.assertEqual(snap_to_word_boundaries("abc", 2, 1), (2, 1))
        # Out-of-range clamps rather than raising.
        s, e = snap_to_word_boundaries("abc", 0, 99)
        self.assertEqual((s, e), (0, 3))


class RedactUserMessageHygieneIntegrationTest(TestCase):
    """End-to-end: hygiene drops junk in the detection filter, so nothing mints.

    Stubs ``_detect_pii`` to return junk spans directly (no ONNX model needed),
    exactly as the WordBoundarySubstitutionTests do. Under the default
    ``mint='all'`` policy the mint gate would coin everything — so a clean result
    proves ``_filter_results`` hygiene, not the mint gate, is what suppressed it.
    """

    def setUp(self):
        from apps.tenants.services import create_tenant

        self.tenant = create_tenant(display_name="Test User", telegram_chat_id=909090)

    def _run_with_hits(self, text, hits):
        from apps.pii.redactor import DetectedEntity, redact_user_message

        detected = [DetectedEntity(etype, start, end, score) for etype, start, end, score in hits]
        with patch("apps.pii.redactor._detect_pii", return_value=detected):
            return redact_user_message(text, self.tenant)

    def test_structure_junk_person_not_minted(self):
        text = "### 08:05 Neighbor sync"
        result = self._run_with_hits(text, [("PERSON", 0, len(text), 0.99)])
        self.assertEqual(result, text)
        self.assertNotIn("[PERSON_", result)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map or {}, {})

    def test_unvalidated_credit_card_label_not_minted(self):
        # Neural CREDITCARDISSUER → CREDIT_CARD on the token "django": no Luhn.
        text = "django"
        result = self._run_with_hits(text, [("CREDIT_CARD", 0, 6, 0.97)])
        self.assertEqual(result, text)
        self.assertNotIn("[CREDIT_CARD_", result)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map or {}, {})

    def test_date_as_account_not_minted(self):
        text = "2026-05-30"
        result = self._run_with_hits(text, [("ACCOUNT", 0, len(text), 0.95)])
        self.assertEqual(result, text)
        self.assertNotIn("[ACCOUNT_", result)

    def test_valid_person_still_minted(self):
        # Control: a clean name is NOT junk and NOT gated by validate_structured,
        # so it mints — proving hygiene does not over-suppress real PII.
        text = "met Sarah Chen"
        start = text.index("Sarah")
        result = self._run_with_hits(text, [("PERSON", start, len(text), 0.99)])
        self.assertIn("[PERSON_1]", result)
        self.tenant.refresh_from_db()
        self.assertIn("[PERSON_1]", self.tenant.pii_entity_map)

    def test_valid_credit_card_still_minted(self):
        # Control: a Luhn-valid card passes validate_structured and mints.
        text = "card 4111111111111111 ok"
        start = text.index("4111")
        end = start + 16
        result = self._run_with_hits(text, [("CREDIT_CARD", start, end, 0.99)])
        self.assertIn("[CREDIT_CARD_1]", result)
