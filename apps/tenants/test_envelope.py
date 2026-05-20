"""Tests for the tenants/envelope.py sections.

Covers the privacy_placeholders section — gated on ``Tenant.pii_entity_map``
being non-empty. The Profile section is exercised indirectly via
``test_cron_envelope.RenderManagedRegionTest``.
"""

from __future__ import annotations

from django.test import TestCase

from apps.tenants.envelope import render_privacy_placeholders
from apps.tenants.services import create_tenant


class PrivacyPlaceholdersSectionTests(TestCase):
    _next_chat_id = 8000_0000

    def _tenant(self, entity_map: dict | None = None):
        type(self)._next_chat_id += 1
        tenant = create_tenant(
            display_name="Test User",
            telegram_chat_id=type(self)._next_chat_id,
        )
        if entity_map is not None:
            tenant.pii_entity_map = entity_map
            tenant.save(update_fields=["pii_entity_map"])
        return tenant

    def test_render_returns_rule_body_unconditionally(self):
        # The render() function itself is unconditional — gating happens
        # via the `enabled` predicate in render_managed_region().
        tenant = self._tenant(entity_map={"[PERSON_1]": "Sarah Chen"})
        body = render_privacy_placeholders(tenant)
        self.assertIn("[PERSON_1]", body)
        self.assertIn("Preserve placeholders exactly as written", body)

    def test_section_appears_in_managed_region_when_entity_map_populated(self):
        from apps.orchestrator.workspace_envelope import render_managed_region

        tenant = self._tenant(entity_map={"[PERSON_1]": "Sarah Chen"})
        rendered = render_managed_region(tenant)
        self.assertIn("## Privacy Placeholders", rendered)
        self.assertIn("[PERSON_1]", rendered)

    def test_section_absent_when_entity_map_empty(self):
        from apps.orchestrator.workspace_envelope import render_managed_region

        tenant = self._tenant(entity_map={})
        rendered = render_managed_region(tenant)
        self.assertNotIn("## Privacy Placeholders", rendered)

    def test_section_absent_when_entity_map_null(self):
        from apps.orchestrator.workspace_envelope import render_managed_region

        tenant = self._tenant(entity_map=None)
        rendered = render_managed_region(tenant)
        self.assertNotIn("## Privacy Placeholders", rendered)

    def test_section_registered_with_expected_metadata(self):
        from apps.orchestrator.envelope_registry import all_sections

        section = next((s for s in all_sections() if s.key == "privacy_placeholders"), None)
        self.assertIsNotNone(
            section,
            "privacy_placeholders section was not registered — check apps.tenants.apps.ready()",
        )
        self.assertEqual(section.heading, "## Privacy Placeholders")
        self.assertEqual(section.order, 70)
