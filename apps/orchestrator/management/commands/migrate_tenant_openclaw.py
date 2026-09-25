"""Safe explicit-list migration; the first failure stops the batch."""

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator import openclaw_migration
from apps.orchestrator.openclaw_migration import MigrationError, migrate_tenant
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Migrate explicit awake tenants to OpenClaw 2026.9.4; never auto-rollback."

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--tenant")
        scope.add_argument("--tenants", help="Comma-separated explicit UUID list; stops on first failure")
        parser.add_argument("--tag", default=None)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--verify-only", action="store_true")

    def handle(self, *args, **options):
        raw = options["tenant"] or options["tenants"]
        try:
            ids = list(dict.fromkeys(uuid.UUID(value.strip()) for value in raw.split(",")))
        except ValueError:
            raise CommandError("Every tenant must be an explicit valid UUID") from None
        if Tenant.objects.filter(pk__in=ids).count() != len(ids):
            raise CommandError("Unknown tenant in batch; nothing changed")
        tag = options["tag"] or settings.OPENCLAW_IMAGE_TAG
        for tenant_id in ids:
            if options["verify_only"]:
                tenant = Tenant.objects.get(pk=tenant_id)
                try:
                    openclaw_migration.verify_existing(tenant, tenant.openclaw_migration or {})
                except openclaw_migration.VerificationError as exc:
                    raise CommandError(f"FAIL {exc.code}") from None
                except Exception:
                    raise CommandError("FAIL verification_unavailable") from None
                self.stdout.write("PASS verified")
                continue
            try:
                result = migrate_tenant(tenant_id, tag, dry_run=options["dry_run"])
            except MigrationError as exc:
                raise CommandError(f"{tenant_id}: {exc}") from None
            self.stdout.write(f"{tenant_id}: {result['status']} steps={','.join(result['steps'])}")
            if result.get("note"):
                self.stdout.write(result["note"])
            if result.get("needs_agent_resync"):
                self.stdout.write(f"Needs agent re-sync: {result['needs_agent_resync']} (see private migration record)")
