"""Regression coverage for relationship-aware model placeholder context."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.pii.redactor import DetectedEntity, RedactionSession, redact_user_message, rehydrate_text
from apps.tenants.services import create_tenant


class AnnotatedModelContextTests(TestCase):
    _next_chat_id = 91_000_000

    def setUp(self):
        self.model_patch = patch("apps.pii.engine.get_pii_pipeline", return_value=lambda _text: [])
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)

    def _tenant(self, entity_map):
        type(self)._next_chat_id += 1
        tenant = create_tenant(display_name="Owner", telegram_chat_id=type(self)._next_chat_id)
        tenant.pii_entity_map = entity_map
        tenant.save(update_fields=["pii_entity_map"])
        return tenant

    def test_known_relationship_is_emitted_in_placeholder_space_only(self):
        tenant = self._tenant(
            {
                "[PERSON_559]": {
                    "name": "Theo",
                    "relationship": "recruiter at Optiver",
                },
                "[ORG_558]": {"name": "Optiver"},
            }
        )

        result = RedactionSession(tenant=tenant, mint="never", annotate=True).redact("Theo called")

        self.assertEqual(result, "[PERSON_559|recruiter at ORG_558] called")
        self.assertNotIn("Theo", result)
        self.assertNotIn("Optiver", result)

    def test_entity_without_relationship_is_explicitly_unresolved(self):
        tenant = self._tenant({"[PERSON_412]": {"name": "Bob"}})

        result = RedactionSession(tenant=tenant, mint="never", annotate=True).redact("Bob called")

        self.assertEqual(result, "[PERSON_412|unresolved] called")
        self.assertNotIn("Bob", result)

    def test_newly_minted_entity_is_unresolved_in_model_bound_text(self):
        tenant = self._tenant({})
        detected = [DetectedEntity("PERSON", 0, 5, 0.99)]

        with patch("apps.pii.redactor._detect_pii", return_value=detected):
            redacted = redact_user_message("Alice called", tenant)

        from apps.pii.redactor import annotate_model_context

        result = annotate_model_context(redacted, tenant.pii_entity_map)

        self.assertRegex(result, r"^\[PERSON_\d+\|unresolved\] called$")
        self.assertNotIn("Alice", result)

    def test_annotated_form_rehydrates_by_canonical_placeholder(self):
        entity_map = {"[PERSON_559]": {"name": "Theo", "relationship": "recruiter"}}

        self.assertEqual(
            rehydrate_text("Ask [PERSON_559|recruiter]", entity_map),
            "Ask Theo",
        )
