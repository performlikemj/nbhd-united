"""Regression tests for the DeepSeek V4 Pro default config bump."""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from apps.billing.constants import DEEPSEEK_FLASH_MODEL, GEMMA_MODEL


class DefaultModelProMigrationTest(TransactionTestCase):
    migrate_from = ("tenants", "0145_tenant_layer1_placeholder_writes")
    migrate_to = ("tenants", "0146_bump_tier_default_tenants_for_pro")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model("tenants", "User")
        Tenant = old_apps.get_model("tenants", "Tenant")

        rolling_user = User.objects.create(username="migration-rolling-default")
        flash_user = User.objects.create(username="migration-explicit-flash")
        other_user = User.objects.create(username="migration-explicit-other")
        self.rolling_default_id = Tenant.objects.create(
            user=rolling_user,
            preferred_model="",
            task_model_preferences={"morning_briefing": DEEPSEEK_FLASH_MODEL},
            pending_config_version=4,
        ).pk
        self.explicit_flash_id = Tenant.objects.create(
            user=flash_user,
            preferred_model=DEEPSEEK_FLASH_MODEL,
            pending_config_version=7,
        ).pk
        self.explicit_other_id = Tenant.objects.create(
            user=other_user,
            preferred_model=GEMMA_MODEL,
            pending_config_version=9,
        ).pk

    def tearDown(self):
        MigrationExecutor(connection).migrate([self.migrate_to])
        super().tearDown()

    def test_bumps_only_rolling_default_tenants(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps
        Tenant = new_apps.get_model("tenants", "Tenant")

        rolling_default = Tenant.objects.get(pk=self.rolling_default_id)
        explicit_flash = Tenant.objects.get(pk=self.explicit_flash_id)
        explicit_other = Tenant.objects.get(pk=self.explicit_other_id)

        self.assertEqual(rolling_default.preferred_model, "")
        self.assertEqual(
            rolling_default.task_model_preferences,
            {"morning_briefing": DEEPSEEK_FLASH_MODEL},
        )
        self.assertEqual(rolling_default.pending_config_version, 5)
        self.assertEqual(explicit_flash.preferred_model, DEEPSEEK_FLASH_MODEL)
        self.assertEqual(explicit_flash.pending_config_version, 7)
        self.assertEqual(explicit_other.preferred_model, GEMMA_MODEL)
        self.assertEqual(explicit_other.pending_config_version, 9)
