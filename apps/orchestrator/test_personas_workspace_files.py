"""Tests for ``render_workspace_files`` composition rules.

These pin the post-refactor contract where behavioral rules move out of
USER.md and into AGENTS.md:

- Gravity-enabled tenants get the Observation Mode + Voice Register
  Selection blocks appended to AGENTS.md.
- Non-Gravity tenants do NOT get those blocks — AGENTS.md stays lean.
- USER.md's ``insights_observation_mode`` section carries only the
  small dynamic counts (NOT the long-form rules).
"""

from __future__ import annotations

from django.test import TestCase

from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant


class RenderWorkspaceFilesObservationModeTest(TestCase):
    def test_finance_enabled_tenant_gets_observation_rules_in_agents_md(self):
        tenant = create_tenant(display_name="Gravity User", telegram_chat_id=900001)
        tenant.finance_enabled = True
        tenant.save(update_fields=["finance_enabled"])

        files = render_workspace_files("neighbor", tenant=tenant)
        agents_md = files["NBHD_AGENTS_MD"]

        # The block heading and signature phrases from the rules must be
        # present in AGENTS.md, not USER.md.
        self.assertIn("## Gravity Observation Mode", agents_md)
        self.assertIn("Voice Register Selection", agents_md)
        self.assertIn("nbhd_insights_signals", agents_md)
        self.assertIn("register_offset", agents_md)

    def test_non_finance_tenant_does_not_get_observation_rules(self):
        tenant = create_tenant(display_name="Plain User", telegram_chat_id=900002)
        # finance_enabled defaults to False; assert it.
        self.assertFalse(getattr(tenant, "finance_enabled", False))

        files = render_workspace_files("neighbor", tenant=tenant)
        agents_md = files["NBHD_AGENTS_MD"]

        self.assertNotIn("Gravity Observation Mode", agents_md)
        self.assertNotIn("Voice Register Selection", agents_md)

    def test_tenant_prompt_extras_still_compose_with_observation_rules(self):
        """Existing prompt_extras append path must not be broken by the new append.

        Both prompt_extras (per-tenant override) and the observation-mode
        rules (gated on finance_enabled) should be present, in that order.
        """
        tenant = create_tenant(display_name="Extras User", telegram_chat_id=900003)
        tenant.finance_enabled = True
        tenant.save(update_fields=["finance_enabled"])
        tenant.user.preferences = {
            "agent_persona": "neighbor",
            "prompt_extras": {"agents_md": "## Per-tenant rule\n\nKeep replies under 80 words."},
        }
        tenant.user.save(update_fields=["preferences"])

        files = render_workspace_files("neighbor", tenant=tenant)
        agents_md = files["NBHD_AGENTS_MD"]

        # Both blocks land. prompt_extras appended before observation rules
        # so the per-tenant overrides come first (closer to the base
        # template), observation rules at the tail.
        extras_pos = agents_md.find("Per-tenant rule")
        observation_pos = agents_md.find("Gravity Observation Mode")
        self.assertGreater(extras_pos, 0)
        self.assertGreater(observation_pos, extras_pos)


class ObservationModeUserMdCountsTest(TestCase):
    """The USER.md ``insights_observation_mode`` section is now tiny.

    Pre-refactor it carried ~6 KB of static prompt text on every turn,
    pushing USER.md past OpenClaw's 12 KB bootstrap budget. Post-refactor
    it's a single-line counts pointer — the rules live in AGENTS.md.
    """

    def test_user_md_section_is_under_one_kb(self):
        from apps.insights.envelope import render_observation_mode

        tenant = create_tenant(display_name="Gravity User", telegram_chat_id=900004)
        tenant.finance_enabled = True
        tenant.save(update_fields=["finance_enabled"])

        text = render_observation_mode(tenant)
        self.assertLess(len(text), 1000)

    def test_user_md_section_does_not_contain_rules_body(self):
        from apps.insights.envelope import render_observation_mode

        tenant = create_tenant(display_name="Gravity User", telegram_chat_id=900005)
        tenant.finance_enabled = True
        tenant.save(update_fields=["finance_enabled"])

        text = render_observation_mode(tenant)
        # Rules-body sentinels (long-form rule sentences) must NOT appear
        # in the USER.md section. Breadcrumb references to AGENTS.md are
        # fine — those just tell the agent where to look.
        self.assertNotIn("Hard floors are mechanical", text)
        self.assertNotIn("Honor any non-zero `user_voice_pref", text)
        self.assertNotIn("Always check trajectory", text)
        self.assertNotIn("Frame as observation, not prescription", text)
