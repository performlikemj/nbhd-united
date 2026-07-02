"""Tests for ``services.reassert_agents_md`` — the AGENTS.md file-share self-heal.

AGENTS.md is seed-once from the ``NBHD_AGENTS_MD`` env var at boot (see
``runtime/openclaw/entrypoint.sh``); the file share is authoritative. This
primitive re-renders and writes the current AGENTS.md to the share (share-only,
no revision/restart), and is called from the container-started hook on every
boot to self-heal after any restart. It patches at the *source* modules because
``reassert_agents_md`` imports them locally.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_tenant(*, suffix: int, status=Tenant.Status.ACTIVE, container: bool = True):
    tenant = create_tenant(display_name=f"Reassert-{suffix}", telegram_chat_id=960000 + suffix)
    tenant.status = status
    tenant.container_id = f"oc-reassert-{suffix}" if container else ""
    tenant.save()
    return tenant


@override_settings(AZURE_MOCK="true")
class ReassertAgentsMdTest(TestCase):
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_writes_when_share_is_stale(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_agents_md

        t = _make_tenant(suffix=1)
        mock_render.return_value = {"NBHD_AGENTS_MD": "FRESH gate content"}
        mock_dl.return_value = "STALE provision snapshot"

        self.assertTrue(reassert_agents_md(t))
        mock_ul.assert_called_once()
        # (tenant_id, path, content)
        self.assertEqual(mock_ul.call_args.args[1], "workspace/AGENTS.md")
        self.assertEqual(mock_ul.call_args.args[2], "FRESH gate content")

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_noop_when_share_matches(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_agents_md

        t = _make_tenant(suffix=2)
        # Trailing-newline diff (env-seeded printf vs Django write) is tolerated.
        mock_render.return_value = {"NBHD_AGENTS_MD": "SAME\n"}
        mock_dl.return_value = "SAME"

        self.assertFalse(reassert_agents_md(t))
        mock_ul.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_force_writes_without_reading_share(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_agents_md

        t = _make_tenant(suffix=3)
        mock_render.return_value = {"NBHD_AGENTS_MD": "forced content"}

        self.assertTrue(reassert_agents_md(t, only_if_changed=False))
        mock_dl.assert_not_called()  # no compare when forcing
        mock_ul.assert_called_once()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_noop_when_no_container(self, mock_render, mock_ul):
        from apps.orchestrator.services import reassert_agents_md

        t = _make_tenant(suffix=4, container=False)

        self.assertFalse(reassert_agents_md(t))
        mock_render.assert_not_called()  # short-circuits before rendering
        mock_ul.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_noop_when_inactive(self, mock_render, mock_ul):
        from apps.orchestrator.services import reassert_agents_md

        t = _make_tenant(suffix=5, status=Tenant.Status.SUSPENDED)

        self.assertFalse(reassert_agents_md(t))
        mock_render.assert_not_called()
        mock_ul.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_noop_when_render_empty(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_agents_md

        t = _make_tenant(suffix=6)
        mock_render.return_value = {"NBHD_AGENTS_MD": ""}

        self.assertFalse(reassert_agents_md(t, only_if_changed=False))
        mock_ul.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_read_error_skips_write(self, mock_render, mock_dl, mock_ul):
        """A read error must NOT force a write (avoids write amplification
        during Azure-throttle / fleet-restart storms)."""
        from apps.orchestrator.services import reassert_agents_md

        t = _make_tenant(suffix=7)
        mock_render.return_value = {"NBHD_AGENTS_MD": "FRESH"}
        mock_dl.side_effect = RuntimeError("azure throttled")

        self.assertFalse(reassert_agents_md(t))
        mock_ul.assert_not_called()
