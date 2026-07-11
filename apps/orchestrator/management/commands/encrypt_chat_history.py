"""Backfill sealed ``*_enc`` columns for chat content (Encryption-at-rest Phase 2 PR-3).

Closes the pre-write-flag gap: rows inserted BEFORE a tenant's
``encrypt_chat_writes`` flag was turned on have ``user_text_enc`` /
``title_enc`` NULL. This command seals each such row's legacy plaintext into
its ``_enc`` sidecar via ``box.encrypt`` so the read-flip (PR-4) can serve
everything from ``_enc``. Covers ``AppChatMessage.user_text`` and
``ChatThread.title``.

ONLY processes tenants whose ``encrypt_chat_writes`` flag is ON — the plan's
ordering (docs/encryption-at-rest-phase2-plan.md §4/§5): backfill runs AFTER
the write-flag flips so it closes EXACTLY the pre-flip gap (rows inserted
post-flip already carry ``_enc``). Flag-off tenants are skipped and counted.

Idempotent: only rows whose ``_enc`` column IS NULL are touched, so a re-run
reports "0 encrypted". Empty legacy values seal to the documented ``b""``
sentinel (NOT NULL — NULL is the dual-read "not encrypted" discriminator; see
apps/crypto/box.py and plan §6).

Transaction discipline (docs/agents/invariants.md #8 — no external calls inside
``transaction.atomic()``): ``box.encrypt`` hits the DEK cache / Key Vault broker
(a network call on a cold DEK), so it runs OUTSIDE any transaction. Each row is:
encrypt (no txn) -> plain ``.update()``. On a connection drop mid-sweep
(idle-in-transaction reap / cross-region blip) the ``.update()`` raises
OperationalError/InterfaceError; we close the dead connection, re-establish the
per-tenant service-role RLS GUC on a fresh one, and retry the write ONCE (the
``_save_session`` pattern in apps/core/services.py).

Zero-arg QStash-triggerable (no-body publish to
``/api/cron/trigger/encrypt_chat_history[_dry_run]/``); also runnable directly.

Usage:
    python manage.py encrypt_chat_history
    python manage.py encrypt_chat_history --tenant-id <uuid>
    python manage.py encrypt_chat_history --dry-run
    python manage.py encrypt_chat_history --max 5
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.router import enc_columns
from apps.router.models import AppChatMessage, ChatThread
from apps.tenants.models import Tenant

# (model, plaintext field, _enc field, AAD tuple) — the two in-scope columns.
_COLUMNS = (
    (AppChatMessage, "user_text", "user_text_enc", enc_columns.APP_CHAT_MESSAGE_USER_TEXT),
    (ChatThread, "title", "title_enc", enc_columns.CHAT_THREAD_TITLE),
)


class Command(BaseCommand):
    help = "Seal legacy chat plaintext (user_text / title) into the *_enc columns for write-flag-on tenants."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            help="Backfill only this tenant (UUID). Default: every ACTIVE/SUSPENDED tenant.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the COUNT of rows that would be sealed (never content); make no writes.",
        )
        parser.add_argument(
            "--max",
            type=int,
            default=None,
            help="Process at most this many write-flag-ON tenants (incremental/canary rollout).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        max_tenants = options.get("max")
        candidates = self._candidates(options.get("tenant_id"))

        self.stdout.write(f"Considering {len(candidates)} tenant(s)")

        processed = 0
        skipped_flag_off = 0
        totals = {"user_text": 0, "title": 0}
        errors = 0

        for tenant in candidates:
            # Plan ordering: only tenants whose write-flag is ON — backfill
            # closes exactly the pre-flip gap. Flag-off tenants have no _enc
            # anywhere yet and are intentionally left alone (counted).
            if not tenant.encrypt_chat_writes:
                skipped_flag_off += 1
                continue
            if max_tenants is not None and processed >= max_tenants:
                break

            per_tenant = self._backfill_tenant(tenant, dry_run=dry_run)
            processed += 1
            totals["user_text"] += per_tenant["user_text"]
            totals["title"] += per_tenant["title"]
            errors += per_tenant["errors"]

            verb = "would seal" if dry_run else "sealed"
            self.stdout.write(
                f"[{str(tenant.id)[:8]}] {verb} {per_tenant['user_text']} user_text + "
                f"{per_tenant['title']} title" + ("" if dry_run else f" ({per_tenant['errors']} errors)")
            )

        summary = (
            f"Would encrypt: {totals['user_text']} user_text + {totals['title']} title "
            if dry_run
            else f"Encrypted: {totals['user_text']} user_text + {totals['title']} title, {errors} errors "
        )
        summary += f"across {processed} tenant(s) ({skipped_flag_off} flag-off skipped)"
        self.stdout.write(self.style.SUCCESS(summary))

    def _candidates(self, tenant_id: str | None) -> list[Tenant]:
        q = Tenant.objects.filter(status__in=[Tenant.Status.ACTIVE, Tenant.Status.SUSPENDED])
        if tenant_id:
            q = q.filter(id=tenant_id)
        return list(q.order_by("created_at", "id"))

    def _backfill_tenant(self, tenant: Tenant, *, dry_run: bool) -> dict:
        # Per-tenant RLS GUC isolation (plan §5): scope the connection to this
        # one tenant before touching any of its rows, one tenant at a time.
        # service_role keeps writes working regardless of the fleet's RLS
        # posture (RLS is off in prod by design, but this stays correct if it
        # is ever re-enabled).
        from apps.tenants.middleware import set_rls_context

        set_rls_context(tenant_id=tenant.id, service_role=True)

        result = {"user_text": 0, "title": 0, "errors": 0}
        for model, value_field, enc_field, aad in _COLUMNS:
            key = value_field
            qs = model.objects.filter(tenant=tenant, **{f"{enc_field}__isnull": True})
            if dry_run:
                # COUNTS ONLY — never fetch or print the plaintext value.
                result[key] = qs.count()
                continue
            # Materialize (pk, value) pairs, then encrypt+update each row with
            # NO transaction open around the KV round trip (invariants #8).
            for pk, value in list(qs.values_list("pk", value_field)):
                try:
                    self._seal_one(tenant, model, pk, enc_field, aad, value)
                    result[key] += 1
                except Exception as exc:
                    result["errors"] += 1
                    self.stderr.write(self.style.ERROR(f"  FAIL {tenant.id} {enc_field} pk={pk}: {exc}"))
        return result

    def _seal_one(self, tenant, model, pk, enc_field, aad, value) -> None:
        from apps.crypto import box

        # box.encrypt runs OUTSIDE any transaction — it may do one KV-broker
        # unwrap on a cold DEK. "" -> b"" sentinel (plan §6).
        enc = box.encrypt(tenant.id, aad[0], aad[1], value)
        self._update_with_retry(tenant, model, pk, enc_field, enc)

    def _update_with_retry(self, tenant, model, pk, enc_field, enc) -> None:
        """Plain per-row ``.update()`` with the reconnect-and-re-set-RLS retry-once.

        The long per-tenant sweep can outlive an idle connection (Supabase
        idle-in-transaction / cross-region drop); the first write on a reaped
        connection raises OperationalError/InterfaceError. Drop it, re-establish
        the per-tenant service-role GUC on a fresh connection (it died with the
        old one), and retry once — the ``_save_session`` pattern.
        """
        from django.db import connection
        from django.db.utils import InterfaceError, OperationalError

        from apps.tenants.middleware import set_rls_context

        try:
            model.objects.filter(pk=pk).update(**{enc_field: enc})
        except (OperationalError, InterfaceError):
            connection.close()
            set_rls_context(tenant_id=tenant.id, service_role=True)
            model.objects.filter(pk=pk).update(**{enc_field: enc})
