"""Tests for per-tenant prompt extras hook in render_workspace_files.

Covers the canary-scoped prompt-override path: extras stored in
``User.preferences['prompt_extras'][<section>]`` get concatenated to the
relevant workspace file (e.g. NBHD_AGENTS_MD). Other tenants are unaffected.
"""

from __future__ import annotations

import io

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.orchestrator.personas import (
    _get_tenant_prompt_extras,
    render_workspace_files,
)
from apps.tenants.services import create_tenant


class TenantPromptExtrasTest(TestCase):
    """_get_tenant_prompt_extras returns only well-formed string values."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Canary", telegram_chat_id=900001)

    def test_returns_empty_when_tenant_is_none(self):
        self.assertEqual(_get_tenant_prompt_extras(None, "agents_md"), "")

    def test_returns_empty_when_no_preferences(self):
        self.tenant.user.preferences = {}
        self.tenant.user.save(update_fields=["preferences"])
        self.assertEqual(_get_tenant_prompt_extras(self.tenant, "agents_md"), "")

    def test_returns_empty_when_no_prompt_extras_key(self):
        self.tenant.user.preferences = {"unrelated": "value"}
        self.tenant.user.save(update_fields=["preferences"])
        self.assertEqual(_get_tenant_prompt_extras(self.tenant, "agents_md"), "")

    def test_returns_string_value_stripped(self):
        self.tenant.user.preferences = {"prompt_extras": {"agents_md": "  rule text  \n"}}
        self.tenant.user.save(update_fields=["preferences"])
        self.assertEqual(_get_tenant_prompt_extras(self.tenant, "agents_md"), "rule text")

    def test_returns_empty_for_other_section(self):
        self.tenant.user.preferences = {"prompt_extras": {"agents_md": "rule"}}
        self.tenant.user.save(update_fields=["preferences"])
        self.assertEqual(_get_tenant_prompt_extras(self.tenant, "some_other_section"), "")

    def test_ignores_non_dict_prompt_extras(self):
        self.tenant.user.preferences = {"prompt_extras": "not-a-dict"}
        self.tenant.user.save(update_fields=["preferences"])
        self.assertEqual(_get_tenant_prompt_extras(self.tenant, "agents_md"), "")

    def test_ignores_non_string_value(self):
        self.tenant.user.preferences = {"prompt_extras": {"agents_md": 42}}
        self.tenant.user.save(update_fields=["preferences"])
        self.assertEqual(_get_tenant_prompt_extras(self.tenant, "agents_md"), "")


class RenderWorkspaceFilesExtrasTest(TestCase):
    """render_workspace_files appends extras only when configured."""

    def test_no_extras_returns_base_agents_md(self):
        tenant = create_tenant(display_name="Baseline", telegram_chat_id=900010)
        files = render_workspace_files("neighbor", tenant=tenant)
        base = files["NBHD_AGENTS_MD"]
        self.assertNotIn("CANARY_RULE_TOKEN", base)

    def test_extras_appended_to_agents_md(self):
        tenant = create_tenant(display_name="Canary", telegram_chat_id=900011)
        tenant.user.preferences = {"prompt_extras": {"agents_md": "CANARY_RULE_TOKEN: do the thing."}}
        tenant.user.save(update_fields=["preferences"])

        files = render_workspace_files("neighbor", tenant=tenant)
        agents_md = files["NBHD_AGENTS_MD"]

        self.assertIn("CANARY_RULE_TOKEN: do the thing.", agents_md)
        # Appended, not replacing — base content must still be present.
        self.assertTrue(len(agents_md) > len("CANARY_RULE_TOKEN: do the thing."))
        # Separation: double newline between base and extras.
        self.assertIn("\n\nCANARY_RULE_TOKEN", agents_md)

    def test_extras_scoped_per_tenant(self):
        canary = create_tenant(display_name="Canary", telegram_chat_id=900020)
        other = create_tenant(display_name="Other", telegram_chat_id=900021)

        canary.user.preferences = {"prompt_extras": {"agents_md": "CANARY_ONLY"}}
        canary.user.save(update_fields=["preferences"])

        canary_files = render_workspace_files("neighbor", tenant=canary)
        other_files = render_workspace_files("neighbor", tenant=other)

        self.assertIn("CANARY_ONLY", canary_files["NBHD_AGENTS_MD"])
        self.assertNotIn("CANARY_ONLY", other_files["NBHD_AGENTS_MD"])


class SetPromptExtrasCommandTest(TestCase):
    """set_prompt_extras management command round-trip."""

    def setUp(self):
        self.tenant = create_tenant(display_name="CanaryCmd", telegram_chat_id=900030)

    def _call(self, *args, **kwargs):
        out = io.StringIO()
        call_command("set_prompt_extras", *args, stdout=out, **kwargs)
        return out.getvalue()

    def test_set_via_stdin(self):
        # Simulate stdin with a pre-seeded buffer on sys.stdin.
        import sys

        original_stdin = sys.stdin
        sys.stdin = io.StringIO("rule text from stdin")
        try:
            self._call(
                "--tenant-id",
                str(self.tenant.id),
                "--section",
                "agents_md",
                "--stdin",
            )
        finally:
            sys.stdin = original_stdin

        self.tenant.user.refresh_from_db()
        self.assertEqual(
            self.tenant.user.preferences["prompt_extras"]["agents_md"],
            "rule text from stdin",
        )

    def test_clear_removes_section(self):
        self.tenant.user.preferences = {"prompt_extras": {"agents_md": "existing rule"}}
        self.tenant.user.save(update_fields=["preferences"])

        self._call(
            "--tenant-id",
            str(self.tenant.id),
            "--section",
            "agents_md",
            "--clear",
        )

        self.tenant.user.refresh_from_db()
        # When the last section is cleared, prompt_extras key is also removed.
        self.assertNotIn("prompt_extras", self.tenant.user.preferences)

    def test_unknown_tenant_raises(self):
        with self.assertRaises(CommandError):
            self._call(
                "--tenant-id",
                "00000000-0000-0000-0000-000000000000",
                "--section",
                "agents_md",
                "--clear",
            )

    def test_empty_value_refused(self):
        import sys

        original_stdin = sys.stdin
        sys.stdin = io.StringIO("   \n  ")
        try:
            with self.assertRaises(CommandError):
                self._call(
                    "--tenant-id",
                    str(self.tenant.id),
                    "--section",
                    "agents_md",
                    "--stdin",
                )
        finally:
            sys.stdin = original_stdin
