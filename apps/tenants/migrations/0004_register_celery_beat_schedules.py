"""No-op — Celery Beat schedules are no longer used (replaced by QStash).

This migration originally created django_celery_beat PeriodicTask rows.
Since django_celery_beat has been removed, it is now a no-op.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0003_telegramlinktoken"),
    ]

    operations = []
