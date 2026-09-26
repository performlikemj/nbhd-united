"""State for the automatic 5.28 -> 9.4 upgrade that runs at idle time.

See apps/orchestrator/openclaw_auto_upgrade.py. No user content is stored:
outcomes are step names and reason codes only.
"""

from django.db import models


class OpenClawAutoUpgradeLock(models.Model):
    """Platform-wide single-flight lease (singleton ``pk=1``).

    The Azure exec console rate-limits subscription-wide, so at most one
    automatic migration runs at a time and runs are spaced apart.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    tenant = models.ForeignKey("tenants.Tenant", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    run_token = models.CharField(max_length=32, blank=True, default="")
    # A live run renews this (heartbeat / scheduled resume); an expired
    # holder means the run died and the idle sweep starts recovery.
    expires_at = models.DateTimeField(null=True, blank=True)
    next_allowed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "openclaw_auto_upgrade_lock"


class OpenClawAutoUpgrade(models.Model):
    """Per-tenant auto-upgrade state: cooldown, retry counters, audit trail."""

    tenant = models.OneToOneField(
        "tenants.Tenant", primary_key=True, on_delete=models.CASCADE, related_name="openclaw_auto_upgrade"
    )
    cooldown_until = models.DateTimeField(null=True, blank=True)
    run_token = models.CharField(max_length=32, blank=True, default="")
    # Owner token handed to migrate_tenant, so recovery can prove the
    # migration record belongs to this run before touching it.
    migration_token = models.CharField(max_length=32, blank=True, default="")
    counters = models.JSONField(default=dict, blank=True)
    last_outcome = models.JSONField(default=dict, blank=True)
    history = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "openclaw_auto_upgrades"
