"""Repoint existing DeepSeek V4 Flash selections to the 0731 snapshot.

The unversioned Flash id is no longer in the runtime allowlist. Without this
rewrite, an existing ``preferred_model`` or per-task override would be silently
ignored and fall back to the tier primary. Bump pending config once per changed
tenant so fleet reconciliation re-renders both primary and cron model choices.

Pure data migration — no schema/table changes, so it doesn't affect the
public-schema RLS relock ordering. No-op when no matching rows exist.
"""

from django.db import migrations

LEGACY_FLASH = "openrouter/deepseek/deepseek-v4-flash"
FLASH_0731 = "openrouter/deepseek/deepseek-v4-flash-0731"


def migrate_flash_snapshot(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")

    for tenant in Tenant.objects.all().iterator():
        changed = False
        if tenant.preferred_model == LEGACY_FLASH:
            tenant.preferred_model = FLASH_0731
            changed = True

        prefs = tenant.task_model_preferences or {}
        if isinstance(prefs, dict):
            new_prefs = {
                slug: (FLASH_0731 if model_id == LEGACY_FLASH else model_id) for slug, model_id in prefs.items()
            }
            if new_prefs != prefs:
                tenant.task_model_preferences = new_prefs
                changed = True

        if changed:
            tenant.pending_config_version = (tenant.pending_config_version or 0) + 1
            tenant.save(
                update_fields=[
                    "preferred_model",
                    "task_model_preferences",
                    "pending_config_version",
                ]
            )


def noop_reverse(apps, schema_editor):
    # Don't restore the out-of-allowlist model on rollback.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0140_relock_after_apple_auth"),
    ]

    operations = [
        migrations.RunPython(migrate_flash_snapshot, noop_reverse),
    ]
