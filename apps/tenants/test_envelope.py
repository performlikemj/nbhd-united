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
        # order=12 — directly after Profile (10). The placeholder legend
        # is load-bearing for redaction round-trips; if USER.md ever gets
        # truncated again, the legend MUST survive (was order=70 before
        # the 2026-05-22 USER.md shrink refactor, got silently cut).
        self.assertLess(section.order, 20, "Privacy Placeholders must sort near the top of USER.md")


class IdentityContextSubSectionTests(TestCase):
    """The ``### Identity context`` sub-section appears inside the
    ``## Privacy Placeholders`` body when entries carry user-curated
    ``relationship`` or ``notes`` metadata (the new dict shape from
    apps.pii.entity_registry). Legacy string-only entries contribute
    nothing here.
    """

    _next_chat_id = 8200_0000

    def _tenant(self, entity_map):
        type(self)._next_chat_id += 1
        tenant = create_tenant(
            display_name="Test User",
            telegram_chat_id=type(self)._next_chat_id,
        )
        tenant.pii_entity_map = entity_map
        tenant.save(update_fields=["pii_entity_map"])
        return tenant

    def test_no_subsection_when_all_entries_are_legacy_strings(self):
        tenant = self._tenant({"[PERSON_1]": "Sarah", "[PERSON_2]": "Bob"})
        body = render_privacy_placeholders(tenant)
        self.assertNotIn("### Identity context", body)

    def test_no_subsection_when_dict_entries_have_only_name(self):
        tenant = self._tenant({"[PERSON_1]": {"name": "Sarah"}})
        body = render_privacy_placeholders(tenant)
        self.assertNotIn("### Identity context", body)

    def test_subsection_appears_when_relationship_present(self):
        tenant = self._tenant(
            {
                "[PERSON_1]": {"name": "Sarah", "relationship": "daughter"},
            }
        )
        body = render_privacy_placeholders(tenant)
        self.assertIn("### Identity context", body)
        self.assertIn("`[PERSON_1]` — daughter", body)
        # Real name MUST NOT leak into the prompt
        self.assertNotIn("Sarah", body)

    def test_subsection_appears_when_notes_present_without_relationship(self):
        tenant = self._tenant(
            {
                "[PERSON_1]": {"name": "Sarah", "notes": "writes haiku"},
            }
        )
        body = render_privacy_placeholders(tenant)
        self.assertIn("`[PERSON_1]` — writes haiku", body)
        self.assertNotIn("Sarah", body)

    def test_subsection_combines_relationship_and_notes_with_em_dash(self):
        tenant = self._tenant(
            {
                "[PERSON_1]": {
                    "name": "Sarah",
                    "relationship": "daughter",
                    "notes": "4.5 years old, into Roblox",
                },
            }
        )
        body = render_privacy_placeholders(tenant)
        self.assertIn("`[PERSON_1]` — daughter — 4.5 years old, into Roblox", body)
        self.assertNotIn("Sarah", body)

    def test_subsection_sorts_entries_by_placeholder_for_stable_diff(self):
        tenant = self._tenant(
            {
                "[PERSON_3]": {"name": "C", "relationship": "coworker"},
                "[PERSON_1]": {"name": "A", "relationship": "daughter"},
                "[PERSON_2]": {"name": "B", "relationship": "spouse"},
            }
        )
        body = render_privacy_placeholders(tenant)
        p1 = body.index("[PERSON_1]")
        p2 = body.index("[PERSON_2]")
        p3 = body.index("[PERSON_3]")
        self.assertLess(p1, p2)
        self.assertLess(p2, p3)

    def test_subsection_skips_entries_without_metadata_in_mixed_map(self):
        tenant = self._tenant(
            {
                "[PERSON_1]": "LegacyOnly",  # legacy, contributes nothing
                "[PERSON_2]": {"name": "Bob"},  # dict but no metadata
                "[PERSON_3]": {"name": "Carol", "relationship": "manager"},
            }
        )
        body = render_privacy_placeholders(tenant)
        self.assertIn("### Identity context", body)
        self.assertNotIn("[PERSON_1]` —", body)
        self.assertNotIn("[PERSON_2]` —", body)
        self.assertIn("`[PERSON_3]` — manager", body)
        # No real names leak
        self.assertNotIn("LegacyOnly", body)
        self.assertNotIn("Carol", body)

    def test_rule_body_still_present_alongside_identity_context(self):
        tenant = self._tenant({"[PERSON_1]": {"name": "X", "relationship": "spouse"}})
        body = render_privacy_placeholders(tenant)
        self.assertIn("Preserve placeholders exactly as written", body)
        self.assertIn("### Identity context", body)
