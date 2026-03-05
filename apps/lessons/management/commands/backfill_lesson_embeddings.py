"""Backfill embeddings for approved lessons that are missing them.

Lessons approved through the nightly extraction callback path before the
process_approved_lesson() fix had no embedding generated.  This command
retroactively processes those lessons so they appear in the constellation
and are discoverable via semantic search.
"""
from django.core.management.base import BaseCommand

from apps.lessons.models import Lesson
from apps.lessons.services import process_approved_lesson


class Command(BaseCommand):
    help = "Backfill embeddings for approved lessons missing them"

    def handle(self, *args, **options):
        lessons = Lesson.objects.filter(status="approved", embedding__isnull=True)
        total = lessons.count()
        self.stdout.write(f"Found {total} approved lessons without embeddings")

        success = 0
        for lesson in lessons:
            try:
                process_approved_lesson(lesson)
                success += 1
                self.stdout.write(f"  Processed lesson {lesson.id}")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Failed lesson {lesson.id}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Done: {success}/{total} lessons processed"))

        # Re-cluster tenants that had lessons backfilled
        if success > 0:
            from apps.lessons.clustering import refresh_constellation

            tenant_ids = (
                Lesson.objects.filter(status="approved", embedding__isnull=False)
                .values_list("tenant_id", flat=True)
                .distinct()
            )
            for tid in tenant_ids:
                from apps.tenants.models import Tenant

                try:
                    tenant = Tenant.objects.get(id=tid)
                    count = Lesson.objects.filter(tenant=tenant, status="approved").count()
                    if count >= 5:
                        result = refresh_constellation(tenant)
                        self.stdout.write(f"  Re-clustered tenant {str(tid)[:8]}: {result}")
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  Clustering failed for tenant {str(tid)[:8]}: {e}"))
