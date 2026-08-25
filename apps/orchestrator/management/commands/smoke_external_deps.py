"""Run the deployed app's real external-dependency smoke checks."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.smoke_external_deps import run_smoke


class Command(BaseCommand):
    help = "Run small real calls against the app's external dependencies"

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Emit the complete report as JSON")
        parser.add_argument("--only", default="", help="Comma-separated check names to run")

    def handle(self, *args, **options):
        checks = [name.strip() for name in options["only"].split(",") if name.strip()] or None
        try:
            report = run_smoke(checks=checks)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), sort_keys=True))
        else:
            for check in report.checks:
                if check.skipped_reason:
                    self.stdout.write(f"SKIP {check.name} {check.ms}ms — {check.skipped_reason}")
                elif check.ok:
                    self.stdout.write(f"PASS {check.name} {check.ms}ms")
                else:
                    self.stdout.write(f"FAIL {check.name} {check.ms}ms — {check.error_type}: {check.error_msg}")

        if not report.ok:
            failed = ", ".join(check.name for check in report.checks if not check.ok)
            raise CommandError(f"External dependency smoke failed: {failed}")
