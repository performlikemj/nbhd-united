"""Tests for ``bump_all_tenant_images`` management command.

The command is the non-lazy fleet rollout for OpenClaw images. It mirrors
``bump_openclaw_version`` but skips the per-tenant config regeneration —
fleet rollouts that ship Dockerfile changes don't always need a config
bump (BYO Anthropic, for example, is a runtime change driven by the
already-deployed config + new image binary).

These tests pin the contract:

  * Idempotent — tenants already on the target tag are skipped.
  * Concurrency capped — the thread pool max_workers honors the flag so
    rate limits aren't tripped.
  * Hibernated skipped by default; ``--include-hibernated`` overrides.
  * Suspended/pending/deprovisioning out of scope.
  * Failures don't block other tenants from succeeding — but the command
    surfaces a non-zero exit so CI/cron see partial rollouts.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

_TARGET_TAG = "abc1234"
_OLDER_TAG = "old-sha"
_REGISTRY = "nbhdunited.azurecr.io"


def _make_tenant(*, suffix: int, status=Tenant.Status.ACTIVE, image_tag: str = _OLDER_TAG, hibernated: bool = False):
    tenant = create_tenant(display_name=f"BumpAllTest-{suffix}", telegram_chat_id=900000 + suffix)
    tenant.status = status
    tenant.container_id = f"oc-bump-all-{suffix}"
    tenant.container_fqdn = f"oc-bump-all-{suffix}.internal"
    tenant.container_image_tag = image_tag
    if hibernated:
        tenant.hibernated_at = timezone.now()
    tenant.save()
    return tenant


@override_settings(
    OPENCLAW_IMAGE_TAG=_TARGET_TAG,
    AZURE_ACR_SERVER=_REGISTRY,
    AZURE_MOCK="true",
)
class BumpAllTenantImagesTest(TestCase):
    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_bumps_all_active_tenants_with_stale_image(self, mock_update):
        """Each active, non-hibernated tenant on a stale image gets one Azure call."""
        t1 = _make_tenant(suffix=1)
        t2 = _make_tenant(suffix=2)

        call_command("bump_all_tenant_images")

        # Two tenants, two Azure calls.
        self.assertEqual(mock_update.call_count, 2)
        called_containers = {call.args[0] for call in mock_update.call_args_list}
        self.assertEqual(called_containers, {t1.container_id, t2.container_id})

        # Both should have their stored tag updated.
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.container_image_tag, _TARGET_TAG)
        self.assertEqual(t2.container_image_tag, _TARGET_TAG)

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_skips_tenants_already_on_target_tag(self, mock_update):
        """Idempotent: rerunning is a no-op for tenants on the target tag."""
        already_current = _make_tenant(suffix=3, image_tag=_TARGET_TAG)
        stale = _make_tenant(suffix=4, image_tag=_OLDER_TAG)

        call_command("bump_all_tenant_images")

        # Only the stale tenant should have been hit.
        self.assertEqual(mock_update.call_count, 1)
        self.assertEqual(mock_update.call_args.args[0], stale.container_id)

        already_current.refresh_from_db()
        # The current-tag tenant's row is untouched (no update_fields call).
        self.assertEqual(already_current.container_image_tag, _TARGET_TAG)

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_run_is_idempotent_on_repeat(self, mock_update):
        """Running twice in a row only bumps each tenant once."""
        _make_tenant(suffix=5)

        call_command("bump_all_tenant_images")
        self.assertEqual(mock_update.call_count, 1)

        # Second run — every tenant is now on the target tag.
        call_command("bump_all_tenant_images")
        # No additional calls.
        self.assertEqual(mock_update.call_count, 1)

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_skips_hibernated_tenants_by_default(self, mock_update):
        """Hibernated tenants are skipped — wake hook (PR #384) handles them lazily."""
        active = _make_tenant(suffix=6)
        hibernated = _make_tenant(suffix=7, hibernated=True)

        call_command("bump_all_tenant_images")

        # Only the active one was bumped.
        self.assertEqual(mock_update.call_count, 1)
        self.assertEqual(mock_update.call_args.args[0], active.container_id)

        hibernated.refresh_from_db()
        # Hibernated tenant's tag stays stale; no Azure call was made.
        self.assertEqual(hibernated.container_image_tag, _OLDER_TAG)

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_include_hibernated_forces_bump(self, mock_update):
        """``--include-hibernated`` covers the wake hook gap for zero-skew rollouts."""
        active = _make_tenant(suffix=8)
        hibernated = _make_tenant(suffix=9, hibernated=True)

        call_command("bump_all_tenant_images", "--include-hibernated")

        self.assertEqual(mock_update.call_count, 2)
        bumped = {call.args[0] for call in mock_update.call_args_list}
        self.assertEqual(bumped, {active.container_id, hibernated.container_id})

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_skips_suspended_pending_and_deprovisioning(self, mock_update):
        """Out-of-scope statuses don't get bumped."""
        active = _make_tenant(suffix=10)
        _make_tenant(suffix=11, status=Tenant.Status.SUSPENDED)
        _make_tenant(suffix=12, status=Tenant.Status.PENDING)
        _make_tenant(suffix=13, status=Tenant.Status.DEPROVISIONING)

        call_command("bump_all_tenant_images")

        self.assertEqual(mock_update.call_count, 1)
        self.assertEqual(mock_update.call_args.args[0], active.container_id)

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_dry_run_makes_no_azure_calls(self, mock_update):
        _make_tenant(suffix=14)

        out = StringIO()
        call_command("bump_all_tenant_images", "--dry-run", stdout=out)

        mock_update.assert_not_called()
        self.assertIn("DRY RUN", out.getvalue())

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_failure_for_one_tenant_does_not_block_others(self, mock_update):
        """Per-tenant failure is isolated — the rest of the fleet still rolls."""
        succeeding = _make_tenant(suffix=15)
        failing = _make_tenant(suffix=16)

        # Fail only the failing tenant's bump.
        def side_effect(container_name, image):
            if container_name == failing.container_id:
                raise RuntimeError("simulated Azure 429")

        mock_update.side_effect = side_effect

        with self.assertRaises(CommandError):
            call_command("bump_all_tenant_images")

        # Both tenants got hit; only the succeeding one persisted its tag.
        self.assertEqual(mock_update.call_count, 2)
        succeeding.refresh_from_db()
        failing.refresh_from_db()
        self.assertEqual(succeeding.container_image_tag, _TARGET_TAG)
        self.assertEqual(failing.container_image_tag, _OLDER_TAG)

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.concurrent.futures.ThreadPoolExecutor")
    def test_max_workers_respected(self, mock_executor_cls, mock_update):
        """Concurrency cap is honored — Container Apps API rate limit guard."""
        _make_tenant(suffix=17)

        # Capture the max_workers arg passed to ThreadPoolExecutor.
        mock_pool = MagicMock()
        mock_pool.__enter__.return_value = mock_pool
        mock_pool.__exit__.return_value = False
        # Submit returns a future-like object whose .result() succeeds.
        future = MagicMock()
        future.result.return_value = None
        mock_pool.submit.return_value = future
        mock_executor_cls.return_value = mock_pool

        # `as_completed` needs to be patched too since we mocked the pool.
        with patch(
            "apps.orchestrator.management.commands.bump_all_tenant_images.concurrent.futures.as_completed",
            side_effect=lambda f: list(f.keys()),
        ):
            call_command("bump_all_tenant_images", "--max-workers", "3")

        mock_executor_cls.assert_called_once_with(max_workers=3)

    def test_refuses_latest_tag(self):
        """Refusing to deploy 'latest' protects the idempotence contract."""
        _make_tenant(suffix=18)

        with override_settings(OPENCLAW_IMAGE_TAG="latest"), self.assertRaises(CommandError) as ctx:
            call_command("bump_all_tenant_images")

        self.assertIn("latest", str(ctx.exception))

    @patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image")
    def test_explicit_tag_overrides_settings(self, mock_update):
        explicit_tag = "explicit-tag-9999"
        _make_tenant(suffix=19)

        call_command("bump_all_tenant_images", "--tag", explicit_tag)

        self.assertEqual(mock_update.call_count, 1)
        # Image string includes the explicit tag, not the settings tag.
        called_image = mock_update.call_args.args[1]
        self.assertIn(explicit_tag, called_image)
        self.assertNotIn(_TARGET_TAG, called_image)
