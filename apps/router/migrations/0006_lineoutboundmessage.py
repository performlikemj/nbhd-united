import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("router", "0005_processedinboundevent"),
        ("tenants", "0067_postgres_cron_canonical_default_true"),
    ]

    operations = [
        migrations.CreateModel(
            name="LineOutboundMessage",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("line_user_id", models.CharField(max_length=128)),
                (
                    "line_message_id",
                    models.CharField(
                        help_text="ID returned by LINE's push/reply API in sentMessages[].id.",
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "text_excerpt",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="First ~500 chars of the message we sent — used as the quoted excerpt.",
                    ),
                ),
                ("sent_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="line_outbound_messages",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "line_outbound_messages",
                "ordering": ["-sent_at"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "line_user_id", "-sent_at"],
                        name="line_outb_tenant_user_idx",
                    ),
                ],
            },
        ),
        # Enable RLS to match the public-schema lockdown invariant
        # (tenants/0059 + tenants/0066). Test
        # ``test_rls_enabled_on_owned_public_tables`` will fail otherwise.
        # No policies are defined here: the Django backend connects as the
        # table owner and bypasses RLS by default, while the anon role
        # PostgREST exposes has no SELECT/INSERT/UPDATE grant on this
        # table — locking it down structurally rather than via policy.
        migrations.RunSQL(
            sql="ALTER TABLE line_outbound_messages ENABLE ROW LEVEL SECURITY;",
            # Intentionally NOT disabling RLS on reverse — would re-expose
            # the table via PostgREST/anon. Mirrors the reverse stance in
            # tenants/0066_relock_public_schema_rls.py. The table itself
            # is dropped by the reverse of CreateModel anyway, so this
            # RunSQL has nothing to undo.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
