"""No-op — Celery Beat cleanup is no longer needed (replaced by QStash).

This migration originally removed django_celery_beat PeriodicTask rows.
Since django_celery_beat has been removed, it is now a no-op.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0004_register_celery_beat_schedules"),
    ]

    operations = []
