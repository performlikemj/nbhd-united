"""Flip ``postgres_cron_canonical`` default to True for newly-created tenants.

Migration 0058 set ``postgres_cron_canonical=True`` for every existing tenant
and the docstring declared canonical "the single architecture for system cron
payload state." The model default was missed in that PR, so new tenants
created via ``apps/tenants/services.py`` (Telegram bootstrap) and
``apps/tenants/views.py`` (trial signup) landed on the legacy gateway-only
branch of ``seed_cron_jobs``: OpenClaw got crons via ``cron.add`` but no
CronJob Postgres rows were created. Consequences for those tenants:
the post_save signal short-circuited, the hourly fleet reconciler skipped
them (filters to canonical=True), reactivation refresh skipped them, and
the dashboard "Scheduled Tasks" view (which reads from CronJob) showed
empty. Worst case: a transient gateway failure at provision T+60s left
them with zero crons.

This migration is metadata-only — Django stores BooleanField defaults
Python-side, no DB column change. Existing rows are unaffected (all 24
already True via 0058).
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0066_relock_public_schema_rls"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="postgres_cron_canonical",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Cutover flag for the Postgres-canonical cron model. "
                    "When True, the CronJob table is the source of truth and "
                    "OpenClaw's SQLite is a derived view kept in sync by the "
                    "regenerate_tenant_crons reconciler."
                ),
            ),
        ),
    ]
