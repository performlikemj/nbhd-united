"""Seed the canonical TopicRegistry rows for Gravity and Fuel.

The seed itself lives in ``apps/insights/seed.py`` so it stays a single
source of truth: this migration just invokes it, and the existing
``manage.py seed_topics`` command still works for ad-hoc reseeds.

Why a migration: ``seed_topics`` was added in Phase 0 but never ran on
production, which silently broke Phase 2's insight-recording path for
6 days (the agent's ``nbhd_insights_record`` calls couldn't resolve
slugs to TopicRegistry FK targets, so ``AssistantInsight`` rows never
accumulated, and the "What I remember" / "Topics I've learned" sections
of Horizons rendered empty). Folding the seed into a migration means
``manage.py migrate`` (which runs on every deploy) keeps the registry
in sync with whatever is declared in ``SEED_TOPICS``.

When you add a new pillar or topic later, update ``SEED_TOPICS`` and
add a sibling migration that calls ``seed_topics()`` again — the
underlying ``get_or_create`` is idempotent so existing rows are never
touched.
"""

from django.db import migrations


def forward(apps, schema_editor):
    # Local import — runs only when the migration applies. Avoids touching
    # Django app state at module import time.
    from apps.insights.seed import seed_topics

    seed_topics()


def reverse(apps, schema_editor):
    # No-op on reverse: removing canonical topics would orphan every
    # AssistantInsight / UserVoicePref FK that references them, and we
    # never want a migration rollback to silently corrupt user data.
    # If a topic needs to be retired, mark it deprecated in the registry
    # via a follow-up migration; don't delete the row.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("insights", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forward, reverse),
    ]
