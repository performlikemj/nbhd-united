from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class EvidenceSource(models.TextChoices):
    GATEWAY_HEARTBEAT = "gateway_heartbeat", "Gateway heartbeat"
    CI_RUN = "ci_run", "CI run"
    ASC_VERSION_STATE = "asc_version_state", "App Store Connect version state"
    MJ_ACK = "mj_ack", "MJ acknowledgement"
    EVAL_RUN = "eval_run", "Eval run"
    EVAL_SLO = "eval_slo", "Eval SLO"


REF_TYPES = frozenset(
    {
        "repo_branch",
        "pr",
        "continuity",
        "asc_version",
        "url",
    }
)


def _validate_refs(refs: object) -> None:
    if not isinstance(refs, list):
        raise ValidationError({"refs": "refs must be a list."})
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict) or set(ref) != {"type", "value"}:
            raise ValidationError({"refs": f"refs[{index}] must contain exactly type and value."})
        if ref["type"] not in REF_TYPES:
            raise ValidationError({"refs": f"refs[{index}].type is not supported."})
        value = ref["value"]
        if not isinstance(value, str) or not value or len(value) > 300:
            raise ValidationError(
                {"refs": f"refs[{index}].value must be a non-empty string of at most 300 characters."}
            )


class TrackedItem(models.Model):
    class Product(models.TextChoices):
        NBHD_IOS = "nbhd_ios", "NBHD iOS"
        NBHD_UNITED = "nbhd_united", "NBHD United"
        SAUTAI = "sautai", "Sautai"
        ACADEMY_WATCH = "academy_watch", "Academy Watch"
        YARDTALK = "yardtalk", "YardTalk"
        PORTFOLIO = "portfolio", "Portfolio"

    class Kind(models.TextChoices):
        WORK = "work", "Work"
        RELEASE = "release", "Release"
        BLOCKED_ON_MJ = "blocked_on_mj", "Blocked on MJ"
        RECURRING = "recurring", "Recurring"
        INFRA_WATCH = "infra_watch", "Infrastructure watch"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        ACTIVE = "active", "Active"
        BLOCKED = "blocked", "Blocked"
        PARKED = "parked", "Parked"
        DONE = "done", "Done"
        ABANDONED = "abandoned", "Abandoned"

    product = models.CharField(max_length=24, choices=Product.choices)
    kind = models.CharField(max_length=24, choices=Kind.choices)
    title = models.CharField(max_length=200)
    context = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    blocked_reason = models.CharField(max_length=200, blank=True)
    refs = models.JSONField(default=list, blank=True)
    provenance = models.CharField(
        max_length=24,
        choices=(
            ("mj", "MJ"),
            ("collector", "Collector"),
            ("agent_proposed", "Agent proposed"),
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status_changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "steward_tracked_items"
        ordering = ["product", "title", "id"]

    def clean(self) -> None:
        super().clean()
        if len(self.context) > 2000:
            raise ValidationError({"context": "context must be at most 2000 characters."})
        _validate_refs(self.refs)

    def __str__(self) -> str:
        return f"{self.product}:{self.title} ({self.status})"


class Expectation(models.Model):
    class Kind(models.TextChoices):
        HEARTBEAT = "heartbeat", "Heartbeat"
        DEADLINE = "deadline", "Deadline"
        RECURRENCE = "recurrence", "Recurrence"

    class State(models.TextChoices):
        ARMED = "armed", "Armed"
        SATISFIED = "satisfied", "Satisfied"
        MISSED = "missed", "Missed"
        RETIRED = "retired", "Retired"

    class OnMiss(models.TextChoices):
        URGENT = "urgent", "Urgent"
        DIGEST = "digest", "Digest"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    interval_s = models.PositiveIntegerField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    cron_expr = models.CharField(max_length=128, null=True, blank=True)
    grace_s = models.PositiveIntegerField()
    evidence_source = models.CharField(max_length=32, choices=EvidenceSource.choices)
    subject = models.CharField(max_length=128)
    state = models.CharField(max_length=16, choices=State.choices, default=State.ARMED)
    last_satisfied_at = models.DateTimeField(null=True, blank=True)
    miss_count = models.PositiveIntegerField(default=0)
    last_alerted_at = models.DateTimeField(null=True, blank=True)
    on_miss = models.CharField(max_length=16, choices=OnMiss.choices)
    owner = models.CharField(max_length=32, default="mj")
    subject_item = models.ForeignKey(
        TrackedItem,
        on_delete=models.PROTECT,
        related_name="expectations",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "steward_expectations"
        indexes = [
            models.Index(fields=["state", "kind"]),
            models.Index(fields=["evidence_source", "subject"]),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.kind == self.Kind.DEADLINE:
            if self.due_at is None:
                errors["due_at"] = "Deadline expectations require due_at."
            if self.interval_s is not None:
                errors["interval_s"] = "Deadline expectations cannot use interval_s."
            if self.cron_expr:
                errors["cron_expr"] = "cron_expr is not implemented in Phase 1."
        elif self.kind in (self.Kind.HEARTBEAT, self.Kind.RECURRENCE):
            if not self.interval_s:
                errors["interval_s"] = "Heartbeat and recurrence expectations require a positive interval_s."
            if self.due_at is not None:
                errors["due_at"] = "Interval expectations cannot use due_at."
            if self.cron_expr:
                errors["cron_expr"] = "cron_expr is not implemented in Phase 1."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.kind}:{self.subject} ({self.state})"


class EvidenceEvent(models.Model):
    class Trust(models.TextChoices):
        AUTHENTICATED_API = "authenticated_api", "Authenticated API"
        HOST_LOG = "host_log", "Host log"
        UNTRUSTED_TEXT = "untrusted_text", "Untrusted text"

    class Provenance(models.TextChoices):
        MJ = "mj", "MJ"
        COLLECTOR = "collector", "Collector"
        AGENT_PROPOSED = "agent_proposed", "Agent proposed"

    source = models.CharField(max_length=32, choices=EvidenceSource.choices)
    subject = models.CharField(max_length=128)
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured evidence metadata only; maximum 4KB and no user PII.",
    )
    fingerprint = models.CharField(max_length=192, unique=True)
    trust = models.CharField(max_length=24, choices=Trust.choices)
    provenance = models.CharField(max_length=24, choices=Provenance.choices)

    class Meta:
        db_table = "steward_evidence_events"
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["source", "subject", "-occurred_at"]),
        ]

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            raise ValidationError("EvidenceEvent is append-only and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("EvidenceEvent is append-only and cannot be deleted.")

    def __str__(self) -> str:
        return f"{self.source}:{self.subject}@{self.occurred_at.isoformat()}"


class Decision(models.Model):
    decided_at = models.DateTimeField(default=timezone.now)
    decision = models.TextField()
    rationale = models.TextField()
    alternatives_rejected = models.TextField(blank=True)
    refs = models.JSONField(default=list, blank=True)
    supersedes = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="superseded_by",
        null=True,
        blank=True,
    )
    provenance = models.CharField(
        max_length=24,
        choices=EvidenceEvent.Provenance.choices,
    )

    class Meta:
        db_table = "steward_decisions"
        ordering = ["-decided_at", "-id"]

    def clean(self) -> None:
        super().clean()
        _validate_refs(self.refs)

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            raise ValidationError("Decision is append-only and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Decision is append-only and cannot be deleted.")

    def __str__(self) -> str:
        return f"Decision({self.pk or 'new'}): {self.decision[:80]}"


class DependencyEdge(models.Model):
    class Kind(models.TextChoices):
        BLOCKS = "blocks", "Blocks"
        RELEASE_ORDER = "release_order", "Release order"
        SHARED_CONTRACT = "shared_contract", "Shared contract"

    from_item = models.ForeignKey(
        TrackedItem,
        on_delete=models.CASCADE,
        related_name="outgoing_dependencies",
    )
    to_item = models.ForeignKey(
        TrackedItem,
        on_delete=models.CASCADE,
        related_name="incoming_dependencies",
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)

    class Meta:
        db_table = "steward_dependency_edges"
        constraints = [
            models.UniqueConstraint(
                fields=["from_item", "to_item", "kind"],
                name="steward_dependency_edge_unique",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.from_item_id is not None and self.from_item_id == self.to_item_id:
            raise ValidationError("Dependency edges cannot point to the same item.")


class AlertState(models.Model):
    fingerprint = models.CharField(max_length=128, unique=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_reserved_at = models.DateTimeField(null=True, blank=True)
    sent_count = models.PositiveIntegerField(default=0)
    suppressed_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "steward_alert_states"


class DigestRecord(models.Model):
    class Delivery(models.TextChoices):
        DELIVERED = "delivered", "Delivered"
        TIMEOUT = "timeout", "Timeout"
        TRANSIENT = "transient", "Transient"
        UNDELIVERABLE = "undeliverable", "Undeliverable"

    sent_at = models.DateTimeField(default=timezone.now)
    period_date = models.DateField(
        null=True,
        blank=True,
        unique=True,
        help_text="UTC delivery date claimed by the scheduled daily digest.",
    )
    delivery = models.CharField(max_length=16, choices=Delivery.choices)
    body = models.TextField()
    stats = models.JSONField(default=dict)

    class Meta:
        db_table = "steward_digest_records"
        ordering = ["-sent_at", "-id"]

    def clean(self) -> None:
        super().clean()
        if len(self.body) > 8192:
            raise ValidationError({"body": "body must be at most 8192 characters."})
