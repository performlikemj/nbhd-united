from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.journal import encryption
from apps.journal.models import Document
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Encrypt existing unencrypted Document rows with tenant-specific journal keys."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            dest="tenant_id",
            help="Limit migration to one tenant UUID.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print actions without writing encrypted data.",
        )

    def handle(self, *args, **options):
        tenant_id = options.get("tenant_id")
        dry_run = bool(options.get("dry_run", False))

        tenant_qs = Tenant.objects.all()
        if tenant_id:
            tenant_qs = tenant_qs.filter(id=tenant_id)

        tenant_count = 0
        total_scanned = 0
        total_encrypted = 0
        total_skipped = 0

        for tenant in tenant_qs:
            tenant_count += 1
            self.stdout.write(f"Tenant {tenant.id}: ensuring journal key...")

            if not tenant.encryption_key_ref:
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  - skipped key creation in dry-run mode for {tenant.id}",
                        ),
                    )
                else:
                    tenant.encryption_key_ref = encryption.create_tenant_key(tenant.id)
                    tenant.save(update_fields=["encryption_key_ref", "updated_at"])
                    encryption.backup_tenant_key(tenant.id)

            docs = Document.objects.filter(tenant=tenant, is_encrypted=False)
            count = docs.count()
            total_scanned += count

            if count == 0:
                self.stdout.write("  - no unencrypted documents")
                continue

            if dry_run:
                self.stdout.write(f"  - would encrypt {count} document(s)")
                total_skipped += count
                continue

            for doc in docs:
                doc.title = doc.title
                doc.markdown = doc.markdown
                doc.save()
                total_encrypted += 1
                self.stdout.write(f"  - encrypted: {doc.kind}/{doc.slug}")

            self.stdout.write(f"  - encrypted {count} document(s)")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry-run complete: tenants={tenant_count}, scanned={total_scanned}, would_encrypt={total_skipped}",
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Encrypt complete: tenants={tenant_count}, scanned={total_scanned}, encrypted={total_encrypted}",
                )
            )
