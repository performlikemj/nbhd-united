"""Channel-identity correctness: the assistant must know it's on the NBHD app
(iOS), Telegram, or LINE — never assume Telegram.

Covers the orchestrator-side pieces of the fix:
  * ``_resolve_channel_formatting`` — linkage-based doc selection, NO telegram
    fallback (an iOS-only signup must not get telegram-formatting.md).
  * ``converge_tools_md_text`` / ``reassert_tools_md`` — the seed-once TOOLS.md
    convergence primitive (surgical channel-line swap through the share
    chokepoint) + a regression guard on the shipped template.
  * ``_build_channels_config`` — no preferred_channel fallback; unlinked tenants
    get an EMPTY channels dict, which the config validator accepts.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_tenant(*, suffix: int, container: bool = True) -> Tenant:
    tenant = create_tenant(display_name=f"ChanId-{suffix}", telegram_chat_id=970000 + suffix)
    tenant.status = Tenant.Status.ACTIVE
    tenant.container_id = f"oc-chanid-{suffix}" if container else ""
    tenant.save()
    return tenant


def _fresh(tenant: Tenant) -> Tenant:
    return Tenant.objects.select_related("user").get(id=tenant.id)


class ResolveChannelFormattingTest(TestCase):
    """Selection is linkage-based with NO telegram fallback."""

    def test_ios_only_gets_app_doc_not_telegram(self):
        # The 21-of-37 case: signed up on iOS, never linked Telegram/LINE.
        from apps.orchestrator.personas import _resolve_channel_formatting

        t = _make_tenant(suffix=1)
        t.user.telegram_chat_id = None
        t.user.line_user_id = ""
        t.user.save()

        content = _resolve_channel_formatting(_fresh(t))
        self.assertIn("NBHD App Formatting", content)
        self.assertNotIn("Telegram Formatting", content)

    def test_device_token_prefers_app_even_with_telegram_linked(self):
        from apps.orchestrator.personas import _resolve_channel_formatting
        from apps.router.models import DeviceToken

        t = _make_tenant(suffix=2)  # telegram_chat_id is set by create_tenant
        DeviceToken.objects.create(
            tenant=t, user=t.user, token="a" * 64, environment=DeviceToken.Environment.PRODUCTION
        )

        content = _resolve_channel_formatting(_fresh(t))
        self.assertIn("NBHD App Formatting", content)

    def test_telegram_linked_no_token_gets_telegram_doc(self):
        from apps.orchestrator.personas import _resolve_channel_formatting

        t = _make_tenant(suffix=3)  # telegram linked, no device token
        content = _resolve_channel_formatting(_fresh(t))
        self.assertIn("Telegram Formatting", content)

    def test_line_linked_no_token_gets_line_doc(self):
        from apps.orchestrator.personas import _resolve_channel_formatting

        t = _make_tenant(suffix=4)
        t.user.telegram_chat_id = None
        t.user.line_user_id = "U" + "b" * 32
        t.user.save()

        content = _resolve_channel_formatting(_fresh(t))
        self.assertIn("LINE Formatting", content)

    def test_nothing_linked_no_tenant_gets_app_doc(self):
        from apps.orchestrator.personas import _resolve_channel_formatting

        content = _resolve_channel_formatting(None)
        self.assertIn("NBHD App Formatting", content)

    def test_both_linked_no_token_prefers_telegram(self):
        # Precedence locks to app → telegram → line, matching the final
        # resolve_user_channel fallback order (integration-train fix 094e7744).
        from apps.orchestrator.personas import _resolve_channel_formatting

        t = _make_tenant(suffix=5)  # telegram linked by create_tenant
        t.user.line_user_id = "U" + "d" * 32
        t.user.save()

        content = _resolve_channel_formatting(_fresh(t))
        self.assertIn("Telegram Formatting", content)


class ToolsMdConvergenceTest(TestCase):
    """TOOLS.md seed-once convergence — surgical channel-line swap."""

    _LEGACY = "- **Telegram** is the primary channel. All messages come through Telegram DM."

    def test_converge_swaps_legacy_line(self):
        from apps.orchestrator.services import converge_tools_md_text

        current = f"# Tools\n\n## Communication\n\n{self._LEGACY}\n\n## Notes\n\nmy note\n"
        out = converge_tools_md_text(current)
        self.assertIsNotNone(out)
        self.assertNotIn(self._LEGACY, out)
        self.assertIn("NBHD app (iOS)", out)
        # Preserves the user's Notes section verbatim.
        self.assertIn("my note", out)

    def test_converge_idempotent_when_already_converged(self):
        from apps.orchestrator.services import converge_tools_md_text

        current = "# Tools\n\n## Communication\n\n- Messages reach you from the **NBHD app (iOS)**...\n"
        self.assertIsNone(converge_tools_md_text(current))

    def test_converge_none_on_empty(self):
        from apps.orchestrator.services import converge_tools_md_text

        self.assertIsNone(converge_tools_md_text(None))
        self.assertIsNone(converge_tools_md_text(""))

    @override_settings(AZURE_MOCK="true")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_reassert_writes_converged(self, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_tools_md

        t = _make_tenant(suffix=10)
        mock_dl.return_value = f"# Tools\n\n## Communication\n\n{self._LEGACY}\n"

        self.assertTrue(reassert_tools_md(t))
        mock_ul.assert_called_once()
        self.assertEqual(mock_ul.call_args.args[1], "workspace/TOOLS.md")
        self.assertNotIn(self._LEGACY, mock_ul.call_args.args[2])
        self.assertIn("NBHD app (iOS)", mock_ul.call_args.args[2])

    @override_settings(AZURE_MOCK="true")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_reassert_noop_when_no_legacy_line(self, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_tools_md

        t = _make_tenant(suffix=11)
        mock_dl.return_value = "# Tools\n\n## Communication\n\n- already channel-agnostic\n"

        self.assertFalse(reassert_tools_md(t))
        mock_ul.assert_not_called()

    @override_settings(AZURE_MOCK="true")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_reassert_read_error_skips_write(self, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_tools_md

        t = _make_tenant(suffix=12)
        mock_dl.side_effect = RuntimeError("azure throttled")

        self.assertFalse(reassert_tools_md(t))
        mock_ul.assert_not_called()

    @override_settings(AZURE_MOCK="true")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_reassert_noop_without_container(self, mock_dl, mock_ul):
        from apps.orchestrator.services import reassert_tools_md

        t = _make_tenant(suffix=13, container=False)
        self.assertFalse(reassert_tools_md(t))
        mock_dl.assert_not_called()
        mock_ul.assert_not_called()

    def test_shipped_template_no_longer_claims_telegram_is_the_channel(self):
        """Regression: the seed template must not claim Telegram is THE channel,
        and must carry the exact channel-agnostic body the reassert converges to."""
        from apps.orchestrator.services import _TOOLS_MD_CHANNEL_BODY

        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(here, "templates", "openclaw", "TOOLS.md")) as f:
            tmpl = f.read()
        self.assertNotIn(self._LEGACY, tmpl)
        self.assertNotIn("Telegram is the primary channel", tmpl)
        self.assertIn(_TOOLS_MD_CHANNEL_BODY, tmpl)


class ConvergeToolsMdCommandTest(TestCase):
    """CLI ergonomics of the fleet-convergence command."""

    def test_malformed_tenant_uuid_raises_command_error(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("converge_tools_md", tenant="not-a-uuid")


class BuildChannelsConfigTest(TestCase):
    """No preferred_channel fallback — unlinked tenants get an empty dict."""

    def test_ios_only_gets_empty_channels(self):
        from apps.orchestrator.config_generator import _build_channels_config

        t = _make_tenant(suffix=20)
        t.user.telegram_chat_id = None
        t.user.line_user_id = ""
        t.user.save()

        self.assertEqual(_build_channels_config(_fresh(t)), {})

    def test_telegram_linked_enables_telegram_only(self):
        from apps.orchestrator.config_generator import _build_channels_config

        t = _make_tenant(suffix=21)  # telegram linked by create_tenant
        self.assertEqual(_build_channels_config(_fresh(t)), {"telegram": {"enabled": True}})

    def test_line_linked_enables_line(self):
        from apps.orchestrator.config_generator import _build_channels_config

        t = _make_tenant(suffix=22)
        t.user.telegram_chat_id = None
        t.user.line_user_id = "U" + "c" * 32
        t.user.save()

        self.assertEqual(_build_channels_config(_fresh(t)), {"line": {"enabled": True}})

    def test_empty_channels_passes_validator(self):
        """An empty channels dict must boot: the container is reached over the
        gateway endpoint, not a channel plugin. The validator raises no
        channels-path issue for {}."""
        from apps.orchestrator.config_validator import validate_openclaw_config

        cfg = {
            "gateway": {
                "mode": "local",
                "bind": "loopback",
                "auth": {"mode": "token", "token": "${NBHD_INTERNAL_API_KEY}"},
            },
            "channels": {},
            "agents": {},
            "tools": {},
            "cron": {"enabled": True},
        }
        issues = validate_openclaw_config(cfg)
        channel_issues = [i for i in issues if i.path.startswith("channels")]
        self.assertEqual(channel_issues, [])
