"""Phase 4 (#593) — stamp a valid ``type`` on every stored set.

Walks ``Workout.detail_json`` and ``WorkoutTemplate.detail_json`` and
gives each set in ``exercises[]`` / ``skills[]`` an explicit ``type``
(via the same field/registry coercer the write paths use). Only sets
missing a valid type change; all other keys (``est_1rm``, ``pr``,
``_normalized``, cardio fields, …) are preserved untouched.

``WorkoutPlan.schedule_json`` is intentionally **not** transformed here
— its nested "workout definition" shape is surveyed read-only by the
``fuel_set_type_dryrun`` command first; a follow-up handles it only if
the survey shows set-shaped data, rather than blind-transforming it.

Reverse is a deliberate no-op: the runtime accessor (``set_metric``) is
shape-agnostic, so un-stamping is unnecessary for a rollback to be
safe, and stripping ``type`` would clobber rows written with an
intentional type after deploy.
"""

from django.db import migrations


def stamp_set_types(apps, schema_editor):
    from apps.fuel.set_contract import _coerce_container

    for model_name in ("Workout", "WorkoutTemplate"):
        Model = apps.get_model("fuel", model_name)
        for obj in Model.objects.all().iterator():
            detail = obj.detail_json
            if not isinstance(detail, dict):
                continue
            new_detail = _coerce_container(detail)
            if new_detail != detail:
                obj.detail_json = new_detail
                obj.save(update_fields=["detail_json"])


class Migration(migrations.Migration):
    dependencies = [
        ("fuel", "0009_use_session_scheduling_flag"),
    ]

    operations = [
        migrations.RunPython(stamp_set_types, migrations.RunPython.noop),
    ]
