"""Re-scrub persisted chat-thread titles without exposing their content.

Usage:
    python manage.py scrub_chat_thread_titles
    python manage.py scrub_chat_thread_titles --tenant-id <uuid>
    python manage.py scrub_chat_thread_titles --dry-run
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.router import enc_columns
from apps.router.conversation_capture import scrub_chat_thread_title
from apps.router.models import ChatThread
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Re-scrub ChatThread titles in plaintext and maintained encrypted sidecars."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            help="Scrub only this tenant (UUID). Default: every ACTIVE/SUSPENDED tenant.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report scanned/changed counts without writing or printing title content.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tenants = self._candidates(options.get("tenant_id"))
        totals = {"scanned": 0, "changed": 0, "errors": 0}

        for tenant in tenants:
            result = self._scrub_tenant(tenant, dry_run=dry_run)
            for key in totals:
                totals[key] += result[key]
            change_label = "would change" if dry_run else "changed"
            self.stdout.write(
                f"[{str(tenant.id)[:8]}] scanned {result['scanned']}, "
                f"{change_label} {result['changed']}, errors {result['errors']}"
            )

        change_label = "would change" if dry_run else "changed"
        summary = (
            f"Scanned {totals['scanned']}; {change_label} {totals['changed']}; "
            f"errors {totals['errors']} across {len(tenants)} tenant(s)"
        )
        self.stdout.write(self.style.SUCCESS(summary))

    def _candidates(self, tenant_id: str | None) -> list[Tenant]:
        if tenant_id:
            try:
                return [Tenant.objects.get(id=tenant_id)]
            except (Tenant.DoesNotExist, ValidationError) as exc:
                raise CommandError(f"Tenant {tenant_id!r} not found") from exc
        return list(
            Tenant.objects.filter(status__in=[Tenant.Status.ACTIVE, Tenant.Status.SUSPENDED]).order_by(
                "created_at", "id"
            )
        )

    def _scrub_tenant(self, tenant: Tenant, *, dry_run: bool) -> dict[str, int]:
        from apps.tenants.middleware import set_rls_context

        set_rls_context(tenant_id=tenant.id, service_role=True)
        result = {"scanned": 0, "changed": 0, "errors": 0}
        rows = ChatThread.objects.filter(tenant=tenant).only("id", "title", "title_enc").iterator(chunk_size=200)

        for thread in rows:
            result["scanned"] += 1
            try:
                safe_title, needs_change = self._safe_title(tenant, thread)
                if not needs_change:
                    continue
                if not dry_run:
                    self._write_title(tenant, thread, safe_title)
                result["changed"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the fleet sweep
                result["errors"] += 1
                self.stderr.write(
                    self.style.ERROR(f"FAIL tenant={str(tenant.id)[:8]} thread={thread.pk} error={type(exc).__name__}")
                )
        return result

    def _safe_title(self, tenant: Tenant, thread: ChatThread) -> tuple[str, bool]:
        legacy_title = thread.title
        legacy_safe = scrub_chat_thread_title(legacy_title)
        encrypted_title = None
        encrypted_safe = None

        if thread.title_enc is not None:
            from apps.crypto import box

            revealed = box.decrypt(tenant.id, *enc_columns.CHAT_THREAD_TITLE, thread.title_enc)
            encrypted_title = revealed.reveal()
            encrypted_safe = scrub_chat_thread_title(encrypted_title)

        needs_change = legacy_safe != legacy_title or encrypted_safe != encrypted_title
        if not needs_change:
            return legacy_title, False

        if tenant.read_encrypted_chat and encrypted_title is not None:
            return encrypted_safe, True
        return legacy_safe, True

    def _write_title(self, tenant: Tenant, thread: ChatThread, safe_title: str) -> None:
        updates = {"title": safe_title}
        if tenant.encrypt_chat_writes or thread.title_enc is not None:
            from apps.crypto import box

            updates["title_enc"] = box.encrypt(tenant.id, *enc_columns.CHAT_THREAD_TITLE, safe_title)
        self._update_with_retry(tenant, thread.pk, updates)

    def _update_with_retry(self, tenant: Tenant, thread_id, updates: dict) -> None:
        from django.db import connection
        from django.db.utils import InterfaceError, OperationalError

        from apps.tenants.middleware import set_rls_context

        try:
            ChatThread.objects.filter(pk=thread_id).update(**updates)
        except (OperationalError, InterfaceError):
            connection.close()
            set_rls_context(tenant_id=tenant.id, service_role=True)
            ChatThread.objects.filter(pk=thread_id).update(**updates)
