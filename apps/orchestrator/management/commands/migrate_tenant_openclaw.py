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
        parser.add_argument(
            "--recover-dead-owner",
            help="Exact owner token; attests this process/replica is confirmed terminated (see runbook)",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--verify-only", action="store_true")
        mode.add_argument("--report", action="store_true", help="Read-only per-tenant preservation readiness")

    def handle(self, *args, **options):
        raw = options["tenant"] or options["tenants"]
        try:
            ids = list(dict.fromkeys(uuid.UUID(value.strip()) for value in raw.split(",")))
        except ValueError:
            raise CommandError("Every tenant must be an explicit valid UUID") from None
        if Tenant.objects.filter(pk__in=ids).count() != len(ids):
            raise CommandError("Unknown tenant in batch; nothing changed")
        if options["recover_dead_owner"] and (
            len(ids) != 1 or options["dry_run"] or options["verify_only"] or options["report"]
        ):
            raise CommandError("Dead-owner recovery requires one tenant and execution mode")
        tag = options["tag"] or settings.OPENCLAW_IMAGE_TAG
        for tenant_id in ids:
            if options["report"]:
                result = openclaw_migration.report_tenant(Tenant.objects.get(pk=tenant_id))
                counts = ",".join(f"{k}={v}" for k, v in result["reasons"].items())
                self.stdout.write(
                    f"{tenant_id}: {result['status']} {counts} quarantined_cache={result.get('quarantined_cache', 0)}".rstrip()
                )
                continue
            if options["verify_only"]:
                tenant = Tenant.objects.get(pk=tenant_id)
                try:
                    result = openclaw_migration.verify_existing(tenant, tenant.openclaw_migration or {})
                    if result.get("status") == "BLOCKED_UNSUPPORTED":
                        counts = ",".join(f"{k}={v}" for k, v in result["reasons"].items())
                        self.stdout.write(f"{tenant_id}: BLOCKED_UNSUPPORTED {counts}")
                        for declaration in result.get("declarations", []):
                            self.stdout.write(f"  {declaration['key']}: {','.join(declaration['reasons'])}")
                        raise CommandError("Batch stopped: BLOCKED_UNSUPPORTED")
                except CommandError:
                    raise
                except openclaw_migration.VerificationError as exc:
                    raise CommandError(f"FAIL {exc.code}") from None
                except Exception:
                    raise CommandError("FAIL verification_unavailable") from None
                self.stdout.write("PASS verified")
                continue
            try:
                result = migrate_tenant(
                    tenant_id,
                    tag,
                    dry_run=options["dry_run"],
                    **({"recover_dead_owner": options["recover_dead_owner"]} if options["recover_dead_owner"] else {}),
                )
            except MigrationError as exc:
                raise CommandError(f"{tenant_id}: {exc}") from None
            self.stdout.write(f"{tenant_id}: {result['status']} steps={','.join(result['steps'])}")
            if result.get("quarantined_cache"):
                self.stdout.write(f"quarantined_cache={result['quarantined_cache']}")
            if result.get("reasons"):
                self.stdout.write(",".join(f"{k}={v}" for k, v in result["reasons"].items()))
            if result["status"] in {"BLOCKED_UNSUPPORTED", "DEFER"}:
                raise CommandError("Batch stopped: " + result["status"])
            if result.get("note"):
                self.stdout.write(result["note"])
            if result.get("needs_agent_resync"):
                self.stdout.write(f"Needs agent re-sync: {result['needs_agent_resync']} (see private migration record)")
