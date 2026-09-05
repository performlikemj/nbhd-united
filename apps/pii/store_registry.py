"""Registry of placeholder-bearing persistence surfaces."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, cached_property
from typing import Any

from django.apps import apps

from apps.common.llm_lookups import (
    CARDIO_EFFORTS,
    CARDIO_KINDS,
    CARDIO_PACE_REGEX,
    CARDIO_RECOVERY_EFFORTS,
    CARDIO_TERRAINS,
)


@dataclass(frozen=True)
class PlaceholderStore:
    """One placeholder-bearing model surface.

    ``json_paths`` use dotted paths beginning with the model JSONField name;
    ``*``, ``[]``, and ``[*]`` fan out over mapping values or list items;
    ``**`` selects every descendant string in a genuinely free-form payload.
    """

    model_label: str
    flat_fields: tuple[str, ...]
    json_paths: tuple[str, ...]
    receipts_field: str
    json_exclude_paths: tuple[str, ...] = ()

    @property
    def model(self):
        return apps.get_model(self.model_label)

    @property
    def json_fields(self) -> tuple[str, ...]:
        """Top-level JSONField names, in registry order without duplicates."""
        return tuple(dict.fromkeys(parts[0] for path in self.json_paths if (parts := json_path_parts(path))))

    @property
    def receipt_fields(self) -> tuple[str, ...]:
        """Receipt keys that can make a row eligible for repair."""
        return tuple(dict.fromkeys((*self.flat_fields, *self.json_fields)))

    def nested_json_paths(self, field: str) -> tuple[tuple[str, ...], ...]:
        """Parsed path suffixes registered below one top-level JSONField."""
        return tuple(parts[1:] for path in self.json_paths if (parts := json_path_parts(path)) and parts[0] == field)

    @cached_property
    def _compiled_exclusions(self):
        parsed = tuple(json_path_parts(path) for path in self.json_exclude_paths)
        return {
            field: tuple(parts[1:] for parts in parsed if parts and parts[0] == field) for field in self.json_fields
        }

    def nested_json_exclusions(self, field: str) -> tuple[tuple[str, ...], ...]:
        return self._compiled_exclusions.get(field, ())


CARDIO_MACHINE_PATHS = (
    "segments[].kind",
    "segments[].effort",
    "segments[].target_pace",
    "segments[].recovery.effort",
    "terrain",
)


def _path_matches(path, pattern):
    return len(path) == len(pattern) and all(
        expected == "*" or expected == str(actual) for actual, expected in zip(path, pattern)
    )


def _valid_cardio_scalar(path, value):
    if not isinstance(value, str) or not path:
        return False
    key = path[-1]
    if key == "target_pace":
        return _CARDIO_PACE.fullmatch(value) is not None
    return value in _CARDIO_VALUES.get(key, ())


def is_cardio_machine_path(path, value):
    """Only recognized Fuel response locations and valid machine scalars skip PII."""
    if not _valid_cardio_scalar(path, value):
        return False
    return any(_path_matches(path, pattern) for pattern in _CARDIO_RESPONSE_PATHS)


@cache
def json_path_parts(path: str) -> tuple[str, ...]:
    """Parse dotted paths; ``[]`` and ``[*]`` are aliases for ``*``.

    This is the single parser for every registry consumer. A registry path
    such as ``evidence[].note`` therefore selects the same leaves in live
    authoring, repair, migration, and junk healing.
    """
    normalized = path.replace("[*]", ".*").replace("[]", ".*")
    return tuple(part for part in normalized.split(".") if part)


_CARDIO_PACE = re.compile(CARDIO_PACE_REGEX)
_CARDIO_VALUES = {
    "kind": frozenset(CARDIO_KINDS),
    "effort": frozenset((*CARDIO_EFFORTS, *CARDIO_RECOVERY_EFFORTS)),
    "terrain": frozenset(CARDIO_TERRAINS),
}
_CARDIO_RESPONSE_PATHS = tuple(
    json_path_parts(".".join(part for part in (wrapper, location, leaf) if part))
    for wrapper in (
        "",
        "workout",
        "workouts.*",
        "plan",
        "plans.*",
        "template",
        "templates.*",
        "data",
        "data.workout",
        "data.plan",
    )
    for location in ("detail_json", "schedule_json.*.detail_json", "week_overrides.*.*.detail_json")
    for leaf in CARDIO_MACHINE_PATHS
)


def rewrite_json_path(
    value: Any,
    parts: tuple[str, ...],
    transform: Callable[[str], str],
    *,
    exclude_paths: tuple[tuple[str, ...], ...] = (),
    _path: tuple = (),
) -> tuple[Any, bool]:
    """Copy-on-write transform of string leaves selected by one parsed path."""
    if _valid_cardio_scalar(_path, value) and any(_path_matches(_path, pattern) for pattern in exclude_paths):
        return value, False
    if not parts:
        if not isinstance(value, str):
            return value, False
        rewritten = transform(value)
        return rewritten, rewritten != value

    head, *tail_list = parts
    tail = tuple(tail_list)
    if head == "**":
        if tail:
            raise ValueError("** must be the final JSON path component")
        if isinstance(value, str):
            rewritten = transform(value)
            return rewritten, rewritten != value
        if isinstance(value, dict):
            next_value = value
            changed = False
            for key, child in value.items():
                rewritten_child, child_changed = rewrite_json_path(
                    child, ("**",), transform, exclude_paths=exclude_paths, _path=(*_path, key)
                )
                if child_changed:
                    if not changed:
                        next_value = dict(value)
                    next_value[key] = rewritten_child
                    changed = True
            return next_value, changed
        if isinstance(value, list):
            next_value = value
            changed = False
            for index, child in enumerate(value):
                rewritten_child, child_changed = rewrite_json_path(
                    child, ("**",), transform, exclude_paths=exclude_paths, _path=(*_path, index)
                )
                if child_changed:
                    if not changed:
                        next_value = list(value)
                    next_value[index] = rewritten_child
                    changed = True
            return next_value, changed
        return value, False

    if head == "*":
        if isinstance(value, dict):
            next_value = value
            changed = False
            for key, child in value.items():
                rewritten_child, child_changed = rewrite_json_path(
                    child, tail, transform, exclude_paths=exclude_paths, _path=(*_path, key)
                )
                if child_changed:
                    if not changed:
                        next_value = dict(value)
                    next_value[key] = rewritten_child
                    changed = True
            return next_value, changed
        if isinstance(value, list):
            next_value = value
            changed = False
            for index, child in enumerate(value):
                rewritten_child, child_changed = rewrite_json_path(
                    child, tail, transform, exclude_paths=exclude_paths, _path=(*_path, index)
                )
                if child_changed:
                    if not changed:
                        next_value = list(value)
                    next_value[index] = rewritten_child
                    changed = True
            return next_value, changed
        return value, False

    if isinstance(value, dict) and head in value:
        rewritten_child, changed = rewrite_json_path(
            value[head], tail, transform, exclude_paths=exclude_paths, _path=(*_path, head)
        )
        if changed:
            next_value = dict(value)
            next_value[head] = rewritten_child
            return next_value, True
        return value, False
    if isinstance(value, list) and head.isdigit():
        index = int(head)
        if 0 <= index < len(value):
            rewritten_child, changed = rewrite_json_path(
                value[index], tail, transform, exclude_paths=exclude_paths, _path=(*_path, index)
            )
            if changed:
                next_value = list(value)
                next_value[index] = rewritten_child
                return next_value, True
    return value, False


_STORES = (
    PlaceholderStore(
        model_label="journal.Task",
        flat_fields=("title", "description"),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.Goal",
        flat_fields=("title", "description"),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.PendingTaskAction",
        flat_fields=("evidence",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    # ── P3 W2b: the Document family ──────────────────────────────────────
    # ``target`` (Document) stays OUT: it is structured lifecycle metadata, and
    # enc_columns.py excludes it from encryption on the same grounds.
    PlaceholderStore(
        model_label="journal.Document",
        flat_fields=("title", "markdown"),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.DocumentChunk",
        flat_fields=("text",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.DocumentIngestion",
        flat_fields=("original_filename",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.DocumentIngestionArtifact",
        flat_fields=("content_excerpt",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    # ── P3 W3a: legacy journal + first nested-JSON stores ──────────────
    PlaceholderStore(
        model_label="journal.DailyNote",
        flat_fields=("markdown",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.JournalEntry",
        flat_fields=("mood", "reflection", "raw_text"),
        json_paths=("wins[]", "challenges[]"),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.WeeklyReview",
        flat_fields=("mood_summary", "raw_text"),
        json_paths=("top_wins[]", "top_challenges[]", "lessons[]", "intentions_next_week[]"),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.Purpose",
        flat_fields=("statement",),
        json_paths=("evidence[].note",),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.PendingExtraction",
        flat_fields=("text",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    # ── P3 W3b: flat long tail + explicit free-form payloads ───────────
    PlaceholderStore(
        model_label="lessons.Lesson",
        flat_fields=("text", "context", "galaxy_note"),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="lessons.StarJournalEntry",
        flat_fields=("text",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="fuel.WorkoutPlan",
        flat_fields=("name", "notes", "objective"),
        json_paths=("schedule_json.**", "week_overrides.**"),
        receipts_field="pii_receipts",
        json_exclude_paths=tuple(
            f"{prefix}.{path}"
            for prefix in ("schedule_json.*.detail_json", "week_overrides.*.*.detail_json")
            for path in CARDIO_MACHINE_PATHS
        ),
    ),
    PlaceholderStore(
        model_label="fuel.Workout",
        flat_fields=("skip_reason", "activity", "notes"),
        json_paths=("notes_thread[].text", "detail_json.**"),
        receipts_field="pii_receipts",
        json_exclude_paths=tuple(f"{prefix}.{path}" for prefix in ("detail_json",) for path in CARDIO_MACHINE_PATHS),
    ),
    PlaceholderStore(
        model_label="fuel.FuelProfile",
        flat_fields=("additional_context",),
        json_paths=("limitations[]",),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="fuel.WorkoutTemplate",
        flat_fields=("name",),
        json_paths=("detail_json.**",),
        receipts_field="pii_receipts",
        json_exclude_paths=tuple(f"{prefix}.{path}" for prefix in ("detail_json",) for path in CARDIO_MACHINE_PATHS),
    ),
    PlaceholderStore(
        model_label="fuel.SleepLog",
        flat_fields=("notes",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="datebook.MirrorEvent",
        flat_fields=("title", "location", "notes", "calendar_title", "source_title"),
        json_paths=(
            "staged_payload.title",
            "staged_payload.location",
            "staged_payload.notes",
            "staged_payload.calendar_title",
            "staged_payload.source_title",
        ),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="datebook.MirrorReminder",
        flat_fields=("title", "location", "notes", "list_title", "source_title"),
        json_paths=(
            "staged_payload.title",
            "staged_payload.location",
            "staged_payload.notes",
            "staged_payload.list_title",
            "staged_payload.source_title",
        ),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="datebook.CalendarContext",
        flat_fields=("container_title", "source_title", "context_note"),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="datebook.DeviceCommand",
        flat_fields=("display_text", "destination_name", "result_display"),
        json_paths=(
            "payload.items[].title",
            "payload.items[].location",
            "payload.items[].notes",
            "payload.items[].destination_name",
            "payload.items[].calendar_title",
            "payload.items[].list_title",
        ),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="datebook.DatebookDestinationDefault",
        flat_fields=("name",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="finance.FinanceAccount",
        flat_fields=("nickname",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="finance.FinanceTransaction",
        flat_fields=("description",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="finance.PayoffPlan",
        flat_fields=(),
        json_paths=("schedule_json.**",),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="finance.FinanceSnapshot",
        flat_fields=(),
        json_paths=("accounts_json.**",),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="actions.PendingAction",
        flat_fields=("display_summary",),
        json_paths=("action_payload.**",),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="actions.ActionAuditLog",
        flat_fields=("display_summary",),
        json_paths=("action_payload.**",),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.Session",
        flat_fields=("project", "summary"),
        json_paths=("accomplishments[]", "blockers[]", "next_steps[]", "processed_summary.**"),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="journal.NoteTemplate",
        flat_fields=("name",),
        json_paths=("sections[].title", "sections[].content"),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="router.DeliveryAttempt",
        flat_fields=("response_excerpt",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="core.CoreProfile",
        flat_fields=("additional_context",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    # RULE: these paths enumerate USER-AUTHORED TEXT ONLY. Control/settings values
    # are never PII and must never be added here — no setting in the manifest is
    # PII-related, so redaction has no business touching one.
    #
    # The render manifest mixes narration with the TTS control values that voice it
    # (``voice`` -> Gemini ``voice_name``; ``global_tone``/segment ``tone`` -> the style
    # prompt; segment ``type``/``seconds: "flex"`` and the phase ``name`` arc -> the
    # renderer and ``validate_manifest``). ``manifest.**`` rewrote those too, which
    # rendered a whole sit as silence on 2026-08-18. Enumerate prose; never exclude
    # controls from a wildcard — a new control key must be safe by default.
    PlaceholderStore(
        model_label="core.MeditationSession",
        flat_fields=("title", "theme", "guidance_text", "feedback_note"),
        json_paths=(
            "manifest.title",
            "manifest.theme",
            "manifest.phases[].intent",
            "manifest.phases[].segments[].text",
        ),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="integrations.SautaiMealPlanJob",
        flat_fields=("user_prompt",),
        json_paths=(),
        receipts_field="pii_receipts",
    ),
    PlaceholderStore(
        model_label="automations.AutomationRun",
        flat_fields=(),
        json_paths=("input_payload.**", "result_payload.**"),
        receipts_field="pii_receipts",
    ),
)


def registered_stores() -> tuple[PlaceholderStore, ...]:
    return _STORES


def registered_store(model_label: str) -> PlaceholderStore:
    """Return one registered store or fail loudly at a writer seam."""
    for store in _STORES:
        if store.model_label == model_label:
            return store
    raise LookupError(f"placeholder store is not registered: {model_label}")


# Parse registered exclusions once, before any authoring traversal.
for _store in _STORES:
    for _field in _store.json_fields:
        _store.nested_json_exclusions(_field)

# Version the actual registered traversal table and its value predicates.
CARDIO_TRAVERSAL_VERSION = hashlib.sha256(
    repr(
        (
            tuple((store.model_label, store._compiled_exclusions) for store in _STORES if store.json_exclude_paths),
            CARDIO_KINDS,
            CARDIO_EFFORTS,
            CARDIO_RECOVERY_EFFORTS,
            CARDIO_TERRAINS,
            CARDIO_PACE_REGEX,
        )
    ).encode()
).hexdigest()[:16]
