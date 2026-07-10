"""Tests for apps.crypto.prewarm — async best-effort DEK pre-warm (Phase 1 PR4).

Runs against the stateful `_MOCK_KEK_REGISTRY` in
`apps.orchestrator.azure_client` (AZURE_MOCK=true, forced via `_is_mock`
patch — never the ambient env var, matching the convention in
apps/crypto/test_cache.py and apps/crypto/test_keys.py). Each test mints its
own tenant(s) with a fresh random UUID.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from django.test import TestCase

from apps.crypto import cache
from apps.crypto.keys import mint_and_wrap_dek
from apps.crypto.prewarm import (
    _is_management_command,
    prewarm_all_provisioned,
    start_prewarm_thread,
)
from apps.tenants.models import Tenant, User

# sys.argv as it looks inside a gunicorn worker (no manage.py anywhere) —
# the shape `start_prewarm_thread`/`prewarm_all_provisioned` must actually warm under.
SERVER_ARGV = ["/usr/local/bin/gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
# sys.argv as it looks for the poller's own `python manage.py poll_telegram`.
POLLER_ARGV = ["manage.py", "poll_telegram"]


def _create_tenant(*, suffix: str, provisioned: bool = False, hibernated: bool = False) -> Tenant:
    user = User.objects.create_user(username=f"crypto-prewarm-{suffix}", password="pass1234")
    kwargs = dict(user=user, status=Tenant.Status.ACTIVE, model_tier=Tenant.ModelTier.STARTER)
    if provisioned:
        kwargs["container_id"] = f"oc-{suffix}"
        kwargs["managed_identity_id"] = f"mi-nbhd-{suffix}"
    if hibernated:
        from django.utils import timezone

        kwargs["hibernated_at"] = timezone.now()
    return Tenant.objects.create(**kwargs)


class _ForceMockAzureMixin:
    """Force `azure_client` into mock mode via patch, never the ambient env var.

    Mirrors the proven-green pattern in apps/crypto/test_cache.py /
    apps/crypto/test_keys.py — CI's full suite can leave `AZURE_MOCK` unset
    in `os.environ`, so this must not depend on it.
    """

    def setUp(self):
        super().setUp()
        mock_patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        mock_patcher.start()
        self.addCleanup(mock_patcher.stop)


class IsManagementCommandTest(TestCase):
    """Pure `sys.argv` classification — no DB, no mock needed."""

    def test_gunicorn_argv_is_not_a_management_command(self):
        with patch("sys.argv", SERVER_ARGV):
            self.assertFalse(_is_management_command())

    def test_poll_telegram_is_allowlisted_as_the_server_subcommand(self):
        with patch("sys.argv", POLLER_ARGV):
            self.assertFalse(_is_management_command())

    def test_poll_telegram_via_absolute_manage_py_path_is_allowlisted(self):
        with patch("sys.argv", ["/app/manage.py", "poll_telegram"]):
            self.assertFalse(_is_management_command())

    def test_migrate_is_a_management_command(self):
        with patch("sys.argv", ["manage.py", "migrate"]):
            self.assertTrue(_is_management_command())

    def test_test_runner_is_a_management_command(self):
        with patch("sys.argv", ["manage.py", "test", "apps.crypto.test_prewarm"]):
            self.assertTrue(_is_management_command())

    def test_makemigrations_is_a_management_command(self):
        with patch("sys.argv", ["manage.py", "makemigrations", "--check", "--dry-run"]):
            self.assertTrue(_is_management_command())

    def test_bare_manage_py_with_no_subcommand_is_a_management_command(self):
        with patch("sys.argv", ["manage.py"]):
            self.assertTrue(_is_management_command())

    def test_empty_argv_is_not_a_management_command(self):
        with patch("sys.argv", []):
            self.assertFalse(_is_management_command())


class PrewarmAllProvisionedTest(_ForceMockAzureMixin, TestCase):
    def test_warms_every_provisioned_tenant_including_hibernated(self):
        active = _create_tenant(suffix="active", provisioned=True)
        hibernated = _create_tenant(suffix="hibernated", provisioned=True, hibernated=True)
        unprovisioned = _create_tenant(suffix="unprovisioned")  # no container/identity

        mint_and_wrap_dek(active)
        mint_and_wrap_dek(hibernated)
        # Deliberately no DEK minted for `unprovisioned` — proves it's never touched.

        with (
            patch("sys.argv", SERVER_ARGV),
            patch("apps.crypto.prewarm.cache.get_dek", wraps=cache.get_dek) as spy_get_dek,
        ):
            prewarm_all_provisioned()

        called = {(str(c.args[0]), c.args[1]) for c in spy_get_dek.call_args_list}
        self.assertIn((str(active.id), 0), called)
        self.assertIn((str(hibernated.id), 0), called)
        self.assertNotIn((str(unprovisioned.id), 0), called)

        # The cache now genuinely holds both DEKs — a later read does zero unwraps.
        with patch("apps.crypto.cache.azure_client.unwrap_dek") as mock_unwrap:
            self.assertEqual(len(cache.get_dek(active.id, 0)), 32)
            self.assertEqual(len(cache.get_dek(hibernated.id, 0)), 32)
        mock_unwrap.assert_not_called()

    def test_warms_under_the_poller_argv_too(self):
        tenant = _create_tenant(suffix="poller", provisioned=True)
        mint_and_wrap_dek(tenant)

        with patch("sys.argv", POLLER_ARGV):
            prewarm_all_provisioned()

        with patch("apps.crypto.cache.azure_client.unwrap_dek") as mock_unwrap:
            self.assertEqual(len(cache.get_dek(tenant.id, 0)), 32)
        mock_unwrap.assert_not_called()

    def test_zero_unwrap_calls_under_a_management_command_argv(self):
        tenant = _create_tenant(suffix="mgmt", provisioned=True)
        mint_and_wrap_dek(tenant)

        with (
            patch("sys.argv", ["manage.py", "test", "apps.crypto.test_prewarm"]),
            patch("apps.crypto.prewarm.cache.get_dek") as spy_get_dek,
        ):
            prewarm_all_provisioned()

        spy_get_dek.assert_not_called()

    def test_zero_unwrap_calls_under_migrate_argv(self):
        """Red-team finding 7: this is the exact deploy-time scenario the
        guard exists for — `tenant_deks` columns may not exist yet."""
        tenant = _create_tenant(suffix="migrate-guard", provisioned=True)
        mint_and_wrap_dek(tenant)

        with (
            patch("sys.argv", ["manage.py", "migrate"]),
            patch("apps.orchestrator.azure_client.unwrap_dek") as mock_unwrap,
        ):
            prewarm_all_provisioned()

        mock_unwrap.assert_not_called()

    def test_one_tenant_failure_does_not_abort_the_sweep(self):
        good = _create_tenant(suffix="good", provisioned=True)
        bad = _create_tenant(suffix="bad", provisioned=True)
        mint_and_wrap_dek(good)
        mint_and_wrap_dek(bad)

        real_get_dek = cache.get_dek

        def _flaky(tenant_id, dek_epoch):
            if str(tenant_id) == str(bad.id):
                raise RuntimeError("Key Vault throttled")
            return real_get_dek(tenant_id, dek_epoch)

        with (
            patch("sys.argv", SERVER_ARGV),
            patch("apps.crypto.prewarm.cache.get_dek", side_effect=_flaky),
            self.assertLogs("apps.crypto.prewarm", level="WARNING") as log_ctx,
        ):
            prewarm_all_provisioned()  # must not raise

        self.assertTrue(any(str(bad.id)[:8] in msg for msg in log_ctx.output))

        # The good tenant still ended up warm despite the bad one raising.
        with patch("apps.crypto.cache.azure_client.unwrap_dek") as mock_unwrap:
            self.assertEqual(len(cache.get_dek(good.id, 0)), 32)
        mock_unwrap.assert_not_called()

    def test_no_provisioned_tenants_is_a_quiet_no_op(self):
        with patch("sys.argv", SERVER_ARGV):
            prewarm_all_provisioned()  # must not raise even with nothing to warm


class StartPrewarmThreadTest(_ForceMockAzureMixin, TestCase):
    def test_spawns_a_daemon_thread_and_returns_immediately(self):
        tenant = _create_tenant(suffix="thread", provisioned=True)
        mint_and_wrap_dek(tenant)

        with patch("sys.argv", SERVER_ARGV), patch("threading.Thread") as mock_thread_cls:
            start_prewarm_thread()

        mock_thread_cls.assert_called_once()
        _args, kwargs = mock_thread_cls.call_args
        self.assertTrue(kwargs.get("daemon"))
        mock_thread_cls.return_value.start.assert_called_once()

    def test_thread_spawn_failure_never_raises(self):
        with (
            patch("threading.Thread", side_effect=RuntimeError("can't start new thread")),
            self.assertLogs("apps.crypto.prewarm", level="WARNING"),
        ):
            start_prewarm_thread()  # must not raise


class LoggerNameSanityTest(TestCase):
    def test_module_logger_matches_module_name(self):
        import apps.crypto.prewarm as prewarm_module

        self.assertEqual(prewarm_module.logger.name, "apps.crypto.prewarm")
        self.assertIsInstance(prewarm_module.logger, logging.Logger)
