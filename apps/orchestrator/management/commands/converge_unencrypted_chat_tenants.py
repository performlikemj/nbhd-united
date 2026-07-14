"""Converge the post-flip plaintext-chat cohort onto encryption (Phase 2).

Compressed, per-tenant fleet ladder for tenants that were PROVISIONED before the
provision-time flag fix landed (this PR) and were never covered by the one-time
2026-07-11 fleet UPDATE that turned chat encryption on for the tenants that
existed then. Symptom: their ``Tenant.encrypt_chat_writes`` /
``read_encrypted_chat`` are still the model default ``False``, so every chat row
is written AND read as plaintext.

For each such tenant, in dependency order (the docs/encryption-at-rest-phase2-plan.md
§5 ladder, compressed to a single idempotent pass):
  1. flip ``encrypt_chat_writes`` ON  — from now on new writes dual-write ``_enc``.
  2. run the PR-3 chat backfill      — seal every pre-flip plaintext row into ``_enc``.
  3. VERIFY zero plaintext-only rows remain (``_enc IS NULL AND legacy <> ''``).
  4. only if verified clean, flip ``read_encrypted_chat`` ON — reads prefer ``_enc``.

Ordering matters: the write-flag flips (and is persisted) BEFORE the backfill, so a
row inserted mid-run already carries ``_enc`` (the columns are insert-once) and the
backfill closes exactly the pre-flip gap. The read-flag flips LAST, and only when
the tenant has zero unsealed content rows, so a read never has to serve
ciphertext-only data it can't reach. ``box.decrypt`` dual-reads regardless (a legacy
``str`` passes through verbatim), so even a stray plaintext-only row stays readable.

Idempotent / no-op safe:
  * A tenant already at (writes=ON, reads=ON) is SKIPPED untouched — the whole
    command is a no-op once the fleet is converged.
  * A re-run after a partial pass seals whatever is still ``_enc IS NULL`` and only
    then flips reads; a tenant whose backfill hit per-row errors keeps reads OFF and
    is reported ``incomplete`` (a later fire retries it).

``--dry-run`` reports the cohort + per-tenant ``_enc``-NULL row COUNTS (never
content) and flips no flag, seals no row.

Zero-arg QStash-triggerable (no-body publish to
``/api/cron/trigger/converge_unencrypted_chat_tenants[_dry_run]/``); also runnable
directly. Ships DARK — nothing fires it until an operator does.

Usage:
    python manage.py converge_unencrypted_chat_tenants
    python manage.py converge_unencrypted_chat_tenants --tenant-id <uuid>
    python manage.py converge_unencrypted_chat_tenants --dry-run
    python manage.py converge_unencrypted_chat_tenants --max 3
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.orchestrator.chat_encryption import CHAT_ENC_COLUMNS, count_unsealed_chat_rows
from apps.router.models import AppChatMessage
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Converge post-flip plaintext-chat tenants: flip write-flag, backfill *_enc, "
        "verify, then flip read-flag — per tenant, idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            help="Converge only this tenant (UUID). Default: every ACTIVE/SUSPENDED tenant.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the cohort + per-tenant *_enc-NULL COUNTS (never content); flip no flag, seal no row.",
        )
        parser.add_argument(
            "--max",
            type=int,
            default=None,
            help="Converge at most this many tenants that need it (incremental/canary rollout).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        max_tenants = options.get("max")
        candidates = self._candidates(options.get("tenant_id"))

        self.stdout.write(f"Considering {len(candidates)} tenant(s)")

        already = 0
        converged = 0
        incomplete = 0
        processed = 0
        totals = {"user_text": 0, "title": 0, "remaining": 0}

        for tenant in candidates:
            # Fully-converged tenants are never touched — no flag write, no
            # backfill query. This is what makes the command a no-op once the
            # fleet has converged.
            if tenant.encrypt_chat_writes and tenant.read_encrypted_chat:
                already += 1
                continue
            if max_tenants is not None and processed >= max_tenants:
                break
            processed += 1

            outcome = self._converge_tenant(tenant, dry_run=dry_run)
            totals["user_text"] += outcome["user_text"]
            totals["title"] += outcome["title"]
            totals["remaining"] += outcome["remaining"]
            if not dry_run:
                if outcome["converged"]:
                    converged += 1
                else:
                    incomplete += 1

            if dry_run:
                verb = "would seal"
            elif outcome["converged"]:
                verb = "converged, sealed"
            else:
                verb = "INCOMPLETE, sealed"
            line = f"[{str(tenant.id)[:8]}] {verb} {outcome['user_text']} user_text + {outcome['title']} title"
            if not dry_run:
                line += f" ({outcome['remaining']} unsealed remaining)"
            self.stdout.write(line)

        if dry_run:
            summary = (
                f"Would converge {processed} tenant(s): would seal {totals['user_text']} user_text + "
                f"{totals['title']} title; {already} already-converged (skipped)"
            )
        else:
            summary = (
                f"Converged {converged}, {incomplete} incomplete, {already} already-converged (skipped); "
                f"sealed {totals['user_text']} user_text + {totals['title']} title, "
                f"{totals['remaining']} rows still unsealed"
            )
        self.stdout.write(self.style.SUCCESS(summary))

    def _candidates(self, tenant_id: str | None) -> list[Tenant]:
        # Same candidate set as the PR-3 backfill: real, provisioned tenants
        # (ACTIVE or SUSPENDED). Synthetic/never-provisioned tenants are left
        # out. Stable order so --max is deterministic.
        q = Tenant.objects.filter(status__in=[Tenant.Status.ACTIVE, Tenant.Status.SUSPENDED])
        if tenant_id:
            q = q.filter(id=tenant_id)
        return list(q.order_by("created_at", "id"))

    def _converge_tenant(self, tenant: Tenant, *, dry_run: bool) -> dict:
        # Per-tenant RLS GUC isolation (plan §5): scope the connection to this
        # one tenant before touching any of its rows or flags, one tenant at a
        # time. service_role keeps the flag save + backfill working regardless of
        # the fleet's RLS posture (off in prod by design, correct if re-enabled).
        from apps.tenants.middleware import set_rls_context

        set_rls_context(tenant_id=tenant.id, service_role=True)

        result = {"user_text": 0, "title": 0, "remaining": 0, "converged": False}

        if dry_run:
            # Preview only: count the rows the backfill WOULD seal (every
            # ``_enc IS NULL`` row, empty legacy included → sealed to b""). No
            # flag flip, no write.
            for model, _value_field, enc_field in CHAT_ENC_COLUMNS:
                key = "user_text" if model is AppChatMessage else "title"
                result[key] = model.objects.filter(tenant=tenant, **{f"{enc_field}__isnull": True}).count()
            return result

        # 1. Flip the write-flag ON and PERSIST before the backfill, so a row
        #    inserted mid-run already carries ``_enc`` (insert-once) and the
        #    backfill closes exactly the pre-flip gap. Idempotent (a tenant that
        #    is writes=ON/reads=OFF from a prior partial run is picked up here).
        if not tenant.encrypt_chat_writes:
            tenant.encrypt_chat_writes = True
            tenant.save(update_fields=["encrypt_chat_writes", "updated_at"])

        # 2. Seal every pre-flip plaintext row into ``_enc`` — reuse the PR-3
        #    backfill's per-tenant sealer (RLS isolation + KV round trip OUTSIDE
        #    any txn + reconnect-and-retry). That is the single place the sealing
        #    logic lives; do not re-implement it here.
        sealed = self._sealer()._backfill_tenant(tenant, dry_run=False)
        result["user_text"] = sealed["user_text"]
        result["title"] = sealed["title"]

        # 3. VERIFY the backfill is complete: zero plaintext-only CONTENT rows
        #    remain (``_enc IS NULL AND legacy <> ''`` — the plan §7 completeness
        #    query, via the SHARED helper provision_tenant also gates on, so the
        #    two gates can never drift). Empty legacy rows are excluded: they
        #    read as "" via the fallback either way, so they never block the
        #    flip. A per-row backfill error leaves its non-empty row here and
        #    keeps reads OFF.
        remaining = count_unsealed_chat_rows(tenant)
        result["remaining"] = remaining

        # 4. Flip the read-flag ON only when the backfill is provably complete.
        #    Leaving it OFF on a non-empty remainder keeps reads on plaintext
        #    (correct) until a re-run finishes the seal.
        if remaining == 0:
            if not tenant.read_encrypted_chat:
                tenant.read_encrypted_chat = True
                tenant.save(update_fields=["read_encrypted_chat", "updated_at"])
            result["converged"] = True

        return result

    def _sealer(self):
        # Reuse the PR-3 backfill command's per-tenant sealer rather than
        # duplicate its RLS/transaction/retry discipline. Route its output to
        # this command's streams so per-row FAIL lines land in the same captured
        # buffer (the QStash task tails it into Log Analytics).
        from apps.orchestrator.management.commands.encrypt_chat_history import (
            Command as _EncryptChatHistory,
        )

        return _EncryptChatHistory(stdout=self.stdout, stderr=self.stderr)
