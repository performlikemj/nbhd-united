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


class ProfileDeliveryChannelTests(TestCase):
    """The Profile block must render the RESOLVED delivery channel, never the
    raw ``preferred_channel`` column.

    ``preferred_channel`` is the untouched schema default ("telegram") on
    essentially every prod row, so rendering it asserted "Preferred channel:
    telegram" into the agent-visible context of iOS-only tenants who never
    linked Telegram — the exact falsehood the channel-identity fix kills.
    """

    def _tenant(self, *, suffix: int):
        t = create_tenant(display_name=f"Env-{suffix}", telegram_chat_id=990000 + suffix)
        # Every row carries the schema default regardless of what's linked.
        self.assertEqual(t.user.preferred_channel, "telegram")
        return t

    def test_ios_only_tenant_never_says_telegram(self):
        from apps.router.models import DeviceToken
        from apps.tenants.envelope import render_profile

        t = self._tenant(suffix=1)
        t.user.telegram_chat_id = None
        t.user.line_user_id = ""
        t.user.save()
        DeviceToken.objects.create(
            tenant=t, user=t.user, token="e" * 64, environment=DeviceToken.Environment.PRODUCTION
        )

        body = render_profile(t)
        self.assertIn("- Delivery channel: NBHD app", body)
        self.assertNotIn("Telegram", body)
        self.assertNotIn("Preferred channel", body)

    def test_telegram_linked_tenant_still_reads_telegram(self):
        from apps.tenants.envelope import render_profile

        t = self._tenant(suffix=2)  # telegram_chat_id set by create_tenant
        body = render_profile(t)
        self.assertIn("- Delivery channel: Telegram", body)

    def test_line_linked_tenant_reads_line(self):
        from apps.tenants.envelope import render_profile

        t = self._tenant(suffix=3)
        t.user.telegram_chat_id = None
        t.user.line_user_id = "U" + "f" * 32
        t.user.save()

        body = render_profile(t)
        self.assertIn("- Delivery channel: LINE", body)
        self.assertNotIn("Telegram", body)

    def test_no_delivery_surface_omits_the_line_entirely(self):
        # Printing nothing beats printing a falsehood.
        from apps.tenants.envelope import render_profile

        t = self._tenant(suffix=4)
        t.user.telegram_chat_id = None
        t.user.line_user_id = ""
        t.user.save()

        body = render_profile(t)
        self.assertNotIn("Delivery channel", body)
        self.assertNotIn("Telegram", body)

    def test_ios_only_managed_region_asserts_no_telegram(self):
        """The end-to-end symptom: the AGENT-VISIBLE USER.md region an iOS-only
        tenant's assistant reads must not claim Telegram anywhere."""
        from apps.orchestrator.workspace_envelope import render_managed_region
        from apps.router.models import DeviceToken

        t = self._tenant(suffix=5)
        t.user.telegram_chat_id = None
        t.user.line_user_id = ""
        t.user.save()
        DeviceToken.objects.create(
            tenant=t, user=t.user, token="c" * 64, environment=DeviceToken.Environment.PRODUCTION
        )
        t.refresh_from_db()

        region = render_managed_region(t)
        self.assertIn("- Delivery channel: NBHD app", region)
        self.assertNotIn("telegram", region.lower())
