"""Regression locks for the "Rakuten" -> "Rocketen" incident.

Forensics established the garble was created by *speech-to-text* — Apple's
on-device recognizer in the iOS app misheard the brand (iOS voice never touches
server-side Whisper; the chat ingress accepts text only) — and stored verbatim;
the PII layer only mirrored the misheard surface form into
``Tenant.pii_entity_map`` as ``[PERSON_526]`` and replays it faithfully.
Because redaction/rehydration match on the EXACT canonical key
(``casefold().strip()``), a correctly-spelled "Rakuten" can never collide onto
the "rocketen" placeholder — so the map is not a corruption source and the fix
is data repair, not redactor code.

These tests pin that behaviour so a future "fuzzy match" refactor (the tempting
but wrong hypothesis) can't silently reintroduce cross-contamination, and so the
denylist safeguard keeps a cleared garble from re-entering the mint loop. They
patch ``_detect_pii`` to [] so only the deterministic known-entity pass runs —
no ONNX model required.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.pii.redactor import redact_user_message, rehydrate_text
from apps.tenants.services import create_tenant

# Mixed storage shapes on purpose: the legacy string entry ("Rakuten") and the
# newer dict entry (the mis-minted garble) coexist in MJ's real map.
_MAP = {
    "[CREDIT_CARD_14]": "Rakuten",  # legit brand (NER mislabelled type)
    "[PERSON_526]": {"name": "rocketen"},  # transcription garble, frozen by NER
}


class RehydrationFaithfulReplayTests(SimpleTestCase):
    def test_placeholder_replays_its_own_stored_surface_form(self):
        # The garble only ever appears because it is *stored*; rehydration is a
        # faithful 1:1 replay, never a transform.
        text = "meeting at [CREDIT_CARD_14], not [PERSON_526]"
        self.assertEqual(rehydrate_text(text, _MAP), "meeting at Rakuten, not rocketen")

    def test_distinct_placeholders_stay_distinct(self):
        self.assertEqual(rehydrate_text("[CREDIT_CARD_14]", _MAP), "Rakuten")
        self.assertEqual(rehydrate_text("[PERSON_526]", _MAP), "rocketen")


class ExactKeyMatchingRulesOutCrossContaminationTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Michael Jones", telegram_chat_id=910001)
        self.tenant.pii_entity_map = dict(_MAP)
        self.tenant.save(update_fields=["pii_entity_map"])

    def test_correct_brand_never_maps_onto_garble_placeholder(self):
        # A correctly-spelled "Rakuten" must collapse onto its OWN placeholder
        # ([CREDIT_CARD_14]), never the phonetic-neighbour garble ([PERSON_526]).
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            out = redact_user_message("I got a refund from Rakuten today", self.tenant)

        self.assertIn("[CREDIT_CARD_14]", out)
        self.assertNotIn("[PERSON_526]", out)
        # And the round-trip restores the correct brand, not the garble.
        self.assertEqual(
            rehydrate_text(out, self.tenant.pii_entity_map),
            "I got a refund from Rakuten today",
        )
        self.assertNotIn("ocketen", rehydrate_text(out, self.tenant.pii_entity_map))

    def test_denylisted_garble_is_not_re_redacted(self):
        # Once the arbiter denylists "rocketen", the existing safeguard must stop
        # it driving redaction, so it can't re-enter the freeze-and-replay loop.
        self.tenant.pii_denylist = {"rocketen": {"reason": "arbiter"}}
        self.tenant.save(update_fields=["pii_denylist"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            out = redact_user_message("we discussed rocketen today", self.tenant)

        self.assertNotIn("[PERSON_526]", out)
        self.assertIn("rocketen", out)
