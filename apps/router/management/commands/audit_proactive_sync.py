"""Report on proactive-outbound thread-continuity coverage.

Looks back over the last N days at every ``ProactiveOutbound`` row and
breaks down what fraction was actually surfaced into a subsequent
inbound's envelope (``consumed_at IS NOT NULL``) vs. silently ignored.

A low consumption rate means users aren't replying to proactive
messages within the surface window — that's product-level signal, not
a bug. A high "unconsumed-but-superseded" rate (multiple proactive
sends with no reply between them) means the cron cadence is too dense
or the user has muted us — also worth knowing.

Read-only. Doesn't modify any rows.

Example::

    manage.py audit_proactive_sync --days 30
    manage.py audit_proactive_sync --days 7 --tenant 148ccf1c-ef13-47f8-ada1-a98fa90e14a0
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone

from apps.router.models import ProactiveOutbound


class Command(BaseCommand):
    help = "Report on proactive-outbound thread-continuity coverage."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="How many days back to scan (default: 30).",
        )
        parser.add_argument(
            "--tenant",
            type=str,
            default="",
            help="Optional tenant UUID to filter on.",
        )

    def handle(self, *args, **opts) -> None:
        days = int(opts["days"])
        tenant_filter = (opts.get("tenant") or "").strip()
        cutoff = timezone.now() - timedelta(days=days)

        qs = ProactiveOutbound.objects.filter(created_at__gte=cutoff)
        if tenant_filter:
            qs = qs.filter(tenant_id=tenant_filter)

        per_tenant = (
            qs.values("tenant_id")
            .annotate(
                total=Count("id"),
                consumed=Count("id", filter=Q(consumed_at__isnull=False)),
                with_items=Count("id", filter=~Q(parsed_items=[])),
            )
            .order_by("-total")
        )

        self.stdout.write(self.style.HTTP_INFO(f"Window: last {days} days (since {cutoff.isoformat()})"))
        self.stdout.write("")
        header = f"{'tenant':<40} {'total':>6} {'consumed':>9} {'rate':>6} {'structured':>11}"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        grand_total = 0
        grand_consumed = 0
        grand_structured = 0
        for row in per_tenant:
            total = row["total"]
            consumed = row["consumed"]
            structured = row["with_items"]
            rate = (consumed / total) if total else 0.0
            grand_total += total
            grand_consumed += consumed
            grand_structured += structured
            self.stdout.write(f"{str(row['tenant_id']):<40} {total:>6} {consumed:>9} {rate:>6.0%} {structured:>11}")

        if grand_total == 0:
            self.stdout.write(self.style.WARNING("No proactive outbounds in window."))
            return

        rate = grand_consumed / grand_total
        self.stdout.write("-" * len(header))
        self.stdout.write(f"{'TOTAL':<40} {grand_total:>6} {grand_consumed:>9} {rate:>6.0%} {grand_structured:>11}")
        self.stdout.write("")
        unconsumed = grand_total - grand_consumed
        self.stdout.write(
            self.style.HTTP_INFO(
                f"Unconsumed: {unconsumed} ({unconsumed / grand_total:.0%}) — "
                "these proactive messages were sent but no inbound surfaced them. "
                "Either the user never replied within 24h, or the reply landed "
                "on a path that hasn't been wired (audit reveals deployment "
                "coverage gaps)."
            )
        )
