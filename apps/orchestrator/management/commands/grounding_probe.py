"""Read-only grounding probe (see ``apps/orchestrator/grounding_probe.py``).

Renders the structured-state context a proactive cron sees for a tenant +
topic and checks whether known-recent ground truth is reachable.

    python manage.py grounding_probe --tenant 148ccf1c --topic "Security Champions" \\
        --expect "budget meeting" --expect offsec --expect "vendor"

Exit code 0 when every ``--expect`` term is reachable (GREEN), 1 when any is
missing (RED) — so it doubles as a pass/fail check in scripts/CI. Read-only.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.grounding_probe import probe_grounding
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Render the structured-state context a proactive cron sees for a tenant + topic "
        "and check it contains known-recent ground truth (read-only)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant id (full UUID or unique prefix)")
        parser.add_argument(
            "--topic",
            required=True,
            help="Topic a proactive cron would reference, e.g. 'Security Champions'",
        )
        parser.add_argument(
            "--expect",
            action="append",
            default=[],
            metavar="SUBSTRING",
            help="Ground-truth substring that SHOULD be reachable (repeatable)",
        )

    def _resolve_tenant(self, ident: str) -> Tenant:
        ident = ident.strip()
        matches = [t for t in Tenant.objects.all() if str(t.id) == ident or str(t.id).startswith(ident)]
        if not matches:
            raise CommandError(f"No tenant matching '{ident}'")
        if len(matches) > 1:
            raise CommandError(f"'{ident}' is ambiguous across {len(matches)} tenants — use the full UUID")
        return matches[0]

    def handle(self, *args, **opts):
        tenant = self._resolve_tenant(opts["tenant"])
        report = probe_grounding(tenant, opts["topic"], opts["expect"])
        w = self.stdout.write

        w("")
        w(f"Grounding probe — tenant {str(tenant.id)[:8]} — topic: {report.topic!r}")
        w(f"  topic in always-loaded USER.md envelope: {report.topic_in_envelope}")
        if report.envelope_error:
            w(f"  [envelope render error (fell back to docs only): {report.envelope_error}]")

        w(f"  reachable docs (nbhd_journal_search + phrase match): {len(report.reachable_docs)}")
        for d in report.reachable_docs:
            w(f"    - {d['kind']:>8}/{d['slug']:<30} updated {d['updated_at']:%Y-%m-%d %H:%M} rank={d['rank']:.3f}")

        if report.newest_source is not None:
            w(f"  newest source: {report.newest_source:%Y-%m-%d %H:%M UTC}")
        else:
            w("  newest source: (none — topic not found in any document)")

        if report.expect_terms:
            w("  ground-truth terms:")
            for t in report.expect_terms:
                if report.term_in_envelope[t]:
                    where, mark = "in envelope (always loaded)", "✓"
                elif report.term_reachable[t]:
                    where, mark = "reachable via doc search/get", "✓"
                else:
                    where, mark = "ABSENT from all cron-visible sources", "✗"
                w(f"    {mark} {t!r}: {where}")

        verdict = "GREEN — grounded" if report.grounded else "RED — NOT grounded (stale or missing)"
        w("")
        w(f"  VERDICT: {verdict}")
        w("")

        if not report.grounded:
            raise SystemExit(1)
