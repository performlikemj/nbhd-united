"""Run one phase of an OpenClaw auto-upgrade (spawned by the QStash task).

Not for operators: runs are started by the idle sweep. A token that does not
hold the platform lease is a no-op. See apps/orchestrator/openclaw_auto_upgrade.py.
"""

from django.core.management.base import BaseCommand

from apps.orchestrator.openclaw_auto_upgrade import run_phase


class Command(BaseCommand):
    help = "Internal: run one auto-upgrade phase for a tenant holding the lease."

    def add_arguments(self, parser):
        parser.add_argument("tenant_id")
        parser.add_argument("run_token")
        parser.add_argument("phase", choices=("start", "resume", "recover"))

    def handle(self, *args, **options):
        from apps.crypto import audit
        from apps.tenants.middleware import set_rls_context

        # Same context as a QStash-dispatched task.
        set_rls_context(service_role=True)
        audit.set_principal("system_cron")
        result = run_phase(options["tenant_id"], options["run_token"], options["phase"])
        # Status/action/delay only; never tenant data.
        self.stdout.write(
            f"auto_upgrade {options['tenant_id'][:8]} {options['phase']}: "
            + " ".join(f"{k}={v}" for k, v in sorted(result.items()))
        )
