"""Postgres cache of OpenClaw cron jobs.

Phase 1 of the Postgres-first migration: a passive read cache populated
from successful ``cron.list`` calls and from hibernation snapshots. The
dashboard list endpoint reads from this table when the gateway is
unreachable (most commonly: idle hibernation), so users never see the
Azure "Container App - Unavailable" splash where they expect their
scheduled tasks. Writes still flow through the gateway.
"""

from __future__ import annotations

from django.db import models


class CronJob(models.Model):
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="cron_jobs",
    )
    name = models.CharField(max_length=255)
    gateway_job_id = models.CharField(max_length=64, blank=True, default="")
    data = models.JSONField(
        default=dict,
        help_text="The full job dict as returned by the OpenClaw gateway.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                name="cron_unique_tenant_name",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "last_synced_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.name}"
