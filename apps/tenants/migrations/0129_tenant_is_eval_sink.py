"""Add ``Tenant.is_eval_sink`` and classify the existing eval tenants once.

``is_eval_sink`` is the dedicated gate for eval-sink behavior (delivery
suppression, memory/digest/proactive-context exclusion, eval target guards).
It replaces the previous overload of ``is_synthetic``, which means ONLY
"excluded from business-facing aggregates" and must never gate assistant-facing
behavior — see ``docs/evals-directive.md``.

Backfill scope is deliberately narrow: only tenants whose user email ends in
``@evals.invalid`` (the eval harness's own domain) flip to ``True``. Synthetic
tenants with real-looking emails — notably the App Store Review demo account —
stay ``False`` and keep normal assistant behavior.

Renumbered: authored as ``0123_tenant_is_eval_sink`` / ``0124_relock_after_is_eval_sink``,
finally landed as ``0129``/``0130`` to stack after PR #1198's typed-crons pair,
which merged to main as ``0127_alter_tenant_experimental_typed_crons`` /
``0128_relock_after_typed_crons_default``.
"""

from django.db import migrations, models


def backfill_existing_eval_sinks(apps, schema_editor):
    """Classify the existing eval tenants once; the boolean is authoritative after this."""
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(user__email__iendswith="@evals.invalid").update(is_eval_sink=True)


def clear_backfilled_eval_sinks(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(user__email__iendswith="@evals.invalid").update(is_eval_sink=False)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0128_relock_after_typed_crons_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="is_eval_sink",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Dedicated eval delivery/memory sink. When enabled, outbound messages "
                    "are recorded as eval evidence but are not sent to user transports or "
                    "surfaced in user/model history. Independent of is_synthetic: synthetic "
                    "demo accounts keep normal assistant behavior unless explicitly enabled."
                ),
            ),
        ),
        migrations.RunPython(backfill_existing_eval_sinks, clear_backfilled_eval_sinks),
    ]
