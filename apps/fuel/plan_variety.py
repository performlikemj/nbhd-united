"""Deterministic per-session-track variety validation for workout plans."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Any

from apps.common.llm_contracts import WEEKDAY_NAMES

from . import catalog

_DOSE_KEYS = frozenset(
    {
        "distance_km",
        "duration_minutes",
        "hold_s",
        "reps",
        "rest_s",
        "rounds",
        "rpe",
        "sets",
        "target_rpe",
        "weight",
        "work_s",
    }
)


@dataclass(frozen=True, slots=True)
class _WeekRecipe:
    week: int
    recipe: tuple[str, ...]
    dose: tuple[Any, ...]
    day: dict[str, Any]
    source: tuple[str | int, ...]


def _effective_week(schedule: dict, overrides: dict, week: int) -> tuple[dict, dict[str, tuple[str | int, ...]]]:
    effective = {key: value for key, value in schedule.items() if str(key).isdigit()}
    sources = {str(key): ("schedule_json", WEEKDAY_NAMES[int(key)]) for key in effective}
    override = overrides.get(str(week))
    if isinstance(override, dict):
        for day, value in override.items():
            day_key = str(day)
            if value is None:
                effective.pop(day_key, None)
                sources.pop(day_key, None)
            elif isinstance(value, dict):
                effective[day_key] = value
                sources[day_key] = ("week_overrides", str(week), WEEKDAY_NAMES[int(day_key)])
    return effective, sources


def _items(day: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    detail = day.get("detail_json")
    if not isinstance(detail, dict):
        return "exercises", []
    for container in ("exercises", "skills"):
        values = detail.get(container)
        if isinstance(values, list) and values:
            return container, [item for item in values if isinstance(item, dict)]
    return "exercises", []


def _recipe(day: dict[str, Any]) -> tuple[str, ...]:
    _container, items = _items(day)
    has_roles = any("role" in item for item in items)
    if has_roles:
        items = [item for item in items if item.get("role") == "accessory"]
        if not items:
            return ("__no_accessory__",)
    recipe = []
    for item in items:
        ref = item.get("catalog_ref")
        slug = ref.get("slug") if isinstance(ref, dict) else None
        identity = str(slug or catalog.normalize(item.get("name", ""))).strip()
        if identity:
            recipe.append(identity)
    return tuple(recipe)


def _dose(value: Any) -> tuple[Any, ...]:
    found: list[Any] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for child_key in sorted(node):
                child = node[child_key]
                if child_key in _DOSE_KEYS and not isinstance(child, dict | list):
                    found.append((child_key, child))
                walk(child, child_key)
        elif isinstance(node, list):
            if key == "sets":
                found.append(("set_count", len(node)))
            for child in node:
                walk(child, key)

    walk(value)
    return tuple(found)


def _longest_run(recipes: list[_WeekRecipe]) -> list[_WeekRecipe]:
    longest: list[_WeekRecipe] = []
    current: list[_WeekRecipe] = []
    for recipe in recipes:
        if current and recipe.recipe != current[-1].recipe:
            current = []
        current.append(recipe)
        if len(current) > len(longest):
            longest = list(current)
    return longest


def _catalog_candidates(offending: list[tuple[tuple[int, str], list[_WeekRecipe]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_slots: set[tuple[int, str, int]] = set()
    seen_names: set[str] = set()
    total = 0
    for (_day, _category), recipes in offending:
        for recipe in recipes:
            container, items = _items(recipe.day)
            for index, item in enumerate(items):
                if item.get("role") != "accessory":
                    continue
                slot = (_day, container, index)
                if slot in seen_slots:
                    continue
                seen_slots.add(slot)
                resolved = catalog.resolve_name(str(item.get("name") or ""))
                if resolved is None:
                    continue
                names = []
                for entry in sorted(catalog._catalog().entries, key=lambda candidate: candidate.name.casefold()):
                    if entry.slug == resolved.entry.slug:
                        continue
                    if entry.primaryMuscle.casefold() != resolved.entry.primaryMuscle.casefold():
                        continue
                    if entry.equipment.casefold() != resolved.entry.equipment.casefold():
                        continue
                    if entry.name in seen_names:
                        continue
                    seen_names.add(entry.name)
                    names.append(entry.name)
                    total += 1
                    if len(names) == 6 or total == 18:
                        break
                if names:
                    candidates.append(
                        {"loc": [*recipe.source, "detail_json", container, index, "name"], "names": names}
                    )
                if total == 18:
                    return candidates
    return candidates


def validate_plan_variety(
    schedule_json: dict,
    weeks: int,
    week_overrides: dict,
    *,
    variation_policy: str = "",
    repeat_policy: str = "",
    repeat_reason: str = "",
) -> dict[str, Any] | None:
    """Return a ``plan_rotation_required`` payload, or ``None`` when valid."""
    if weeks <= 4 or (repeat_policy == "intentional" and repeat_reason.strip()):
        return None

    tracks: dict[tuple[int, str], list[_WeekRecipe]] = defaultdict(list)
    for week in range(weeks):
        effective, sources = _effective_week(schedule_json, week_overrides, week)
        for day_key, day in effective.items():
            if not isinstance(day, dict):
                continue
            recipe = _recipe(day)
            if not recipe:
                continue
            day_int = int(day_key)
            category = str(day.get("category") or "other").strip().casefold()
            track_key = (day_int, category)
            tracks[track_key].append(
                _WeekRecipe(
                    week=week,
                    recipe=recipe,
                    dose=_dose(day),
                    day=day,
                    source=sources[day_key],
                )
            )

    offending: list[tuple[tuple[int, str], list[_WeekRecipe]]] = []
    track_payloads: list[dict[str, Any]] = []
    for track_key in sorted(tracks):
        recipes = tracks[track_key]
        if len(recipes) < 4:
            continue
        run = _longest_run(recipes)
        distinct_recipes = len({recipe.recipe for recipe in recipes})
        if len(run) <= 2 and distinct_recipes >= ceil(len(recipes) / 2):
            continue
        if variation_policy == "progression_only" and len({recipe.dose for recipe in recipes}) > 1:
            continue
        offending.append((track_key, recipes))
        track_payloads.append(
            {
                "weekday": WEEKDAY_NAMES[track_key[0]],
                "category": track_key[1],
                "weeks": [recipe.week + 1 for recipe in (run if len(run) > 2 else recipes)],
                "max_consecutive_same": len(run),
            }
        )

    if not offending:
        return None
    return {
        "error": "plan_rotation_required",
        "message": (
            "Plans longer than four weeks must rotate each recurring session recipe at least every two "
            "active weeks, or declare a validated progression/intentional-repeat policy."
        ),
        "tracks": track_payloads,
        "week_overrides_semantics": "whole_map_replacement",
        "catalog_candidates": _catalog_candidates(offending),
    }
