"""A retired binding must STOP substituting — the point of retiring one.

Retirement used to be cosmetic on the inbound path: Step 1 of
``_redact_user_message`` replaces known entities BEFORE ``_filter_results`` runs,
so a retired "Calendar" binding kept swapping ``[PERSON_7]`` into the user's text
forever no matter what the stoplist said. These tests pin the four seams that had
to change, and the two that must NOT: rehydration of old text, and placeholder
numbering (a retired number is never reissued).

Detection is stubbed at ``_detect_pii`` so the assertions are about substitution
and mint-reuse, not about model behavior — no ONNX weights needed.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.pii.redactor import (
    MINT_NEVER,
    DetectedEntity,
    RedactionSession,
    redact_known_entities,
    redact_user_message,
    rehydrate_text,
)
from apps.tenants.services import create_tenant

RETIRED_AT = "2026-08-08T00:00:00+00:00"


def _map(*, retired: bool) -> dict:
    entry = {"name": "Calendar"}
    if retired:
        entry = {"name": "Calendar", "retired": True, "retired_at": RETIRED_AT}
    return {"[PERSON_7]": entry}


class RetiredBindingSubstitutionTest(TestCase):
    """Inbound chat ingress: redact_user_message must ignore retired bindings."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Test Owner", telegram_chat_id=910101)

    def _redact(self, text: str, *, retired: bool, hits: list | None = None) -> str:
        self.tenant.pii_entity_map = _map(retired=retired)
        self.tenant.save(update_fields=["pii_entity_map"])
        with patch("apps.pii.redactor._detect_pii", return_value=hits or []):
            return redact_user_message(text, self.tenant)

    def test_active_binding_still_masks_the_name(self):
        # Control. Without this, the retired assertion below could pass for the
        # wrong reason (e.g. Step 1 silently broken for everyone).
        self.assertEqual(self._redact("Check my Calendar", retired=False), "Check my [PERSON_7]")

    def test_retired_binding_leaves_the_name_unmasked(self):
        # THE money assertion: "Check my [PERSON_7]" is what prod does today.
        self.assertEqual(self._redact("Check my Calendar", retired=True), "Check my Calendar")

    def test_retired_binding_is_not_reused_as_a_mint_target(self):
        # A real name whose binding was retired comes back: it must mint a FRESH
        # placeholder rather than resurrecting the tombstone (directive A9).
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Marcus Delgado", "retired": True, "retired_at": RETIRED_AT},
        }
        self.tenant.save(update_fields=["pii_entity_map"])
        text = "Call Marcus Delgado today"
        start = text.index("Marcus Delgado")
        hits = [DetectedEntity("PERSON", start, start + len("Marcus Delgado"), 0.99)]

        with patch("apps.pii.redactor._detect_pii", return_value=hits):
            out = redact_user_message(text, self.tenant)

        self.assertEqual(out, "Call [PERSON_2] today")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map["[PERSON_2]"]["name"], "Marcus Delgado")
        # The tombstone is untouched, and its number was not reissued.
        self.assertTrue(self.tenant.pii_entity_map["[PERSON_1]"]["retired"])

    def test_rehydration_of_old_text_still_resolves_a_retired_placeholder(self):
        # Retire is a tombstone, NOT a delete: messages stored before the retire
        # still carry [PERSON_7] and must keep resolving.
        self.assertEqual(
            rehydrate_text("Ping [PERSON_7] later", _map(retired=True)),
            "Ping Calendar later",
        )


class RetiredBindingKnownOnlyPathsTest(TestCase):
    """The reuse-only seams (mint nothing) must honour retirement too."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Test Owner", telegram_chat_id=910102)

    def test_redact_known_entities_skips_retired(self):
        self.tenant.pii_entity_map = _map(retired=False)
        self.assertEqual(redact_known_entities(self.tenant, "Check my Calendar"), "Check my [PERSON_7]")

        self.tenant.pii_entity_map = _map(retired=True)
        self.assertEqual(redact_known_entities(self.tenant, "Check my Calendar"), "Check my Calendar")

    def test_mint_never_session_skips_retired(self):
        # Workspace memory sync / galaxy co-pilot path.
        self.tenant.pii_entity_map = _map(retired=False)
        self.tenant.save(update_fields=["pii_entity_map"])
        session = RedactionSession(tenant=self.tenant, mint=MINT_NEVER)
        self.assertEqual(session.redact("Check my Calendar"), "Check my [PERSON_7]")

        self.tenant.pii_entity_map = _map(retired=True)
        self.tenant.save(update_fields=["pii_entity_map"])
        retired_session = RedactionSession(tenant=self.tenant, mint=MINT_NEVER)
        self.assertEqual(retired_session.redact("Check my Calendar"), "Check my Calendar")
