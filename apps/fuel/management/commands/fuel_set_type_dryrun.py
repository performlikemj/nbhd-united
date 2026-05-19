"""Read-only blast-radius report for the Phase 4 (#593) set-type stamp.

Run this against production *before* applying migration 0010 to see
exactly how many rows/sets would change and to survey whether
``WorkoutPlan.schedule_json`` carries set-shaped data (which 0010 does
not transform). Writes nothing.

    python manage.py fuel_set_type_dryrun
"""

from collections import Counter

from django.core.management.base import BaseCommand

from apps.fuel.models import Workout, WorkoutPlan, WorkoutTemplate
from apps.fuel.set_contract import _coerce_container


def _set_stats(detail: dict) -> tuple[int, int, Counter]:
    """(total_sets, sets_changed, type_distribution) for one detail dict."""
    total = 0
    changed = 0
    dist: Counter = Counter()
    for key in ("exercises", "skills"):
        container = detail.get(key)
        if not isinstance(container, list):
            continue
        for ex in container:
            if not isinstance(ex, dict) or not isinstance(ex.get("sets"), list):
                continue
            for s in ex["sets"]:
                if not isinstance(s, dict):
                    continue
                total += 1
                before = s.get("type")
                after = _coerce_container({"exercises": [{"name": ex.get("name", ""), "sets": [s]}]})["exercises"][0][
                    "sets"
                ][0]["type"]
                dist[after] += 1
                if before != after:
                    changed += 1
    return total, changed, dist


class Command(BaseCommand):
    help = "Read-only report of what migration 0010 would stamp (no writes)."

    def handle(self, *args, **options):
        grand = Counter()
        for label, Model in (("Workout", Workout), ("WorkoutTemplate", WorkoutTemplate)):
            rows = sets = changed_sets = 0
            rows_changed = 0
            dist: Counter = Counter()
            for obj in Model.objects.all().iterator():
                rows += 1
                d = obj.detail_json
                if not isinstance(d, dict):
                    continue
                t, c, dd = _set_stats(d)
                sets += t
                changed_sets += c
                dist.update(dd)
                if _coerce_container(d) != d:
                    rows_changed += 1
            grand.update(dist)
            self.stdout.write(
                f"{label}: rows={rows} rows_to_update={rows_changed} "
                f"sets={sets} sets_restamped={changed_sets} "
                f"resulting_types={dict(dist)}"
            )

        # Survey (no mutation) — does schedule_json carry set-shaped data?
        plans = set_bearing = 0
        for plan in WorkoutPlan.objects.all().iterator():
            plans += 1
            sched = plan.schedule_json
            if not isinstance(sched, dict):
                continue
            for val in sched.values():
                if isinstance(val, dict) and (
                    isinstance(val.get("exercises"), list) or isinstance(val.get("skills"), list)
                ):
                    set_bearing += 1
                    break
        self.stdout.write(
            f"WorkoutPlan.schedule_json: plans={plans} "
            f"set_bearing_definitions={set_bearing} "
            f"(NOT transformed by 0010 — follow-up only if >0)"
        )
        self.stdout.write(self.style.SUCCESS(f"DRY RUN — no writes. Grand type totals: {dict(grand)}"))
