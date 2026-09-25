"""Repair leaked panel markers locally or by explicit operator invocation."""

from datetime import UTC
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.router.models import AppChatMessage, ProactiveOutbound
from apps.router.panels import extract_panels, prepare_panels
from apps.router.proactive_context import parse_markdown_items
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Strip leaked panel markers and recover gated references. Dry-run unless --apply; counts only."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, type=UUID)
        parser.add_argument("--since", required=True, help="Inclusive ISO timestamp; naive timestamps use UTC.")
        parser.add_argument("--apply", action="store_true", help="Write repairs (default: dry-run).")

    def handle(self, *args, **options):
        try:
            since = parse_datetime(options["since"])
        except ValueError:
            since = None
        if since is None:
            raise CommandError("--since must be an ISO timestamp.")
        if timezone.is_naive(since):
            since = timezone.make_aware(since, UTC)
        set_rls_context(tenant_id=options["tenant"], service_role=True)
        try:
            tenant = Tenant.objects.get(pk=options["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError("Tenant not found.") from None

        for model, field in ((ProactiveOutbound, "message_text"), (AppChatMessage, "reply_text")):
            counts = {"matched": 0, "changed": 0, "panels_added": 0, "written": 0}
            rows = model.objects.filter(tenant=tenant, created_at__gte=since, **{f"{field}__contains": "[[panel:"})
            for pk in rows.values_list("pk", flat=True).iterator(chunk_size=200):
                # Re-read under a row lock so a concurrent writer's panels win.
                with transaction.atomic():
                    row = model.objects.select_for_update().get(pk=pk, tenant=tenant)
                    original = getattr(row, field)
                    if "[[panel:" not in original:
                        continue
                    counts["matched"] += 1
                    text, fallback = extract_panels(original)
                    updates = {}
                    if text != original:
                        updates[field] = text
                        if model is ProactiveOutbound:
                            updates["parsed_items"] = parse_markdown_items(text)
                    if not row.panels:
                        panels = prepare_panels(tenant, fallback, tool=True)
                        if panels:
                            updates["panels"] = panels
                            counts["panels_added"] += len(panels)
                    if updates:
                        counts["changed"] += 1
                        if options["apply"]:
                            model.objects.filter(pk=pk, tenant=tenant).update(**updates)
                            counts["written"] += 1
            self.stdout.write(model.__name__ + " " + " ".join(f"{key}={value}" for key, value in counts.items()))
