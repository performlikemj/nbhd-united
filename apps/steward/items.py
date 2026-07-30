from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.steward.models import EvidenceEvent, TrackedItem


def set_item_status(
    item: TrackedItem,
    status: str,
    *,
    provenance: str,
    reason: str = "",
) -> TrackedItem:
    """Mutate item status while preserving the soft-invariant audit timestamp."""
    if status not in TrackedItem.Status.values:
        raise ValidationError({"status": "status is not a valid TrackedItem status."})
    if provenance not in EvidenceEvent.Provenance.values:
        raise ValidationError({"provenance": "provenance is not valid."})
    if len(reason) > 200:
        raise ValidationError({"blocked_reason": "reason must be at most 200 characters."})

    changed = item.status != status
    item.status = status
    item.provenance = provenance
    item.blocked_reason = reason if status == TrackedItem.Status.BLOCKED else ""
    if changed:
        item.status_changed_at = timezone.now()
    item.full_clean()
    update_fields = ["status", "provenance", "blocked_reason", "updated_at"]
    if changed:
        update_fields.append("status_changed_at")
    item.save(update_fields=update_fields)
    return item
