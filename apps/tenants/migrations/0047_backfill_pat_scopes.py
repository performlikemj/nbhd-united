"""Backfill scopes on existing PATs.

Pre-this-migration the scopes JSONField was unenforced, so every PAT had
implicit full access. To preserve behavior for any token that already
exists, give scope-less PATs both ``sessions:write`` and ``sessions:read``.
"""

from django.db import migrations


def backfill_scopes(apps, schema_editor):
    PersonalAccessToken = apps.get_model("tenants", "PersonalAccessToken")
    PersonalAccessToken.objects.filter(scopes=[]).update(
        scopes=["sessions:write", "sessions:read"],
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0046_openclaw_version_4_25"),
    ]

    operations = [
        migrations.RunPython(backfill_scopes, noop_reverse),
    ]
