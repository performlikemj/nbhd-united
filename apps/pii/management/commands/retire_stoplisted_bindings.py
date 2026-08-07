"""Retire active PERSON/LOCATION bindings whose name is fleet-stoplisted.

Companion backfill to the never-a-name stoplist in ``apps.pii.redactor``. The
stoplist only stops NEW junk from minting — Step 1 of redaction substitutes
already-known entities before ``_filter_results`` ever runs, so an existing
"calendar" PERSON binding keeps swapping a placeholder into the user's text
forever (the fleet audit found 830 live "calendar" bindings across 21 tenants).

Retire, never delete: a retired binding stops driving redaction but still
rehydrates old messages. Same semantics and same helper as
``retire_denied_bindings`` (#1402); the only difference is the match predicate —
there, the tenant's own denylist; here, the global stoplist via
``redactor.is_never_a_name``.

    manage.py retire_stoplisted_bindings <tenant_id>   # dry run (default)
    manage.py retire_stoplisted_bindings --all --commit
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.pii.entity_registry import canonical_key, get_name, retire_bindings_for_key
from apps.pii.redactor import is_never_a_name

# Only the free-form neural kinds are eligible. EMAIL/PHONE/ACCOUNT/… bindings
# come from checksummed or pattern recognizers, so a vocabulary rule has no
# business retiring them.
_RETIRE_KINDS = frozenset({"PERSON", "LOCATION"})

_PLACEHOLDER_KIND_RE = re.compile(r"^\[([A-Z_]+)_\d+\]$")


def _placeholder_kind(placeholder: str) -> str:
    """Entity kind encoded in a map key ("[PERSON_3]" -> "PERSON")."""
    match = _PLACEHOLDER_KIND_RE.match(placeholder or "")
    return match.group(1) if match else ""


# Fields only a human can put on a binding: relationship/notes come from the
# console's manual add-or-edit, reviewed_at is stamped when the user saw the
# binding in the tier-2 review queue and chose to KEEP it, and updated_at is
# written by every console write path (apps/tenants/views.py passes
# ``updated_at=now`` on add, merge and edit). The detector's mint writes NONE of
# them — it calls ``to_storage_value(original)`` with the name alone — so any of
# these four is proof a human touched this binding.
_CURATION_FIELDS = ("relationship", "notes", "reviewed_at", "updated_at")


def _is_user_curated(entry: object) -> bool:
    """True when a binding carries user-authored context or an explicit keep.

    The console screens a manual add with ``hygiene.is_junk_span`` only — NOT
    with this stoplist — so a user can legitimately hold a binding for someone
    whose name is stoplist vocabulary (a surname like "Quick"). Retiring it would
    silently undo their decision, so the backfill leaves curated bindings to the
    per-tenant deny flow.
    """
    if not isinstance(entry, dict):
        return False
    return any(entry.get(field) for field in _CURATION_FIELDS)


def _safe_label(canonical: str) -> str:
    """One-line, bounded form of a matched key for stdout.

    Only ever called on keys that already passed :func:`is_never_a_name`, so the
    text is stoplist vocabulary rather than anyone's name. It still needs
    flattening: the markdown-fragment junk class carries newlines ("quick
    wins\\n-") that would otherwise garble the per-tenant report line.
    """
    return " ".join(canonical.split())[:60]


def _retire_stoplisted(entity_map: dict, *, now_iso: str) -> tuple[dict, dict[str, int]]:
    """Return ``(updated_map, retired_count_by_label)`` for one tenant's map."""
    canonicals: set[str] = set()
    for placeholder, entry in entity_map.items():
        if _placeholder_kind(placeholder) not in _RETIRE_KINDS or _is_user_curated(entry):
            continue
        key = canonical_key(get_name(entry))
        if key and is_never_a_name(key):
            canonicals.add(key)

    updated_map = entity_map
    retired_by_key: dict[str, int] = {}
    for canonical in sorted(canonicals):
        before = updated_map
        updated_map, placeholders = retire_bindings_for_key(before, canonical, now_iso=now_iso)
        # ``retire_bindings_for_key`` matches on name across every kind and knows
        # nothing about curation. Put back anything outside this command's remit
        # so it can only ever touch detector-minted PERSON/LOCATION bindings.
        retired = []
        for placeholder in placeholders:
            original = before[placeholder]
            if _placeholder_kind(placeholder) in _RETIRE_KINDS and not _is_user_curated(original):
                retired.append(placeholder)
            else:
                updated_map[placeholder] = original
        if retired:
            label = _safe_label(canonical)
            retired_by_key[label] = retired_by_key.get(label, 0) + len(retired)
    return updated_map, retired_by_key


class Command(BaseCommand):
    help = "Retire pii_entity_map PERSON/LOCATION bindings whose name is on the global never-a-name stoplist (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("tenant_id", nargs="?", help="One tenant UUID")
        parser.add_argument("--all", action="store_true", help="Process every tenant")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Persist retirements. Without this flag the command is a dry run.",
        )

    def handle(self, *args, **options):
        from apps.tenants.models import Tenant

        tenant_id = options["tenant_id"]
        all_tenants = options["all"]
        commit = options["commit"]
        if bool(tenant_id) == bool(all_tenants):
            raise CommandError("Provide exactly one tenant_id or --all.")

        tenant_ids = Tenant.objects.order_by("id").values_list("id", flat=True)
        if tenant_id:
            tenant_ids = tenant_ids.filter(pk=tenant_id)
        tenant_ids = list(tenant_ids)
        if tenant_id and not tenant_ids:
            raise CommandError(f"Unknown tenant: {tenant_id}")

        total_retired = 0
        tenants_with_matches = 0
        for pk in tenant_ids:
            if commit:
                with transaction.atomic():
                    tenant = Tenant.objects.select_for_update().only("id", "pii_entity_map").get(pk=pk)
                    entity_map, retired_by_key = _retire_stoplisted(
                        dict(tenant.pii_entity_map or {}),
                        now_iso=timezone.now().isoformat(),
                    )
                    if retired_by_key:
                        Tenant.objects.filter(pk=pk).update(pii_entity_map=entity_map)
            else:
                tenant = Tenant.objects.only("id", "pii_entity_map").get(pk=pk)
                _, retired_by_key = _retire_stoplisted(
                    dict(tenant.pii_entity_map or {}),
                    now_iso=timezone.now().isoformat(),
                )

            retired_count = sum(retired_by_key.values())
            total_retired += retired_count
            tenants_with_matches += bool(retired_count)
            grouped = ",".join(f"{key}={count}" for key, count in retired_by_key.items()) or "none"
            verb = "retired" if commit else "would_retire"
            self.stdout.write(f"tenant={tenant.id} {verb}={retired_count} by_word={grouped}")

        mode = "COMMIT" if commit else "DRY-RUN"
        total_label = "bindings_retired" if commit else "bindings_would_retire"
        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] tenants_scanned={len(tenant_ids)} "
                f"tenants_with_matches={tenants_with_matches} {total_label}={total_retired}"
            )
        )
