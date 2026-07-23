"""Managed per-tenant rules region in ``workspace/TOOLS.md``."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.orchestrator.config_generator import BOOTSTRAP_MAX_CHARS
from apps.orchestrator.services import (
    TOOLS_MD_EXTRAS_BEGIN_MARKER,
    TOOLS_MD_EXTRAS_END_MARKER,
    reassert_tools_md_extras,
    splice_tools_md_extras,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_tenant(*, suffix: int) -> Tenant:
    tenant = create_tenant(display_name=f"ToolsExtras-{suffix}", telegram_chat_id=980000 + suffix)
    tenant.status = Tenant.Status.ACTIVE
    tenant.container_id = f"oc-tools-extras-{suffix}"
    tenant.save(update_fields=["status", "container_id"])
    return tenant


class SpliceToolsMdExtrasTest(TestCase):
    def test_append_when_no_markers(self):
        current = "# Tools\n\nTenant notes."

        result = splice_tools_md_extras(current, "  ## Canary\n\nFollow this.  ")

        self.assertEqual(
            result,
            current
            + "\n\n"
            + TOOLS_MD_EXTRAS_BEGIN_MARKER
            + "\n## Canary\n\nFollow this.\n"
            + TOOLS_MD_EXTRAS_END_MARKER
            + "\n",
        )

    def test_replace_when_markers_present(self):
        prefix = "# Tools\n\nTenant prefix.\n\n"
        suffix = "\n\nTenant suffix.\n"
        current = prefix + TOOLS_MD_EXTRAS_BEGIN_MARKER + "\nold rule\n" + TOOLS_MD_EXTRAS_END_MARKER + suffix

        result = splice_tools_md_extras(current, "new rule")

        self.assertEqual(
            result,
            prefix + TOOLS_MD_EXTRAS_BEGIN_MARKER + "\nnew rule\n" + TOOLS_MD_EXTRAS_END_MARKER + suffix,
        )

    def test_clear_removes_region_and_added_separator(self):
        clean = "# Tools\n\nTenant notes.\n"
        current = splice_tools_md_extras(clean, "temporary rule")

        self.assertEqual(splice_tools_md_extras(current, None), clean)

    def test_idempotent_noop_returns_none(self):
        current = "# Tools\n\n" + TOOLS_MD_EXTRAS_BEGIN_MARKER + "\nsame rule\n" + TOOLS_MD_EXTRAS_END_MARKER + "\n"

        self.assertIsNone(splice_tools_md_extras(current, "same rule"))

    def test_malformed_single_marker_returns_none(self):
        for marker in (TOOLS_MD_EXTRAS_BEGIN_MARKER, TOOLS_MD_EXTRAS_END_MARKER):
            with self.subTest(marker=marker):
                self.assertIsNone(splice_tools_md_extras(f"# Tools\n\n{marker}\n", "rule"))

    def test_empty_current_returns_none(self):
        self.assertIsNone(splice_tools_md_extras(None, "rule"))
        self.assertIsNone(splice_tools_md_extras("", "rule"))
        self.assertIsNone(splice_tools_md_extras(" \n", "rule"))


@override_settings(AZURE_MOCK="true")
class ReassertToolsMdExtrasTest(TestCase):
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_reassert_uploads_managed_region(self, mock_download, mock_upload):
        tenant = _make_tenant(suffix=1)
        tenant.user.preferences = {"prompt_extras": {"tools_md": "managed rule"}}
        tenant.user.save(update_fields=["preferences"])
        mock_download.return_value = "# Tools\n"

        self.assertTrue(reassert_tools_md_extras(tenant))

        mock_upload.assert_called_once()
        self.assertEqual(mock_upload.call_args.args[1], "workspace/TOOLS.md")
        self.assertIn("managed rule", mock_upload.call_args.args[2])

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_reassert_download_error_fails_closed(self, mock_download, mock_upload):
        tenant = _make_tenant(suffix=2)
        tenant.user.preferences = {"prompt_extras": {"tools_md": "managed rule"}}
        tenant.user.save(update_fields=["preferences"])
        mock_download.side_effect = RuntimeError("azure throttled")

        self.assertFalse(reassert_tools_md_extras(tenant))
        mock_upload.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_reassert_no_write_when_extras_absent(self, mock_download, mock_upload):
        tenant = _make_tenant(suffix=3)
        mock_download.return_value = "# Tools\n"

        self.assertFalse(reassert_tools_md_extras(tenant))
        mock_upload.assert_not_called()

    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_reassert_logs_error_near_bootstrap_cap(self, mock_download, mock_upload):
        tenant = _make_tenant(suffix=4)
        tenant.user.preferences = {"prompt_extras": {"tools_md": "x" * BOOTSTRAP_MAX_CHARS}}
        tenant.user.save(update_fields=["preferences"])
        mock_download.return_value = "# Tools\n"

        with self.assertLogs("apps.orchestrator.services", level="ERROR") as logs:
            self.assertTrue(reassert_tools_md_extras(tenant))

        self.assertTrue(any("TOOLS.md near bootstrap cap" in line for line in logs.output))
        mock_upload.assert_called_once()
