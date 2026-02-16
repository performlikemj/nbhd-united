from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.journal.models import DailyNote
from apps.journal.services import get_default_template, seed_default_templates_for_tenant
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Seed missing default note templates for existing tenants and backfill note template FK fields."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            dest="tenant_id",
            help="Limit backfill to a single tenant UUID.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print actions without writing changes.",
        )

    def handle(self, *args, **options):
        tenant_id = options.get("tenant_id")
        dry_run = options.get("dry_run", False)

        tenants_qs = Tenant.objects.all()
        if tenant_id:
            tenants_qs = tenants_qs.filter(id=tenant_id)

        tenant_count = 0
        template_created_count = 0
        notes_linked_count = 0

        for tenant in tenants_qs:
            tenant_count += 1
            template_result = seed_default_templates_for_tenant(
                tenant=tenant,
                dry_run=dry_run,
            )
            template_created_count += 1 if template_result["created"] else 0

            if dry_run:
                template = get_default_template(tenant=tenant)
                if template and DailyNote.objects.filter(
                    tenant=tenant,
                    template_id__isnull=True,
                ).exists():
                    notes = DailyNote.objects.filter(tenant=tenant, template_id__isnull=True).count()
                    notes_linked_count += notes
                    self.stdout.write(
                        f"Tenant {tenant.id}: would link {notes} daily notes to default template",
                    )
                continue

            template = get_default_template(tenant=tenant)
            if template is None:
                continue

            notes = DailyNote.objects.filter(tenant=tenant, template_id__isnull=True)
            count = notes.count()
            if count:
                notes.update(template=template)
                notes_linked_count += count
                self.stdout.write(
                    f"Tenant {tenant.id}: linked {count} daily note(s) to template {template.slug}",
                )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run complete. Tenants: {tenant_count}, templates_missing: {template_created_count}, notes_to_link: {notes_linked_count}",
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Seed complete. Tenants: {tenant_count}, templates_created: {template_created_count}, notes_linked: {notes_linked_count}",
                )
            )
