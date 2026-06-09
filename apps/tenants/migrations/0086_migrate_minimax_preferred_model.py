"""Migrate tenants off the retired MiniMax M2.7 model.

MiniMax M2.7 was replaced by DeepSeek V4 Flash as the selectable "fast"
slot on 2026-06-09 — it's no longer in any tier's allowlist. Any tenant
whose ``preferred_model`` or ``task_model_preferences`` still points at
MiniMax would have that selection silently dropped at config-generation
time (the allowlist guard ignores out-of-list models, falling back to the
tier primary). Repoint those rows at DeepSeek V4 Flash so the user's
"fast model" intent is preserved.

Pure data migration — no schema/table changes, so it doesn't affect the
public-schema RLS relock ordering. No-op when no matching rows exist.
"""

from django.db import migrations

MINIMAX = "openrouter/minimax/minimax-m2.7"
FLASH = "openrouter/deepseek/deepseek-v4-flash"


def migrate_off_minimax(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")

    # Explicit primary-model selection.
    Tenant.objects.filter(preferred_model=MINIMAX).update(preferred_model=FLASH)

    # task_model_preferences is a JSON dict {cron_slug: model_id}; rewrite
    # any value still pinned to MiniMax. The fleet is small, so iterate.
    for tenant in Tenant.objects.all().iterator():
        prefs = tenant.task_model_preferences or {}
        if not isinstance(prefs, dict):
            continue
        changed = False
        for slug, model_id in list(prefs.items()):
            if model_id == MINIMAX:
                prefs[slug] = FLASH
                changed = True
        if changed:
            tenant.task_model_preferences = prefs
            tenant.save(update_fields=["task_model_preferences"])


def noop_reverse(apps, schema_editor):
    # Don't restore the retired model on rollback.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0085_relock_after_credits"),
    ]

    operations = [
        migrations.RunPython(migrate_off_minimax, noop_reverse),
    ]
