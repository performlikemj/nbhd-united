"""Collapse premium/byok tiers to starter and remove UserLLMConfig."""

from django.db import migrations, models


def collapse_tiers(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")

    # Move all non-starter tenants to starter
    updated = Tenant.objects.exclude(model_tier="starter").update(model_tier="starter")
    if updated:
        print(f"  Migrated {updated} tenant(s) to starter tier")

    # Clear preferred_model where it references non-OpenRouter models
    Tenant.objects.exclude(preferred_model="").exclude(
        preferred_model__startswith="openrouter/"
    ).update(preferred_model="")

    # Clear task_model_preferences entries referencing non-OpenRouter models
    for tenant in Tenant.objects.exclude(task_model_preferences={}):
        prefs = tenant.task_model_preferences or {}
        cleaned = {
            k: v for k, v in prefs.items()
            if v.startswith("openrouter/")
        }
        if cleaned != prefs:
            tenant.task_model_preferences = cleaned
            tenant.save(update_fields=["task_model_preferences"])


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0036_cron_jobs_snapshot"),
    ]

    operations = [
        migrations.RunPython(collapse_tiers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="tenant",
            name="model_tier",
            field=models.CharField(
                choices=[("starter", "Standard")],
                default="starter",
                max_length=20,
            ),
        ),
        migrations.DeleteModel(
            name="UserLLMConfig",
        ),
    ]
