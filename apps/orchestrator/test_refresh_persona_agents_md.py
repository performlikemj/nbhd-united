"""Tests for ``refresh_persona_agents_md`` management command.

The command re-renders ``workspace/AGENTS.md`` from the latest template +
per-tenant persona + prompt extras, then pushes it to each tenant's
Azure File Share. It's the non-lazy counterpart to the
``apply_pending_configs`` cron path — used for fleet rollouts that need
the new template instructions live *now*, not after the next idle cycle.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_tenant(*, suffix: int, status=Tenant.Status.ACTIVE, hibernated: bool = False):
    tenant = create_tenant(display_name=f"RefreshTest-{suffix}", telegram_chat_id=950000 + suffix)
    tenant.status = status
    tenant.container_id = f"oc-refresh-{suffix}"
    tenant.container_fqdn = f"oc-refresh-{suffix}.internal"
    if hibernated:
        tenant.hibernated_at = timezone.now()
    tenant.save()
    return tenant


@override_settings(AZURE_MOCK="true")
class RefreshPersonaAgentsMdTest(TestCase):
    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_refreshes_each_active_tenant(self, mock_upload):
        """Every active tenant gets exactly one workspace upload."""
        t1 = _make_tenant(suffix=1)
        t2 = _make_tenant(suffix=2)

        call_command("refresh_persona_agents_md")

        self.assertEqual(mock_upload.call_count, 2)
        # First positional arg is tenant_id; second is the file path.
        called_ids = {call.args[0] for call in mock_upload.call_args_list}
        self.assertEqual(called_ids, {str(t1.id), str(t2.id)})
        # All calls target workspace/AGENTS.md.
        for call in mock_upload.call_args_list:
            self.assertEqual(call.args[1], "workspace/AGENTS.md")

    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_uploads_non_empty_content(self, mock_upload):
        """Render must produce content; we verify the upload payload is non-trivial."""
        _make_tenant(suffix=3)

        call_command("refresh_persona_agents_md")

        self.assertEqual(mock_upload.call_count, 1)
        rendered = mock_upload.call_args.args[2]
        self.assertTrue(rendered.strip(), "AGENTS.md must not be empty")
        # Sanity-check the rendered content includes content from the
        # bundled template's persona-personality block.
        self.assertIn("NBHD United", rendered)

    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_skips_hibernated_by_default(self, mock_upload):
        active = _make_tenant(suffix=4)
        _make_tenant(suffix=5, hibernated=True)

        call_command("refresh_persona_agents_md")

        self.assertEqual(mock_upload.call_count, 1)
        self.assertEqual(mock_upload.call_args.args[0], str(active.id))

    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_include_hibernated_covers_them(self, mock_upload):
        active = _make_tenant(suffix=6)
        hibernated = _make_tenant(suffix=7, hibernated=True)

        call_command("refresh_persona_agents_md", "--include-hibernated")

        self.assertEqual(mock_upload.call_count, 2)
        called_ids = {call.args[0] for call in mock_upload.call_args_list}
        self.assertEqual(called_ids, {str(active.id), str(hibernated.id)})

    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_skips_non_active_tenants(self, mock_upload):
        active = _make_tenant(suffix=8)
        _make_tenant(suffix=9, status=Tenant.Status.SUSPENDED)
        _make_tenant(suffix=10, status=Tenant.Status.PENDING)

        call_command("refresh_persona_agents_md")

        self.assertEqual(mock_upload.call_count, 1)
        self.assertEqual(mock_upload.call_args.args[0], str(active.id))

    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_dry_run_makes_no_uploads(self, mock_upload):
        _make_tenant(suffix=11)

        out = StringIO()
        call_command("refresh_persona_agents_md", "--dry-run", stdout=out)

        mock_upload.assert_not_called()
        self.assertIn("DRY RUN", out.getvalue())

    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_single_tenant_target(self, mock_upload):
        _make_tenant(suffix=12)
        target = _make_tenant(suffix=13)

        call_command("refresh_persona_agents_md", "--tenant", str(target.id))

        self.assertEqual(mock_upload.call_count, 1)
        self.assertEqual(mock_upload.call_args.args[0], str(target.id))

    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_persona_extras_are_preserved_on_refresh(self, mock_upload):
        """Tenant prompt_extras['agents_md'] are spliced into the rendered output.

        Refresh uses ``render_workspace_files`` which respects
        ``user.preferences['prompt_extras']``. Pinning this guards against
        regression where someone replaces the render path with a flat
        template read that would silently clobber per-tenant additions.
        """
        tenant = _make_tenant(suffix=14)
        user = tenant.user
        user.preferences = {
            "agent_persona": "neighbor",
            "prompt_extras": {"agents_md": "## Custom Tenant Section\n\nKeep responses under 100 words."},
        }
        user.save(update_fields=["preferences"])

        call_command("refresh_persona_agents_md")

        rendered = mock_upload.call_args.args[2]
        self.assertIn("Custom Tenant Section", rendered)
        self.assertIn("Keep responses under 100 words.", rendered)

    @patch("apps.orchestrator.management.commands.refresh_persona_agents_md.upload_workspace_file")
    def test_failure_for_one_tenant_surfaces_command_error(self, mock_upload):
        succeeding = _make_tenant(suffix=15)
        failing = _make_tenant(suffix=16)

        def side_effect(tenant_id, file_path, content, **kwargs):
            if tenant_id == str(failing.id):
                raise RuntimeError("simulated storage 503")

        mock_upload.side_effect = side_effect

        with self.assertRaises(CommandError):
            call_command("refresh_persona_agents_md")

        # Both tenants got attempted.
        self.assertEqual(mock_upload.call_count, 2)
        ids_attempted = {call.args[0] for call in mock_upload.call_args_list}
        self.assertEqual(ids_attempted, {str(succeeding.id), str(failing.id)})

    def test_no_matching_tenants_raises(self):
        # No tenants created — command should refuse to silently no-op.
        with self.assertRaises(CommandError):
            call_command("refresh_persona_agents_md")
