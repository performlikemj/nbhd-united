"""Cleanup empty daily-note stubs minted before the daily-slug guard.

Before the ISO-date guard landed on the console write paths (commit fd81be7,
2026-02-24) and was generalised in ``apps.journal.path_validation`` (9ae5ac3),
a daily note could be created with a slug that isn't a real calendar date.
The web UI's date-navigation minted the literal slug ``NaN-NaN-NaN`` (an
Invalid-Date artifact — ``new Date("daily").getFullYear()`` is ``NaN``), and
early runtime/tooling writes seeded sibling stubs like ``template`` and
``AGENTS.md`` under ``kind=daily``.

These stubs are *empty*: because the slug wasn't a parseable date, the daily
template's date placeholder never rendered, so the body is the raw template
still containing the literal ``{{date}}`` token. ``memory_sync`` skips them on
every run (they fail ``DAILY_SLUG_RE``), and they pollute the agent's
recent-notes context. Scrub them.

SAFETY: scoped to the unrendered-placeholder shape so it can ONLY match empty
template stubs. A real daily note has an ISO-date slug (excluded here); a real
mis-kinded daily with content (e.g. ``2026-03-29-debt-chart``,
``memory/week-ahead/2026-W15``) has rendered text and never contains the literal
``{{date}}`` placeholder. Idempotent — a no-op once the stubs are gone.
"""

from django.db import migrations


def cleanup_nan_daily_stubs(apps, schema_editor):
    Document = apps.get_model("journal", "Document")

    stubs = (
        Document.objects.filter(kind="daily", markdown__contains="{{date}}")
        # Never touch a real ISO-date daily note, even in the (impossible) case
        # one carried an unrendered placeholder — belt and suspenders.
        .exclude(slug__regex=r"^\d{4}-\d{2}-\d{2}$")
    )
    count = stubs.count()
    if count:
        stubs.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0019_pendingextraction_goal_pendingextraction_task_and_more"),
    ]

    operations = [
        migrations.RunPython(cleanup_nan_daily_stubs, migrations.RunPython.noop),
    ]
