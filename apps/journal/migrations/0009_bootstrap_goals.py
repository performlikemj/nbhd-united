"""Bootstrap goals document for existing tenants.

Creates goals doc if missing. Replaces unmodified old-format docs.
Leaves customized docs alone.
"""

from django.db import migrations

NEW_GOALS_MD = "# Goals\n\n## Active\n\n(No goals yet — your agent will suggest them as you chat.)\n\n## Completed\n"

# Old seed template content — if a doc matches this exactly, it's unmodified
OLD_GOALS_MD = "# Goals\n\n## Short-term goals\n- What can you finish soon?\n- Small win for this week:\n\n## Long-term goals\n- What would you be proud of in a few months?\n\nKeep these simple and update when your focus changes.\n"


def bootstrap_goals(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Document = apps.get_model("journal", "Document")

    for tenant in Tenant.objects.all():
        doc = Document.objects.filter(tenant=tenant, kind="goal", slug="goals").first()

        if doc is None:
            # No goals doc — create one
            Document.objects.create(
                tenant=tenant,
                kind="goal",
                slug="goals",
                title="Goals",
                markdown=NEW_GOALS_MD,
            )
        elif doc.markdown.strip() == OLD_GOALS_MD.strip():
            # Old unmodified template — update to new format
            doc.markdown = NEW_GOALS_MD
            doc.save(update_fields=["markdown"])
        # else: customized — leave it alone


def reverse_bootstrap(apps, schema_editor):
    pass  # No reverse — can't distinguish what was there before


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0008_documentchunk"),
        ("tenants", "0016_add_pending_deletion_fields"),
    ]

    operations = [
        migrations.RunPython(bootstrap_goals, reverse_bootstrap),
    ]
