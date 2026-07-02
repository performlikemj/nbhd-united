"""Promote Bug-A-culprit ``pii_entity_map`` rows into each tenant's denylist.

Production entity maps accumulated rows whose stored name is a degenerate
span — single letters ("I", "P", "u"), two-char fragments ("az"), or bare
punctuation ("_", "["). NER mis-minted them as CRYPTO_ADDRESS/ACCOUNT/PERSON,
and because Step 1 substituted them everywhere they garbled real messages.

The redactor now skips ALL degenerate stored rows at read time (its
``_is_degenerate_span`` guard covers 2-char alnum spans too), so a data
migration isn't strictly required. This command additionally records the
canonical key on ``pii_denylist`` so the worst false positives are durable,
visible in the admin UI, and suppressed even if a future code path stops
honoring the read-time guard. The ``pii_entity_map`` rows are KEPT so
historical placeholder references still rehydrate.

The durable-denylist criterion is deliberately STRICTER than the runtime
guard (see ``_is_bug_a_culprit``): only single-character or punctuation-only
spans are written. A denylist key suppresses that string type-agnostically and
forever, so a legitimate 2-char surname mis-minted pre-guard ("Li", "Wu",
"Ng") must NOT land there — the runtime guard already neutralizes those 2-char
spans without a permanent, name-suppressing side effect.

``--dry-run`` is the default: it prints per-tenant counts and the entity TYPES
found (never used for anything but reporting) and writes nothing. Pass
``--apply`` to persist the denylist additions under a per-tenant row lock,
consistent with the arbiter's denylist writes.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.pii.entity_registry import get_name, normalize_denylist_key


def _is_bug_a_culprit(name: str) -> bool:
    """True only for spans that can actually garble a message: a single
    character after stripping, or a span with no letter and no digit
    (punctuation-only, e.g. "_", "[", "[[").

    Stricter than the redactor's ``_is_degenerate_span`` on purpose. That guard
    also covers 2-char alnum spans ("az", "Li") — safe to neutralize at
    runtime, but unsafe to denylist durably: a legitimate 2-char surname would
    then be suppressed for the tenant forever, regardless of type.
    """
    stripped = (name or "").strip()
    if len(stripped) == 1:
        return True
    if not stripped:
        return False
    return not any(ch.isalpha() or ch.isdigit() for ch in stripped)


class Command(BaseCommand):
    help = "Add degenerate pii_entity_map spans to each tenant's pii_denylist (entity_map rows kept)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist denylist additions. Without this flag the command is a dry run.",
        )

    def handle(self, *args, **options):
        from apps.tenants.models import Tenant

        apply = options["apply"]
        now_iso = timezone.now().isoformat()

        tenants_scanned = 0
        tenants_with_degenerate = 0
        total_new_keys = 0

        candidates = (
            Tenant.objects.exclude(pii_entity_map={}).only("id", "pii_entity_map", "pii_denylist").order_by("id")
        )

        for tenant in candidates:
            tenants_scanned += 1
            entity_map = tenant.pii_entity_map or {}
            denylist = tenant.pii_denylist or {}

            # canonical_key -> set of entity TYPES, for reporting + write. Spans
            # here are single chars, so printing them leaks nothing; we still
            # report by key to stay consistent with the rest of the pipeline.
            new_keys: dict[str, set[str]] = {}
            for placeholder, entry in entity_map.items():
                name = get_name(entry)
                if not name or not _is_bug_a_culprit(name):
                    continue
                key = normalize_denylist_key(name)
                if not key or key in denylist:
                    continue
                etype = placeholder.strip("[]").rsplit("_", 1)[0]
                new_keys.setdefault(key, set()).add(etype)

            if not new_keys:
                continue

            tenants_with_degenerate += 1
            total_new_keys += len(new_keys)

            types_summary = sorted({t for types in new_keys.values() for t in types})
            self.stdout.write(f"tenant={str(tenant.id)[:8]} degenerate_keys={len(new_keys)} types={types_summary}")

            if not apply:
                continue

            # Re-read under a row lock so we don't clobber a concurrent denylist
            # or entity_map write (redactor mint, arbiter sweep, settings UI).
            with transaction.atomic():
                locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
                if locked is None:
                    continue
                locked_map = locked.pii_entity_map or {}
                locked_denylist = dict(locked.pii_denylist or {})
                changed = False
                for placeholder, entry in locked_map.items():
                    name = get_name(entry)
                    if not name or not _is_bug_a_culprit(name):
                        continue
                    key = normalize_denylist_key(name)
                    if not key or key in locked_denylist:
                        continue
                    locked_denylist[key] = {"reason": "degenerate", "decided_at": now_iso}
                    changed = True
                if changed:
                    Tenant.objects.filter(pk=tenant.pk).update(pii_denylist=locked_denylist)

        mode = "APPLIED" if apply else "DRY-RUN"
        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] tenants_scanned={tenants_scanned} "
                f"tenants_with_degenerate={tenants_with_degenerate} new_keys={total_new_keys}"
            )
        )
