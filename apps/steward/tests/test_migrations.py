from __future__ import annotations

from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

MIGRATION_MODULE = import_module("apps.steward.migrations.0007_fingerprint_namespace_and_alert_reservation")


class FingerprintNamespaceMigrationTests(TransactionTestCase):
    migrate_from = ("steward", "0006_relock_after_steward_phase2_hardening")
    migrate_to = ("steward", "0007_fingerprint_namespace_and_alert_reservation")

    def _historical_apps(self):
        return MigrationExecutor(connection).loader.project_state([self.migrate_from]).apps

    def test_forward_migration_survives_prefixed_fingerprint_collision(self):
        historical_apps = self._historical_apps()
        EvidenceEvent = historical_apps.get_model("steward", "EvidenceEvent")
        now = timezone.now()
        migrating = EvidenceEvent.objects.create(
            source="ci_run",
            subject="migrating",
            occurred_at=now,
            received_at=now,
            payload={},
            fingerprint="claim",
            trust="authenticated_api",
            provenance="collector",
        )
        collision = EvidenceEvent.objects.create(
            source="eval_run",
            subject="collision",
            occurred_at=now,
            received_at=now,
            payload={},
            fingerprint="ci_run:claim",
            trust="authenticated_api",
            provenance="collector",
        )

        with self.assertLogs(MIGRATION_MODULE.logger.name, level="WARNING"):
            MIGRATION_MODULE.namespace_existing_fingerprints(
                historical_apps,
                schema_editor=None,
            )

        fingerprints = set(
            EvidenceEvent.objects.filter(pk__in=[migrating.pk, collision.pk]).values_list(
                "fingerprint",
                flat=True,
            )
        )
        self.assertEqual(
            fingerprints,
            {
                f"ci_run:claim:migrated:{migrating.pk}",
                "eval_run:ci_run:claim",
            },
        )
        self.assertEqual(len(fingerprints), 2)

    def test_reverse_migration_is_explicitly_irreversible(self):
        run_python = next(
            operation
            for operation in MIGRATION_MODULE.Migration.operations
            if operation.__class__.__name__ == "RunPython"
        )

        self.assertFalse(run_python.reversible)
        with self.assertRaisesRegex(NotImplementedError, "cannot reverse"):
            run_python.database_backwards(
                "steward",
                schema_editor=None,
                from_state=None,
                to_state=None,
            )
