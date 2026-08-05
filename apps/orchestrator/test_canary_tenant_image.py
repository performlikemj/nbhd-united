"""Tests for the ``canary_tenant_image`` management command.

Regression guard for the phantom-canary bug: under ``AZURE_MOCK=true``
(the local dev default — see ``.env``), ``update_container_image`` no-ops
and returns cleanly, but the old command unconditionally printed "Canary
image deployed" anyway. An operator running this one-shot ops command in
a mock-mode shell would believe a production deploy happened when Azure
was never touched at all.

Two layers pin the fix:

  * Mock mode is refused outright, before any Azure call is attempted.
  * Even in real mode, success is only reported after a readback confirms
    the live Container App template actually matches the requested image
    — a no-op ``update_container_image`` (or a deploy that silently
    didn't take) can no longer produce a false "deployed" message.
"""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

_REGISTRY = "nbhdunited.azurecr.io"
_REPOSITORY = "nbhd-openclaw"
_TAG = "canary-abc1234"
_CONTAINER = "oc-148ccf1c-ef13-47f8-a"
_TARGET_IMAGE = f"{_REGISTRY}/{_REPOSITORY}:{_TAG}"


def _call(**extra):
    out = StringIO()
    call_command(
        "canary_tenant_image",
        container=_CONTAINER,
        tag=_TAG,
        stdout=out,
        **extra,
    )
    return out.getvalue()


@override_settings(AZURE_ACR_SERVER=_REGISTRY, AZURE_RESOURCE_GROUP="rg-test")
class CanaryTenantImageMockRefusalTest(SimpleTestCase):
    @patch("apps.orchestrator.management.commands.canary_tenant_image.is_mock", return_value=True)
    @patch("apps.orchestrator.management.commands.canary_tenant_image.update_container_image")
    def test_mock_mode_refuses_before_touching_azure(self, mock_update, mock_is_mock):
        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("canary_tenant_image", container=_CONTAINER, tag=_TAG, stdout=out)

        self.assertIn("AZURE_MOCK", str(ctx.exception))
        # The old bug: this string got printed even when nothing was deployed.
        self.assertNotIn("Canary image deployed", out.getvalue())
        mock_update.assert_not_called()


@override_settings(AZURE_ACR_SERVER=_REGISTRY, AZURE_RESOURCE_GROUP="rg-test")
class CanaryTenantImageReadbackTest(SimpleTestCase):
    @patch("apps.orchestrator.management.commands.canary_tenant_image.is_mock", return_value=False)
    @patch("apps.orchestrator.management.commands.canary_tenant_image.get_container_client")
    @patch("apps.orchestrator.management.commands.canary_tenant_image.update_container_image")
    def test_readback_mismatch_raises_instead_of_claiming_success(self, mock_update, mock_get_client, mock_is_mock):
        """update_container_image no-ops (or silently fails to take) and the
        live template still shows the OLD tag — command must refuse to
        report success and must name requested vs actual.
        """
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        stale_container = SimpleNamespace(name="openclaw", image=f"{_REGISTRY}/{_REPOSITORY}:old-sha")
        mock_client.container_apps.get.return_value = SimpleNamespace(
            template=SimpleNamespace(containers=[stale_container])
        )

        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("canary_tenant_image", container=_CONTAINER, tag=_TAG, stdout=out)

        message = str(ctx.exception)
        self.assertIn(_TARGET_IMAGE, message)
        self.assertIn("old-sha", message)
        self.assertNotIn("Canary image deployed", out.getvalue())
        mock_update.assert_called_once_with(_CONTAINER, _TARGET_IMAGE)

    @patch("apps.orchestrator.management.commands.canary_tenant_image.is_mock", return_value=False)
    @patch("apps.orchestrator.management.commands.canary_tenant_image.get_container_client")
    @patch("apps.orchestrator.management.commands.canary_tenant_image.update_container_image")
    def test_readback_match_reports_success(self, mock_update, mock_get_client, mock_is_mock):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        deployed_container = SimpleNamespace(name="openclaw", image=_TARGET_IMAGE)
        mock_client.container_apps.get.return_value = SimpleNamespace(
            template=SimpleNamespace(containers=[deployed_container])
        )

        output = _call()

        self.assertIn(f"Canary image deployed to {_CONTAINER}", output)
        mock_update.assert_called_once_with(_CONTAINER, _TARGET_IMAGE)
