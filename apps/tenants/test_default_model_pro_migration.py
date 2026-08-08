"""Regression tests for the DeepSeek V4 Pro default config bump."""

from __future__ import annotations

import importlib

from django.test import TestCase

from apps.billing.constants import DEEPSEEK_FLASH_MODEL, GEMMA_MODEL
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class DefaultModelProMigrationTest(TestCase):
    def setUp(self):
        self.rolling_default = create_tenant(display_name="Rolling Default", telegram_chat_id=830001)
        self.explicit_flash = create_tenant(display_name="Explicit Flash", telegram_chat_id=830002)
        self.explicit_other = create_tenant(display_name="Explicit Other", telegram_chat_id=830003)

        Tenant.objects.filter(id=self.rolling_default.id).update(
            preferred_model="",
            task_model_preferences={"morning_briefing": DEEPSEEK_FLASH_MODEL},
            pending_config_version=4,
        )
        Tenant.objects.filter(id=self.explicit_flash.id).update(
            preferred_model=DEEPSEEK_FLASH_MODEL,
            pending_config_version=7,
        )
        Tenant.objects.filter(id=self.explicit_other.id).update(
            preferred_model=GEMMA_MODEL,
            pending_config_version=9,
        )

    def _run_forward(self) -> None:
        migration = importlib.import_module("apps.tenants.migrations.0146_bump_tier_default_tenants_for_pro")

        class _AppsStub:
            @staticmethod
            def get_model(app_label, model_name):
                return Tenant

        migration.bump_tier_default_tenants(_AppsStub(), schema_editor=None)

    def test_bumps_only_rolling_default_tenants(self):
        self._run_forward()

        self.rolling_default.refresh_from_db()
        self.explicit_flash.refresh_from_db()
        self.explicit_other.refresh_from_db()

        self.assertEqual(self.rolling_default.preferred_model, "")
        self.assertEqual(
            self.rolling_default.task_model_preferences,
            {"morning_briefing": DEEPSEEK_FLASH_MODEL},
        )
        self.assertEqual(self.rolling_default.pending_config_version, 5)
        self.assertEqual(self.explicit_flash.preferred_model, DEEPSEEK_FLASH_MODEL)
        self.assertEqual(self.explicit_flash.pending_config_version, 7)
        self.assertEqual(self.explicit_other.preferred_model, GEMMA_MODEL)
        self.assertEqual(self.explicit_other.pending_config_version, 9)
