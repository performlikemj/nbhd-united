import uuid

from django.db import models

from apps.tenants.models import Tenant


class PlatformIssueLog(models.Model):
    class Category(models.TextChoices):
        MISSING_CAPABILITY = "missing_capability", "Missing Capability"
        TOOL_ERROR = "tool_error", "Tool Error"
        CONFIG_ISSUE = "config_issue", "Configuration Issue"
        RATE_LIMIT = "rate_limit", "Rate Limit Hit"
        AUTH_ERROR = "auth_error", "Authentication Error"
        OTHER = "other", "Other"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="platform_issues")
    category = models.CharField(max_length=30, choices=Category.choices, default=Category.OTHER)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.LOW)
    tool_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Name of the tool that failed or was missing",
    )
    summary = models.CharField(max_length=500)
    detail = models.TextField(
        blank=True,
        default="",
        help_text="Additional context (no user PII)",
    )
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_issue_logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["category", "resolved"]),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.tenant} — {self.summary[:80]}"


class ToolContractEvent(models.Model):
    """Content-free telemetry for every model-callable tool / runtime endpoint.

    Deliberately NOT a sibling of PlatformIssueLog: that model carries agent-authored
    prose (redacted at write, but still prose). This one carries no free text at all —
    every field is an enum, a code, a number, or an allowlisted flag. See
    `apps.platform_logs.telemetry.emit_tool_event`, which is the ONLY sanctioned
    writer; it structurally enforces the allowlist. Writing rows directly bypasses
    that guarantee.

    `tenant_id` is a bare UUID, not a ForeignKey, on purpose: telemetry must survive
    tenant deletion (a deprovisioned tenant's error rates are still evidence), the
    insert must not pay for a constraint check on the hot path, and rate queries never
    need to join back to tenant content.
    """

    class Outcome(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        NORMALIZED = "normalized", "Normalized"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    namespace = models.CharField(
        max_length=32,
        default="runtime",
        help_text="Call-site namespace; selects the detail-key allowlist.",
    )
    tool_name = models.CharField(
        max_length=120,
        help_text="Tool or runtime endpoint name (URL name for generic capture).",
    )
    tenant_id = models.UUIDField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    reason_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Short slug explaining a non-accepted outcome (e.g. http_400).",
    )
    detail = models.JSONField(
        default=dict,
        blank=True,
        help_text="Allowlisted scalar flags only — never free text.",
    )
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tool_contract_events"
        ordering = ["-created_at"]
        indexes = [
            # Per-tool call counts and error rates over a window — the query the
            # dead-tool report and every "did this drift?" check runs.
            models.Index(fields=["tool_name", "outcome", "-created_at"]),
            # Window scans across all tools, and the retention purge.
            models.Index(fields=["-created_at"]),
            models.Index(fields=["tenant_id", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.tool_name} {self.outcome} {self.reason_code}".strip()
