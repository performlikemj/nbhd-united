"""Data migration: rename basic→starter, plus→premium in existing rows."""
from django.db import migrations


def rename_tiers_forward(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(model_tier="basic").update(model_tier="starter")
    Tenant.objects.filter(model_tier="plus").update(model_tier="premium")


def rename_tiers_reverse(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(model_tier="starter").update(model_tier="basic")
    Tenant.objects.filter(model_tier="premium").update(model_tier="plus")


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0008_add_userllmconfig"),
    ]

    operations = [
        migrations.RunPython(rename_tiers_forward, rename_tiers_reverse),
    ]
