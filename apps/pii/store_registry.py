"""Registry of placeholder-bearing persistence surfaces."""

from __future__ import annotations

from dataclasses import dataclass

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
)


def registered_stores() -> tuple[PlaceholderStore, ...]:
    return _STORES
