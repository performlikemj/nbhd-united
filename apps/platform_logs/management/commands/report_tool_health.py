"""Per-tool call counts and error rates over a window — dead-tool detection.

A tool that errors on every single call is broken in a way nobody notices: the
assistant quietly stops being able to do the thing, and the drift only surfaces
when a human trips over it. This command turns that into a number.

Report-only by design (Phase 1). Phase 2 wires the same query to the alert channel.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q
from django.utils import timezone

from apps.platform_logs.models import ToolContractEvent


class Command(BaseCommand):
    help = "Report per-tool call counts and error rates; flag tools that only ever error."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--days", type=int, default=7, help="Window size in days (default 7).")
        parser.add_argument(
            "--min-calls",
            type=int,
            default=5,
            help="Calls required before a 100%%-error tool is flagged DEAD (default 5).",
        )
        parser.add_argument("--namespace", default=None, help="Restrict to one call-site namespace.")
        parser.add_argument("--tenant", default=None, help="Restrict to one tenant UUID.")

    def handle(self, *args, **options) -> None:
        days = options["days"]
        min_calls = options["min_calls"]
        if days < 1:
            raise CommandError("--days must be at least 1")
        if min_calls < 1:
            raise CommandError("--min-calls must be at least 1")

        cutoff = timezone.now() - timedelta(days=days)
        queryset = ToolContractEvent.objects.filter(created_at__gte=cutoff)
        if options["namespace"]:
            queryset = queryset.filter(namespace=options["namespace"])
        if options["tenant"]:
            queryset = queryset.filter(tenant_id=options["tenant"])

        rows = (
            queryset.values("tool_name")
            .annotate(
                calls=Count("id"),
                accepted=Count("id", filter=Q(outcome=ToolContractEvent.Outcome.ACCEPTED)),
                rejected=Count("id", filter=Q(outcome=ToolContractEvent.Outcome.REJECTED)),
                normalized=Count("id", filter=Q(outcome=ToolContractEvent.Outcome.NORMALIZED)),
                errors=Count("id", filter=Q(outcome=ToolContractEvent.Outcome.ERROR)),
            )
            .order_by("-calls", "tool_name")
        )

        self.stdout.write(f"Window: last {days}d (since {cutoff.isoformat()})")
        self.stdout.write(f"Dead threshold: 100% error over >= {min_calls} calls")

        if not rows:
            self.stdout.write("No tool events in window.")
            return

        self.stdout.write("")
        self.stdout.write(f"{'tool':<48} {'calls':>6} {'ok':>6} {'rej':>6} {'norm':>6} {'err':>6} {'err%':>6}")

        dead: list[tuple[str, int]] = []
        for row in rows:
            calls = row["calls"]
            errors = row["errors"]
            error_pct = (errors / calls) * 100 if calls else 0.0
            self.stdout.write(
                f"{row['tool_name']:<48} {calls:>6} {row['accepted']:>6} {row['rejected']:>6} "
                f"{row['normalized']:>6} {errors:>6} {error_pct:>5.1f}%"
            )
            if errors == calls and calls >= min_calls:
                dead.append((row["tool_name"], calls))

        self.stdout.write("")
        if dead:
            self.stdout.write(self.style.ERROR(f"DEAD TOOLS ({len(dead)}) — every call errored:"))
            for tool_name, calls in dead:
                self.stdout.write(self.style.ERROR(f"  {tool_name} ({calls} calls, 100% error)"))
        else:
            self.stdout.write(self.style.SUCCESS("No dead tools in window."))
