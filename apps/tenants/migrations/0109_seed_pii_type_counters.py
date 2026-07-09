"""Seed ``Tenant.pii_type_counters`` from each tenant's CURRENT pii_entity_map.

The new field is the monotonic high-water mark for PII placeholder numbering:
``{"PERSON": 537}`` = the highest suffix EVER minted for that type, never
lowered on deletion. Before it existed, the next suffix was re-derived from
``max(map suffix per type) + 1`` alone, so deleting a binding lowered the max
and freed numbers were recycled onto different values — the bug this field
fixes (see ``Tenant.pii_type_counters`` and ``apps/pii/redactor``).

Seeding from the CURRENT map maxima is exactly right: at migration time the map
max IS the highest number in play, so recording it locks the high-water at the
correct floor. From the first post-migration mint onward the counter only ever
rises, even as bindings are deleted.

Batch-safe: streams tenants with a non-empty map via ``iterator()`` and writes
in ``bulk_update`` chunks so a large fleet map never materializes at once.
Reverse is a no-op — dropping the high-water would re-introduce the recycle bug,
and the schema migration's ``RemoveField`` is the real down path. Migrations must
not import app code, so the placeholder regex is inlined here (same shape as
``redactor._PLACEHOLDER_RE``, anchored because map keys are exact ``[TYPE_N]``).
"""

import re

from django.db import migrations

# Exact ``[TYPE_N]`` map key → (TYPE, N). Anchored: a map key is the whole
# placeholder, never embedded text. Mirrors redactor._PLACEHOLDER_RE's shape.
_PLACEHOLDER_KEY_RE = re.compile(r"^\[([A-Z_]+)_(\d+)\]$")

_CHUNK = 500


def _max_suffixes_per_type(entity_map):
    """Return ``{TYPE: max suffix}`` over the placeholder keys of ``entity_map``.

    Malformed / legacy non-placeholder keys are ignored. An empty result means
    the map holds no numbered placeholders (nothing to seed for that tenant).
    """
    counters = {}
    for key in entity_map or {}:
        match = _PLACEHOLDER_KEY_RE.match(key)
        if match:
            etype, num = match.group(1), int(match.group(2))
            counters[etype] = max(counters.get(etype, 0), num)
    return counters


def seed_counters(Tenant):
    """Populate ``pii_type_counters`` from map maxima for every tenant that has
    bindings but no counters yet. Returns the number of tenants updated.

    Split out from the migration hook so tests can drive it directly against the
    real ``Tenant`` model. Idempotent: a tenant whose counters are already set is
    left untouched (``exclude(pii_entity_map={})`` + the per-row empty guard), so
    re-running never lowers a high-water advanced by live mints since migration.
    """
    pending = []
    updated = 0
    queryset = (
        Tenant.objects.exclude(pii_entity_map={}).only("id", "pii_entity_map", "pii_type_counters").order_by("id")
    )
    for tenant in queryset.iterator(chunk_size=_CHUNK):
        # Don't clobber counters already advanced past the map (a mint that
        # landed between deploy and this migration, or a re-run).
        if tenant.pii_type_counters:
            continue
        counters = _max_suffixes_per_type(tenant.pii_entity_map)
        if not counters:
            continue
        tenant.pii_type_counters = counters
        pending.append(tenant)
        if len(pending) >= _CHUNK:
            Tenant.objects.bulk_update(pending, ["pii_type_counters"])
            updated += len(pending)
            pending = []
    if pending:
        Tenant.objects.bulk_update(pending, ["pii_type_counters"])
        updated += len(pending)
    return updated


def forwards(apps, schema_editor):
    seed_counters(apps.get_model("tenants", "Tenant"))


def reverse(apps, schema_editor):
    """No-op — the down path is the schema migration's RemoveField."""


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0108_tenant_pii_type_counters"),
    ]

    operations = [
        migrations.RunPython(forwards, reverse),
    ]
