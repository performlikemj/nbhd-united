"""Turn on friends_agent_propose_enabled for the two launch-flagged tenants (PR9).

The flag defaults False (absorb-only), which is the new baseline. MJ + Kiho are
the pair already living with agent-propose behavior (see CONTINUITY), so this
preserves their current experience across the default flip. Guarded by
``id__in`` filter → idempotent and a no-op wherever those tenants don't exist
(e.g. CI / a fresh DB).
"""

from django.db import migrations

FLAGGED_TENANT_IDS = [
    "148ccf1c-ef13-47f8-ada1-a98fa90e14a0",  # MJ
    "13fa39df-74b6-4b17-b41e-ea0fc400fb13",  # Kiho
]


def enable_for_flagged(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(id__in=FLAGGED_TENANT_IDS).update(friends_agent_propose_enabled=True)


def disable_for_flagged(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(id__in=FLAGGED_TENANT_IDS).update(friends_agent_propose_enabled=False)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0104_tenant_friends_agent_propose_enabled"),
    ]

    operations = [
        migrations.RunPython(enable_for_flagged, disable_for_flagged),
    ]
