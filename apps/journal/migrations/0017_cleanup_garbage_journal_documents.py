"""Cleanup garbage journal_document rows seeded by pre-validation runtime endpoints.

Two row shapes existed on the canary tenant before the path-injection guard
landed in ``apps/journal/path_validation.py``:

1. ``kind=':' slug=':'`` — created 2026-04-08 via ``nbhd_document_put`` with
   literal colons. Produces SMB path ``memory/journal/:/:.md`` which NTFS
   rejects (``:`` is the alternate data stream separator).
2. ``kind='cron' slug='_sync:<job>'`` — created 2026-05-08 when the agent
   misrouted a Phase 2 sync into ``nbhd_document_put`` instead of
   ``cron.add``. ``cron`` is not a valid ``Document.Kind`` choice, and the
   ``_sync:`` prefix carries the NTFS-hostile colon.

Both shapes are now blocked at the endpoint (see
``apps.journal.path_validation.validate_kind_slug``). This migration scrubs
the existing rows so ``sync_documents_to_workspace`` stops grinding against
their bad SMB paths.

Idempotent — once the bad rows are gone, the next run is a no-op. Scoped to
the exact bad shapes; does NOT touch legitimate rows like
``kind=memory slug=week-ahead/2026-W14``.
"""

from django.db import migrations


def cleanup_invalid_documents(apps, schema_editor):
    Document = apps.get_model("journal", "Document")

    # Shape 1: rows whose kind contains a colon (NTFS-hostile) or is outside
    # the canonical Document.Kind enum. The enum at time of writing:
    # daily, weekly, monthly, goal, project, tasks, ideas, memory.
    valid_kinds = {"daily", "weekly", "monthly", "goal", "project", "tasks", "ideas", "memory"}
    bad_kind = Document.objects.exclude(kind__in=valid_kinds)
    bad_kind_count = bad_kind.count()
    if bad_kind_count:
        bad_kind.delete()

    # Shape 2: rows with NTFS-hostile chars in the slug. Limited to ``:`` and
    # ``\`` — these are the actual failure-causers on Azure SMB. Legitimate
    # slugs use ``/`` and ``-`` which are fine.
    bad_slug = Document.objects.filter(slug__regex=r"[:\\]")
    bad_slug_count = bad_slug.count()
    if bad_slug_count:
        bad_slug.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0016_document_intent_status_document_pillar_and_more"),
    ]

    operations = [
        migrations.RunPython(cleanup_invalid_documents, migrations.RunPython.noop),
    ]
