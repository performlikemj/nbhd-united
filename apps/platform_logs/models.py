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
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="platform_issues"
    )
    category = models.CharField(
        max_length=30, choices=Category.choices, default=Category.OTHER
    )
    severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.LOW
    )
    tool_name = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Name of the tool that failed or was missing",
    )
    summary = models.CharField(max_length=500)
    detail = models.TextField(
        blank=True, default="",
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
