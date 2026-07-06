"""Backfill Lesson.pillar from the tag heuristic (PR9).

One-time data migration. Uses the SAME pure classifier the runtime now uses
(``apps.lessons.pillars.infer_pillar_from_tags``) so the backfilled values match
what ``Lesson.save`` would fill going forward. Mirrors ``save()``'s condition:
only tagged lessons get a value; a tagless lesson stays blank (blank = "derive
from tags at read time"). The friends share-block re-checks the tag heuristic
regardless, so this is provenance, not the safety boundary.
"""

from django.db import migrations


def backfill_pillar(apps, schema_editor):
    Lesson = apps.get_model("lessons", "Lesson")
    from apps.lessons.pillars import infer_pillar_from_tags

    for lesson in Lesson.objects.filter(pillar="").only("id", "tags").iterator():
        if not lesson.tags:
            continue  # leave tagless lessons blank, matching Lesson.save()
        Lesson.objects.filter(id=lesson.id).update(pillar=infer_pillar_from_tags(lesson.tags))


class Migration(migrations.Migration):
    dependencies = [
        ("lessons", "0005_lesson_pillar"),
    ]

    operations = [
        migrations.RunPython(backfill_pillar, migrations.RunPython.noop),
    ]
