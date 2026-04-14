"""Remove evening-check-in from default note templates.

The evening section was appearing in daily notes at creation time (morning),
confusing users who saw it before evening. The evening cron now creates
the section on-demand when it actually runs.
"""

from django.db import migrations


def remove_evening_section(apps, schema_editor):
    NoteTemplate = apps.get_model("journal", "NoteTemplate")
    db_alias = schema_editor.connection.alias

    for template in NoteTemplate.objects.using(db_alias).all():
        sections = template.sections or []
        original_len = len(sections)
        sections = [s for s in sections if s.get("slug") != "evening-check-in"]
        if len(sections) < original_len:
            template.sections = sections
            template.save(update_fields=["sections"])


def add_evening_section_back(apps, schema_editor):
    NoteTemplate = apps.get_model("journal", "NoteTemplate")
    db_alias = schema_editor.connection.alias

    evening_section = {
        "slug": "evening-check-in",
        "title": "Evening Check-in",
        "content": (
            "### What got done today?\n"
            "- \n\n"
            "### What didn't get done? Why?\n"
            "- \n\n"
            "### Plan for tomorrow (top 3)\n"
            "1. \n2. \n3. \n\n"
            "### Blockers or decisions needed?\n"
            "- \n\n"
            "### Energy/mood (1-10)\n"
            "- "
        ),
        "source": "human",
    }

    for template in NoteTemplate.objects.using(db_alias).all():
        sections = template.sections or []
        if not any(s.get("slug") == "evening-check-in" for s in sections):
            sections.append(evening_section)
            template.sections = sections
            template.save(update_fields=["sections"])


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0009_bootstrap_goals"),
    ]

    operations = [
        migrations.RunPython(remove_evening_section, add_evening_section_back),
    ]
