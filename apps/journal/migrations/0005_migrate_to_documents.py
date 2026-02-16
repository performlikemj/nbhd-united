"""Migrate existing DailyNote, UserMemory, WeeklyReview data into Document model."""
from __future__ import annotations

import uuid
from django.db import migrations


def migrate_data_forward(apps, schema_editor):
    Document = apps.get_model("journal", "Document")
    DailyNote = apps.get_model("journal", "DailyNote")
    UserMemory = apps.get_model("journal", "UserMemory")
    WeeklyReview = apps.get_model("journal", "WeeklyReview")
    JournalEntry = apps.get_model("journal", "JournalEntry")

    # Migrate DailyNote → Document(kind="daily")
    for note in DailyNote.objects.all():
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        weekday = weekday_names[note.date.weekday()]
        Document.objects.get_or_create(
            tenant=note.tenant,
            kind="daily",
            slug=str(note.date),
            defaults={
                "id": uuid.uuid4(),
                "title": f"{note.date} ({weekday})",
                "markdown": note.markdown or "",
            },
        )

    # Migrate UserMemory → Document(kind="memory")
    for mem in UserMemory.objects.all():
        Document.objects.get_or_create(
            tenant=mem.tenant,
            kind="memory",
            slug="memory",
            defaults={
                "id": uuid.uuid4(),
                "title": "Memory",
                "markdown": mem.markdown or "",
            },
        )

    # Migrate WeeklyReview → Document(kind="weekly")
    for review in WeeklyReview.objects.all():
        slug = str(review.week_start)
        # Build markdown from structured fields
        lines = [f"# Weekly Review — {review.week_start} to {review.week_end}"]
        if review.mood_summary:
            lines.append(f"\n## Mood Summary\n{review.mood_summary}")
        if review.top_wins:
            lines.append("\n## 🏆 Wins")
            for w in review.top_wins:
                lines.append(f"- {w}")
        if review.top_challenges:
            lines.append("\n## ❌ Challenges")
            for c in review.top_challenges:
                lines.append(f"- {c}")
        if review.lessons:
            lines.append("\n## 📚 Lessons")
            for l in review.lessons:
                lines.append(f"- {l}")
        if review.week_rating:
            lines.append(f"\n## Rating: {review.week_rating}")
        if review.intentions_next_week:
            lines.append("\n## 📅 Next Week")
            for i in review.intentions_next_week:
                lines.append(f"- {i}")
        if review.raw_text:
            lines.append(f"\n---\n{review.raw_text}")

        Document.objects.get_or_create(
            tenant=review.tenant,
            kind="weekly",
            slug=slug,
            defaults={
                "id": uuid.uuid4(),
                "title": f"Weekly Review — {review.week_start}",
                "markdown": "\n".join(lines),
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("journal", "0004_document"),
    ]

    operations = [
        migrations.RunPython(migrate_data_forward, migrations.RunPython.noop),
    ]
