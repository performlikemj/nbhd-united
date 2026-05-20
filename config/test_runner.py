"""Custom test runner — quiets the CronJob → reconciler signal during tests.

After migration 0067 flipped ``Tenant.postgres_cron_canonical`` to default
True, every ``CronJob.objects.create`` in a test fires the post_save signal
in ``apps/cron/signals.py``, which enqueues ``regenerate_tenant_crons`` via
``publish_task``. In test settings ``QSTASH_TOKEN`` is empty, so
``publish_task`` falls back to synchronous execution and the reconciler
opens DB connections + makes HTTPS calls against the test tenant's
placeholder ``container_fqdn``. The signal handler swallows the resulting
error, but the dangling connections accumulate and Django's
``destroy_test_db`` fails with ``ObjectInUse`` at teardown — the actual
backend-test failure mode observed in CI.

This runner disconnects the two CronJob signals at test-environment
setup. Tests that need to verify the signal contract (e.g.
``PostgresCanonicalSignalTest``) reconnect them via the helper at
``apps.cron.signals.connect_cronjob_reconcile_signals`` in their own
setUp / tearDown.
"""

from __future__ import annotations

from django.test.runner import DiscoverRunner


class QuietCronSignalRunner(DiscoverRunner):
    """DiscoverRunner that disconnects CronJob → reconciler signals for tests."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)

        from apps.cron.signals import disconnect_cronjob_reconcile_signals

        disconnect_cronjob_reconcile_signals()
