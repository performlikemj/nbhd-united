from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class EvidenceSource(models.TextChoices):
    GATEWAY_HEARTBEAT = "gateway_heartbeat", "Gateway heartbeat"
    CI_RUN = "ci_run", "CI run"
    GITHUB_STATE = "github_state", "GitHub state"
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


class ReleaseTrain(models.Model):
    class Phase(models.TextChoices):
        PLANNED = "planned", "Planned"
        INTEGRATING = "integrating", "Integrating"
        VERIFIED_LOCAL = "verified_local", "Verified locally"
        PUSHED = "pushed", "Pushed"
        CI_GREEN = "ci_green", "CI green"
        TAGGED = "tagged", "Tagged"
        SUBMITTED = "submitted", "Submitted"
        IN_REVIEW = "in_review", "In review"
        RELEASED = "released", "Released"
        ROLLED_BACK = "rolled_back", "Rolled back"

    product = models.CharField(max_length=24, choices=TrackedItem.Product.choices)
    version_string = models.CharField(max_length=32)
    phase = models.CharField(max_length=24, choices=Phase.choices, default=Phase.PLANNED)
    phase_changed_at = models.DateTimeField(default=timezone.now)
    head_sha = models.CharField(max_length=40, null=True, blank=True)
    head_ref = models.CharField(max_length=120, null=True, blank=True)
    refs = models.JSONField(default=list, blank=True)
    tracked_item = models.ForeignKey(
        TrackedItem,
        on_delete=models.PROTECT,
        related_name="release_trains",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "steward_release_trains"
        ordering = ["product", "version_string", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "version_string"],
                name="steward_release_train_product_version_unique",
            )
        ]

    def clean(self) -> None:
        super().clean()
        _validate_refs(self.refs)
        if self.head_sha is not None and (
            len(self.head_sha) != 40 or any(character not in "0123456789abcdef" for character in self.head_sha)
        ):
            raise ValidationError({"head_sha": "head_sha must be exactly 40 lowercase hexadecimal characters."})
        if self.tracked_item_id is not None and self.tracked_item.product != self.product:
            raise ValidationError({"tracked_item": "tracked item product must match the release train product."})

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            previous_phase = type(self).objects.filter(pk=self.pk).values_list("phase", flat=True).first()
            if previous_phase != self.phase and not getattr(self, "_phase_transition_allowed", False):
                raise ValidationError(
                    {"phase": "ReleaseTrain phase changes must use apps.steward.trains.advance_train()."}
                )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.product}:{self.version_string} ({self.phase})"


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


class RepoPullRequest(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Open"
        MERGED = "merged", "Merged"
        CLOSED = "closed", "Closed"

    repo = models.CharField(max_length=60)
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=140)
    author = models.CharField(max_length=60)
    draft = models.BooleanField(default=False)
    state = models.CharField(max_length=12, choices=State.choices)
    opened_at = models.DateTimeField()
    last_activity_at = models.DateTimeField()
    is_dependabot = models.BooleanField(default=False)
    head_ref = models.CharField(max_length=120)
    synced_at = models.DateTimeField()

    class Meta:
        db_table = "steward_repo_pull_requests"
        ordering = ["repo", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["repo", "number"],
                name="steward_repo_pull_request_repo_number_unique",
            )
        ]
        indexes = [
            models.Index(fields=["repo", "state", "last_activity_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.repo}#{self.number} ({self.state})"


class AscVersionSnapshot(models.Model):
    version_id = models.CharField(max_length=120, unique=True)
    version_string = models.CharField(max_length=32)
    app_state = models.CharField(max_length=60)
    build_number = models.CharField(max_length=32, blank=True)
    build_processing_state = models.CharField(max_length=60, blank=True)
    phased_state = models.CharField(max_length=60, blank=True)
    phased_day = models.PositiveSmallIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "steward_asc_version_snapshots"
        ordering = ["version_string", "version_id"]

    def state_tuple(self) -> tuple[str, str, str, str, str, int | None]:
        return (
            self.version_string,
            self.app_state,
            self.build_number,
            self.build_processing_state,
            self.phased_state,
            self.phased_day,
        )


class CollectorStatus(models.Model):
    class Collector(models.TextChoices):
        GITHUB = "github", "GitHub"
        ASC = "asc", "App Store Connect"

    collector = models.CharField(max_length=16, choices=Collector.choices, unique=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(default=timezone.now)
    last_error_class = models.CharField(max_length=60, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    detail = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "steward_collector_statuses"
        ordering = ["collector"]
