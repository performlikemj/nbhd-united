"""Registry of placeholder-bearing persistence surfaces."""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps


@dataclass(frozen=True)
class PlaceholderStore:
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
)


def registered_stores() -> tuple[PlaceholderStore, ...]:
    return _STORES
