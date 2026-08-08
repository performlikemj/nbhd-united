"""Registry of placeholder-bearing persistence surfaces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.apps import apps


@dataclass(frozen=True)
class PlaceholderStore:
    """One placeholder-bearing model surface.

    ``json_paths`` use dotted paths beginning with the model JSONField name;
    ``*``, ``[]``, and ``[*]`` fan out over mapping values or list items.
    """

    model_label: str
    flat_fields: tuple[str, ...]
    json_paths: tuple[str, ...]
    receipts_field: str

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


def json_path_parts(path: str) -> tuple[str, ...]:
    """Parse dotted paths; ``[]`` and ``[*]`` are aliases for ``*``.

    This is the single parser for every registry consumer. A registry path
    such as ``evidence[].note`` therefore selects the same leaves in live
    authoring, repair, migration, and junk healing.
    """
    normalized = path.replace("[*]", ".*").replace("[]", ".*")
    return tuple(part for part in normalized.split(".") if part)


def rewrite_json_path(
    value: Any,
    parts: tuple[str, ...],
    transform: Callable[[str], str],
) -> tuple[Any, bool]:
    """Copy-on-write transform of string leaves selected by one parsed path."""
    if not parts:
        if not isinstance(value, str):
            return value, False
        rewritten = transform(value)
        return rewritten, rewritten != value

    head, *tail_list = parts
    tail = tuple(tail_list)
    if head == "*":
        if isinstance(value, dict):
            next_value = value
            changed = False
            for key, child in value.items():
                rewritten_child, child_changed = rewrite_json_path(child, tail, transform)
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
                rewritten_child, child_changed = rewrite_json_path(child, tail, transform)
                if child_changed:
                    if not changed:
                        next_value = list(value)
                    next_value[index] = rewritten_child
                    changed = True
            return next_value, changed
        return value, False

    if isinstance(value, dict) and head in value:
        rewritten_child, changed = rewrite_json_path(value[head], tail, transform)
        if changed:
            next_value = dict(value)
            next_value[head] = rewritten_child
            return next_value, True
        return value, False
    if isinstance(value, list) and head.isdigit():
        index = int(head)
        if 0 <= index < len(value):
            rewritten_child, changed = rewrite_json_path(value[index], tail, transform)
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
)


def registered_stores() -> tuple[PlaceholderStore, ...]:
    return _STORES


def registered_store(model_label: str) -> PlaceholderStore:
    """Return one registered store or fail loudly at a writer seam."""
    for store in _STORES:
        if store.model_label == model_label:
            return store
    raise LookupError(f"placeholder store is not registered: {model_label}")
