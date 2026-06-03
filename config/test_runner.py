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

    def teardown_databases(self, old_config, **kwargs):
        # Even with the cron signals disconnected, a stray connection can still
        # linger at teardown (a background thread, a signal a test reconnects in
        # its own setUp, etc.). Postgres refuses ``DROP DATABASE`` while other
        # sessions are connected — the intermittent ``ObjectInUse`` CI failure.
        # Terminate every other backend on the test DB first so the drop always
        # succeeds. Best-effort: never let teardown hardening mask a real result.
        from django.db import connections

        for alias in connections:
            conn = connections[alias]
            if conn.vendor != "postgresql":
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                    )
            except Exception:
                pass

        super().teardown_databases(old_config, **kwargs)
