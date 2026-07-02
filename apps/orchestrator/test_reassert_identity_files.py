"""Tests for ``services.reassert_identity_files`` — the SOUL/IDENTITY self-heal.

Mirrors ``test_reassert_agents_md``: the managed region is re-asserted to the
share, the agent's growth region below the END marker is preserved, writes are
hash-gated (no write when already current), and a read error fails CLOSED (skip,
never clobber growth). Patches at the source modules because the function imports
them locally.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.orchestrator import identity_merge as im
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

_MANAGED_SOUL = im.SOUL_BEGIN_MARKER + "\n\nSOUL BODY\n\n" + im.SOUL_END_MARKER + "\n"
_MANAGED_IDENTITY = im.IDENTITY_BEGIN_MARKER + "\n\nIDENT BODY\n\n" + im.IDENTITY_END_MARKER + "\n"
_RENDER = {"NBHD_SOUL_MD": _MANAGED_SOUL, "NBHD_IDENTITY_MD": _MANAGED_IDENTITY}


def _make_tenant(*, suffix: int, status=Tenant.Status.ACTIVE, container: bool = True):
    tenant = create_tenant(display_name=f"Ident-{suffix}", telegram_chat_id=970000 + suffix)
    tenant.status = status
    tenant.container_id = f"oc-ident-{suffix}" if container else ""
    tenant.save()
    return tenant


def _by_path(soul=None, identity=None):
    def _dl(_tid, path):
        return soul if path == "workspace/SOUL.md" else identity

    return _dl


@override_settings(AZURE_MOCK="true")
class ReassertIdentityFilesTest(TestCase):
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_seeds_when_share_missing(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_identity_files

        t = _make_tenant(suffix=1)
        mock_render.return_value = _RENDER
        mock_dl.return_value = None  # fresh share

        result = reassert_identity_files(t)
        self.assertEqual(result, {"soul": True, "identity": True})
        self.assertEqual(mock_ul.call_count, 2)
        soul_call = next(c for c in mock_ul.call_args_list if c.args[1] == "workspace/SOUL.md")
        self.assertTrue(soul_call.args[2].startswith(im.SOUL_BEGIN_MARKER))
        self.assertIn("This space is yours", soul_call.args[2])  # growth seed on fresh file

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_noop_when_share_current(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_identity_files

        t = _make_tenant(suffix=2)
        mock_render.return_value = _RENDER
        mock_dl.side_effect = _by_path(soul=_MANAGED_SOUL, identity=_MANAGED_IDENTITY)

        result = reassert_identity_files(t)
        self.assertEqual(result, {"soul": False, "identity": False})
        mock_ul.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_preserves_growth_region(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_identity_files

        t = _make_tenant(suffix=3)
        mock_render.return_value = _RENDER
        growth = "You started calling me Bird.\n"
        stale = im.SOUL_BEGIN_MARKER + "\n\nOLD BODY\n\n" + im.SOUL_END_MARKER + "\n\n" + growth
        mock_dl.side_effect = _by_path(soul=stale, identity=_MANAGED_IDENTITY)

        result = reassert_identity_files(t, files=("soul",))
        self.assertTrue(result["soul"])
        soul_call = next(c for c in mock_ul.call_args_list if c.args[1] == "workspace/SOUL.md")
        written = soul_call.args[2]
        self.assertIn(growth.strip(), written)  # growth preserved
        self.assertIn("SOUL BODY", written)  # fresh managed
        self.assertNotIn("OLD BODY", written)  # stale managed replaced

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_read_error_fails_closed(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_identity_files

        t = _make_tenant(suffix=4)
        mock_render.return_value = _RENDER
        mock_dl.side_effect = RuntimeError("azure throttled")

        result = reassert_identity_files(t)
        self.assertEqual(result, {"soul": False, "identity": False})
        mock_ul.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_noop_when_no_container(self, mock_render, mock_ul):
        from apps.orchestrator.services import reassert_identity_files

        t = _make_tenant(suffix=5, container=False)

        result = reassert_identity_files(t)
        self.assertEqual(result, {"soul": False, "identity": False})
        mock_render.assert_not_called()
        mock_ul.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.personas.render_workspace_files")
    def test_files_filter_only_touches_requested(self, mock_render, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_identity_files

        t = _make_tenant(suffix=6)
        mock_render.return_value = _RENDER
        mock_dl.return_value = None

        result = reassert_identity_files(t, files=("identity",))
        self.assertEqual(result, {"identity": True})
        self.assertEqual(mock_ul.call_count, 1)
        self.assertEqual(mock_ul.call_args.args[1], "workspace/IDENTITY.md")
