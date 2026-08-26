"""Tests for the tenants/envelope.py sections.

Covers the privacy_placeholders section — gated on ``Tenant.pii_entity_map``
being non-empty. The Profile section is exercised indirectly via
``test_cron_envelope.RenderManagedRegionTest``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.pii.redactor import RedactionOutcome
from apps.tenants.envelope import render_privacy_placeholders, render_safe_user_md, render_situation
from apps.tenants.models import UserSituation
from apps.tenants.services import create_tenant


class SafeUserMdRenderTests(TestCase):
    def _tenant(self):
        tenant = create_tenant(display_name="Alex Rivera", telegram_chat_id=789999)
        tenant.user.location_city = "Yokohama"
        tenant.user.preferences = {"onboarding_interests": "Plan training with Priya Nair"}
        tenant.user.save(update_fields=["location_city", "preferences"])
        tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Alex Rivera"},
            "[LOCATION_1]": {"name": "Yokohama"},
            "[PERSON_2]": {"name": "Priya Nair"},
        }
        tenant.save(update_fields=["pii_entity_map"])
        return tenant

    def test_output_contains_interests_but_no_raw_profile_values(self):
        tenant = self._tenant()

        content = render_safe_user_md(tenant)

        self.assertIsNotNone(content)
        self.assertIn("Interests and priorities", content)
        self.assertIn("[PERSON_1", content)
        self.assertIn("[LOCATION_1", content)
        self.assertIn("[PERSON_2", content)
        self.assertNotIn("Alex Rivera", content)
        self.assertNotIn("Yokohama", content)
        self.assertNotIn("Priya Nair", content)

    @patch("apps.pii.redactor.redact_user_message_checked")
    def test_first_pass_uses_minting_policy_and_failure_skips_render(self, redact):
        tenant = self._tenant()
        redact.return_value = RedactionOutcome(text="raw", confirmed=False, reason="redaction-error")

        with patch("apps.orchestrator.workspace_envelope.render_managed_region") as render:
            content = render_safe_user_md(tenant)

        self.assertIsNone(content)
        render.assert_not_called()
        self.assertNotIn("mint", redact.call_args.kwargs)
        self.assertFalse(redact.call_args.kwargs["allow_user_name"])


class RightNowSectionTests(TestCase):
    _next_chat_id = 7900_0000

    def _tenant(self, *, enabled=True, home="Tokyo", profile_tz="Asia/Tokyo"):
        type(self)._next_chat_id += 1
        tenant = create_tenant(
            display_name="Situation Test",
            telegram_chat_id=type(self)._next_chat_id,
        )
        tenant.situational_context_enabled = enabled
        tenant.save(update_fields=["situational_context_enabled"])
        tenant.user.location_city = home
        tenant.user.timezone = profile_tz
        tenant.user.save(update_fields=["location_city", "timezone"])
        return tenant

    def _situation(self, tenant, **overrides):
        now = timezone.now().replace(microsecond=0)
        defaults = {
            "current_place_label": "Fukuoka",
            "current_place_since": now,
            "current_place_last_observed_at": now,
            "current_place_source": "ios_chat",
        }
        defaults.update(overrides)
        return UserSituation.objects.create(tenant=tenant, **defaults)

    def test_fresh_place_renders_with_local_as_of_and_home_base(self):
        tenant = self._tenant()
        self._situation(tenant)
        with self.assertLogs("apps.tenants.envelope", level="INFO") as logs:
            body = render_situation(tenant)
        self.assertIn("Current location: Fukuoka", body)
        self.assertIn("today; home base Tokyo", body)
        self.assertIn("_Use for day-shaping context:", body)
        self.assertIn("Don't bring it up outside that; never share it outward._", body)
        self.assertIn("situation_rendered", "\n".join(logs.output))
        self.assertIn("fresh=1 traveling=1", "\n".join(logs.output))

    def test_stale_away_place_inside_window_renders_reconfirmation_nudge_and_logs_decay(self):
        tenant = self._tenant()
        old = timezone.now() - timedelta(hours=49)
        self._situation(tenant, current_place_since=old, current_place_last_observed_at=old)
        with self.assertLogs("apps.tenants.envelope", level="INFO") as logs:
            body = render_situation(tenant)
        self.assertIn("Last known location: Fukuoka (stale, from", body)
        self.assertIn("record it with nbhd_update_situation", body)
        self.assertIn("confirm casually", body)
        self.assertIn("situation_decayed", "\n".join(logs.output))

    def test_stale_away_place_beyond_window_is_omitted(self):
        tenant = self._tenant()
        old = timezone.now() - timedelta(days=15)
        self._situation(tenant, current_place_since=old, current_place_last_observed_at=old)
        self.assertEqual(render_situation(tenant), "")

    def test_stale_home_place_is_omitted(self):
        tenant = self._tenant()
        old = timezone.now() - timedelta(hours=49)
        self._situation(
            tenant,
            current_place_label="Tokyo",
            current_place_since=old,
            current_place_last_observed_at=old,
        )
        self.assertEqual(render_situation(tenant), "")

    def test_no_situation_row_is_omitted(self):
        tenant = self._tenant()
        self.assertEqual(render_situation(tenant), "")

    def test_fresh_differing_device_timezone_renders(self):
        tenant = self._tenant(profile_tz="Asia/Tokyo")
        now = timezone.now()
        self._situation(
            tenant,
            current_place_label="",
            current_place_since=None,
            current_place_last_observed_at=None,
            device_tz="America/New_York",
            device_tz_since=now,
            device_tz_last_observed_at=now,
            device_tz_source_device="healthkit",
        )
        self.assertEqual(
            render_situation(tenant),
            "Device timezone: America/New_York (profile: Asia/Tokyo).",
        )

    def test_same_or_stale_device_timezone_is_omitted(self):
        tenant = self._tenant(profile_tz="Asia/Tokyo")
        now = timezone.now()
        situation = self._situation(
            tenant,
            current_place_label="",
            current_place_since=None,
            current_place_last_observed_at=None,
            device_tz="Asia/Tokyo",
            device_tz_since=now,
            device_tz_last_observed_at=now,
        )
        self.assertEqual(render_situation(tenant), "")
        situation.device_tz = "America/New_York"
        situation.device_tz_last_observed_at = now - timedelta(days=8)
        situation.save(update_fields=["device_tz", "device_tz_last_observed_at", "updated_at"])
        self.assertEqual(render_situation(tenant), "")

    def test_away_nudge_appears_at_five_days_when_place_is_fresh(self):
        tenant = self._tenant()
        self._situation(
            tenant,
            current_place_since=timezone.now() - timedelta(days=5, minutes=1),
            current_place_last_observed_at=timezone.now(),
        )
        self.assertIn("(Away 5+ days", render_situation(tenant))

    def test_away_nudge_is_omitted_when_fresh_place_is_home(self):
        tenant = self._tenant()
        self._situation(
            tenant,
            current_place_label="Tokyo",
            current_place_since=timezone.now() - timedelta(days=5, minutes=1),
            current_place_last_observed_at=timezone.now(),
        )
        self.assertNotIn("Away 5+ days", render_situation(tenant))

    def test_hard_cap_drops_nudge_before_purpose_for_representative_full_section(self):
        tenant = self._tenant(
            home="Llanfairpwllgwyngyll, Isle of Anglesey, Wales",
            profile_tz="America/Argentina/Buenos_Aires",
        )
        now = timezone.now()
        self._situation(
            tenant,
            current_place_label="San Fernando del Valle de Catamarca, Argentina",
            current_place_since=now - timedelta(days=6),
            current_place_last_observed_at=now,
            device_tz="America/Indiana/Indianapolis",
            device_tz_since=now,
            device_tz_last_observed_at=now,
        )
        body = render_situation(tenant)
        self.assertLessEqual(len(body), 400)
        self.assertNotIn("Away 5+ days", body)
        self.assertIn("Device timezone", body)
        self.assertIn("Current location", body)
        self.assertIn("_Use for day-shaping context:", body)

    def test_hard_cap_drops_purpose_last_before_truncating_required_place(self):
        tenant = self._tenant(home="H" * 255, profile_tz="America/Argentina/Buenos_Aires")
        now = timezone.now()
        self._situation(
            tenant,
            current_place_label="P" * 64,
            current_place_since=now - timedelta(days=6),
            current_place_last_observed_at=now,
            device_tz="America/Indiana/Indianapolis",
            device_tz_since=now,
            device_tz_last_observed_at=now,
        )
        body = render_situation(tenant)
        self.assertLessEqual(len(body), 400)
        self.assertNotIn("Away 5+ days", body)
        self.assertNotIn("Device timezone", body)
        self.assertNotIn("Use for day-shaping context", body)
        self.assertIn("Current location", body)

    def test_flag_off_and_eval_sink_omit_registered_section(self):
        from apps.orchestrator.workspace_envelope import render_managed_region

        tenant = self._tenant(enabled=False)
        self._situation(tenant)
        self.assertNotIn("## Right now", render_managed_region(tenant))

        tenant.situational_context_enabled = True
        tenant.is_eval_sink = True
        self.assertNotIn("## Right now", render_managed_region(tenant))

    def test_section_registration_uses_unique_order_and_refresh_model(self):
        from apps.orchestrator.envelope_registry import all_sections

        sections = all_sections()
        section = next(section for section in sections if section.key == "right_now")
        self.assertEqual(section.heading, "## Right now")
        self.assertEqual(section.order, 13)
        self.assertEqual(section.refresh_on, (UserSituation,))
        self.assertEqual(sum(1 for other in sections if other.order == 13), 1)


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
        self.assertIn("|unresolved", body)
        self.assertIn("Never claim familiarity", body)

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

    def test_subsection_repseudonymizes_known_names_in_metadata(self):
        tenant = self._tenant(
            {
                "[PERSON_1]": {
                    "name": "Theo",
                    "relationship": "recruiter at Optiver",
                },
                "[ORG_2]": {"name": "Optiver"},
            }
        )

        body = render_privacy_placeholders(tenant)

        self.assertIn("`[PERSON_1]` — recruiter at ORG_2", body)
        self.assertNotIn("Theo", body)
        self.assertNotIn("Optiver", body)

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
