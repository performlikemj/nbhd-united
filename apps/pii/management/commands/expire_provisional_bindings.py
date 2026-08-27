from django.core.management.base import BaseCommand, CommandError

from apps.pii.entity_registry import get_name, is_denied, retire_binding_by_placeholder
from apps.pii.provisional import transition_binding
from apps.pii.provisional_expiry import DEFAULT_MAX_ENTRIES, sweep_all_tenants


class Command(BaseCommand):
    help = "Expire provisional PII bindings (or report/promote/retire rollback candidates)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
        parser.add_argument("--promote-all", action="store_true")
        parser.add_argument("--retire-all", action="store_true")
        parser.add_argument("--report", action="store_true")

    def handle(self, *args, **options):
        modes = sum(bool(options[name]) for name in ("promote_all", "retire_all", "report"))
        if modes > 1:
            raise CommandError("choose only one of --promote-all, --retire-all, or --report")
        if options["max_entries"] <= 0:
            raise CommandError("--max-entries must be greater than zero")
        if not modes:
            totals = sweep_all_tenants(dry_run=options["dry_run"], max_entries=options["max_entries"])
            self.stdout.write(" ".join(f"{key}={value}" for key, value in totals.items()))
            return

        from apps.tenants.models import Tenant

        affected = 0
        for tenant in Tenant.objects.filter(status=Tenant.Status.ACTIVE).order_by("id"):
            for placeholder, entry in (tenant.pii_entity_map or {}).items():
                if options["report"]:
                    kind = placeholder.removeprefix("[").split("_", 1)[0]
                    name = get_name(entry)
                    metadata = entry if isinstance(entry, dict) else {}
                    if (
                        kind in {"PERSON", "LOCATION"}
                        and len(name.split()) == 1
                        and not metadata.get("reviewed_at")
                        and not metadata.get("retired")
                        and not metadata.get("provisional")
                        and not is_denied(tenant.pii_denylist, name)
                    ):
                        self.stdout.write(f"tenant={tenant.pk} placeholder={placeholder}")
                        affected += 1
                elif isinstance(entry, dict) and entry.get("provisional") and not entry.get("retired"):
                    if options["dry_run"]:
                        affected += 1
                    elif options["promote_all"]:
                        affected += int(transition_binding(tenant, placeholder, "promote").changed)
                    else:
                        affected += int(retire_binding_by_placeholder(tenant, placeholder, "rollback"))
        self.stdout.write(f"affected={affected} dry_run={int(options['dry_run'])}")
