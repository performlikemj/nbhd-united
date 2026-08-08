"""Registry-driven authoring and owner-read glue for row-shaped stores."""

from __future__ import annotations

from typing import Any

from apps.pii.authoring import WriterClass, author_json_paths, author_text, resolve_receipt_values
from apps.pii.store_registry import registered_store, rewrite_json_path


def author_store_fields(
    tenant,
    data: dict[str, Any],
    *,
    model_label: str,
    seam: str,
    writer: WriterClass,
    receipts: Any = None,
    flag_off_legacy_redaction: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Author supplied registered fields, preserving receipts for omitted ones."""
    store = registered_store(model_label)
    authored_data = dict(data)
    next_receipts = dict(receipts or {})

    for field in store.flat_fields:
        value = authored_data.get(field)
        if not isinstance(value, str):
            continue
        authored = author_text(
            tenant,
            value,
            seam=f"{seam}.{field}",
            writer=writer,
            field=field,
            model_label=model_label,
            flag_off_legacy_redaction=flag_off_legacy_redaction,
        )
        authored_data[field] = authored.text
        next_receipts[field] = authored.receipt

    for field in store.json_fields:
        if field not in authored_data:
            continue
        authored = author_json_paths(
            tenant,
            authored_data[field],
            paths=store.nested_json_paths(field),
            seam=f"{seam}.{field}",
            writer=writer,
            field=field,
            model_label=model_label,
            flag_off_legacy_redaction=flag_off_legacy_redaction,
        )
        authored_data[field] = authored.value
        next_receipts[field] = authored.receipt

    return authored_data, next_receipts


def owner_store_representation(instance, tenant, data: dict[str, Any], *, model_label: str) -> dict[str, Any]:
    """Rehydrate registered fields and resolve their receipts at an owner read."""
    from apps.pii.redactor import rehydrate_for_tenant

    store = registered_store(model_label)
    represented = dict(data)
    for field in store.flat_fields:
        if isinstance(represented.get(field), str):
            represented[field] = rehydrate_for_tenant(tenant, represented[field])
    for field in store.json_fields:
        if field not in represented:
            continue
        value = represented[field]
        for path in store.nested_json_paths(field):
            value, _changed = rewrite_json_path(
                value,
                path,
                lambda text: rehydrate_for_tenant(tenant, text),
            )
        represented[field] = value
    if "pii_receipts" in represented:
        represented["pii_receipts"] = resolve_receipt_values(
            getattr(instance, store.receipts_field, {}) or {},
            getattr(tenant, "pii_entity_map", None),
        )
    return represented


class OwnerStoreSerializerMixin:
    """Opt-in owner representation for DRF model serializers.

    Callers must provide ``context={"tenant": tenant, "rehydrate": True}``.
    Runtime serializers intentionally omit that marker and therefore remain in
    placeholder space without receipts.
    """

    pii_model_label: str

    def to_representation(self, instance):
        represented = super().to_representation(instance)
        tenant = self.context.get("tenant")
        if tenant is None or not self.context.get("rehydrate"):
            represented.pop("pii_receipts", None)
            return represented
        return owner_store_representation(
            instance,
            tenant,
            represented,
            model_label=self.pii_model_label,
        )
