"""Backfill PlanSlot rows for every existing WorkoutPlan + back-link planned workouts.

Phase 2 of the durable plan-reconciler fix. After ``0012_plan_slot_and_edit_lock``
introduced the ``PlanSlot`` model, this migration:

1. Walks every existing ``WorkoutPlan`` and materializes one slot per
   ``(plan, week_index, weekday)`` derived from ``plan.schedule_json``.
2. Back-links every planned-status ``Workout`` whose ``(date, activity)``
   matches the slot's template entry. Workouts whose ``activity`` was
   user-edited away from the template stay ``slot=NULL`` — standalone
   overrides, which is fine.

The body lives in ``apps.fuel.services.backfill_plan_slots`` so the same
function is unit-testable under the live ORM in addition to running as
this migration's RunPython.

Idempotent: re-running is a no-op for slots that exist and for workouts
already linked.

Reverse: clears the FK links to NULL so a downgrade is safe. The slot
rows themselves stay — dropping them would break Phase 5 of the
reconciler work, which depends on slot identity persisting.
"""

from django.db import migrations


def _forwards(apps, schema_editor):
    # Delayed import so the migration module loads cleanly even if
    # apps.fuel.services pulls in anything model-touching in the future.
    from apps.fuel.services import backfill_plan_slots

    stats = backfill_plan_slots(
        apps.get_model("fuel", "WorkoutPlan"),
        apps.get_model("fuel", "PlanSlot"),
        apps.get_model("fuel", "Workout"),
    )
    print(
        "[backfill_plan_slots] "
        f"plans_skipped={stats['plans_skipped']} "
        f"slots_created={stats['slots_created']} "
        f"workouts_linked={stats['workouts_linked']} "
        f"workouts_skipped={stats['workouts_skipped']}"
    )


def _unlink_slots(apps, schema_editor):
    Workout = apps.get_model("fuel", "Workout")
    Workout.objects.filter(slot__isnull=False).update(slot=None)


class Migration(migrations.Migration):
    dependencies = [
        ("fuel", "0012_plan_slot_and_edit_lock"),
    ]

    operations = [
        migrations.RunPython(_forwards, _unlink_slots),
    ]
