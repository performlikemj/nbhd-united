"""Journal and weekly review persistence models."""

from __future__ import annotations

import uuid

from django.db import models
from pgvector.django import VectorField

from apps.insights.pillars import Pillar
from apps.tenants.models import Tenant

# Import so Django discovers the model for migrations
from .session_models import Session  # noqa: F401


class NoteTemplate(models.Model):
    """Per-tenant template definition for sectionized daily notes.

    `sections` stores a JSON list of section descriptors, each with:
    - slug: stable machine key
    - title: display heading in markdown
    - content: template seed content for the section
    - source: optional ownership hint (agent/human/shared)
    """

    class Source(models.TextChoices):
        AGENT = "agent", "Agent"
        HUMAN = "human", "Human"
        SHARED = "shared", "Shared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="note_templates")
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=128)
    sections = models.JSONField(default=list)
    is_default = models.BooleanField(default=False)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.SHARED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "note_templates"
        unique_together = [
            ("tenant", "slug"),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.slug}"


class JournalEntry(models.Model):
    class Energy(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="journal_entries")
    date = models.DateField()
    mood = models.CharField(max_length=255)
    energy = models.CharField(max_length=16, choices=Energy.choices)
    wins = models.JSONField(default=list, blank=True)
    challenges = models.JSONField(default=list, blank=True)
    reflection = models.TextField(blank=True, default="")
    raw_text = models.TextField()
    pii_receipts = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "journal_entries"
        indexes = [
            models.Index(fields=["tenant", "date"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.date}"


class WeeklyReview(models.Model):
    class WeekRating(models.TextChoices):
        THUMBS_UP = "thumbs-up", "Thumbs Up"
        THUMBS_DOWN = "thumbs-down", "Thumbs Down"
        MEH = "meh", "Meh"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="weekly_reviews")
    week_start = models.DateField()
    week_end = models.DateField()
    mood_summary = models.TextField()
    top_wins = models.JSONField(default=list, blank=True)
    top_challenges = models.JSONField(default=list, blank=True)
    lessons = models.JSONField(default=list, blank=True)
    week_rating = models.CharField(max_length=16, choices=WeekRating.choices)
    intentions_next_week = models.JSONField(default=list, blank=True)
    raw_text = models.TextField()
    pii_receipts = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "weekly_reviews"
        indexes = [
            models.Index(fields=["tenant", "week_start", "week_end"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.week_start}:{self.week_end}"


class DailyNote(models.Model):
    """One markdown document per tenant per date. Both human and agent append to it."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="daily_notes")
    date = models.DateField()
    markdown = models.TextField(default="")
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.DAILY_NOTE_MARKDOWN``) — ships DARK.
    # DailyNote is legacy v1 but still live-written (plan §1.1), so it is in scope.
    markdown_enc = models.BinaryField(null=True)
    template = models.ForeignKey(
        NoteTemplate,
        on_delete=models.SET_NULL,
        related_name="notes",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["tenant", "date"]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.date}"


class UserMemory(models.Model):
    """One markdown document per tenant — like MEMORY.md. Agent curates this."""

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="user_memory")
    markdown = models.TextField(default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.tenant_id}:memory"


class Document(models.Model):
    """One markdown document. Can be a daily note, goal, project, review, etc.

    This is the v2 unified model replacing DailyNote, JournalEntry, WeeklyReview,
    UserMemory, and NoteTemplate.

    Goal-kind documents may carry pillar/topic tagging plus a structured
    ``target`` and ``intent_status`` lifecycle so the assistant can anchor
    confidence and prescriptions to the user's stated intent. These fields are
    nullable so non-goal documents and pre-existing goals remain valid.
    """

    class Kind(models.TextChoices):
        DAILY = "daily", "Daily Note"
        WEEKLY = "weekly", "Weekly Review"
        MONTHLY = "monthly", "Monthly Review"
        # DEPRECATED — promoted to journal.Goal model when the tenant flag
        # ``experimental_typed_journal_lifecycle`` is on. Existing rows are
        # migrated via ``manage.py migrate_documents_to_typed_models``. New
        # writes should land in Goal; this choice remains for stale tenants
        # and historical rows.
        GOAL = "goal", "Goal"
        PROJECT = "project", "Project"
        # DEPRECATED — promoted to journal.Task model. See GOAL above.
        TASKS = "tasks", "Tasks"
        IDEAS = "ideas", "Ideas"
        MEMORY = "memory", "Memory"

    class IntentStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        ACHIEVED = "achieved", "Achieved"
        ABANDONED = "abandoned", "Abandoned"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    slug = models.CharField(max_length=128)
    title = models.CharField(max_length=256)
    markdown = models.TextField(default="")
    # P3 placeholder-at-rest receipts, keyed by field name ("title"/"markdown").
    # An ABSENT key means pre-P3 legacy text with no provenance — never "clean".
    # See apps/pii/authoring.py and docs/pii-placeholder-at-rest-directive.md §A2.
    pii_receipts = models.JSONField(default=dict, blank=True)
    pillar = models.CharField(max_length=32, choices=Pillar.choices, blank=True, default="")
    topic = models.ForeignKey(
        "insights.TopicRegistry",
        on_delete=models.SET_NULL,
        related_name="documents",
        null=True,
        blank=True,
    )
    target = models.JSONField(null=True, blank=True)
    intent_status = models.CharField(max_length=16, choices=IntentStatus.choices, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["tenant", "kind", "slug"]
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "kind"]),
            models.Index(fields=["tenant", "pillar", "kind"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.kind}:{self.slug}"


class PendingExtraction(models.Model):
    """A goal, task, or lesson extracted from a daily note, awaiting user approval.

    Created by the nightly extraction job. Delivered to the user via Telegram
    inline buttons. Auto-expires after 7 days if not actioned.
    """

    class Kind(models.TextChoices):
        LESSON = "lesson", "Lesson"
        GOAL = "goal", "Goal"
        TASK = "task", "Task"
        # A North Star hypothesis the nightly extractor proposes from
        # cross-pillar evidence. Unlike goal/task cards it is NOT auto-added;
        # it lands as ``status=PENDING`` and only becomes a confirmed Purpose
        # when the user approves it (consent-first). "purpose" fits the 16-char
        # ``kind`` column.
        PURPOSE = "purpose", "Purpose hypothesis"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DISMISSED = "dismissed", "Dismissed"
        EXPIRED = "expired", "Expired"
        UNDONE = "undone", "Undone"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="pending_extractions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    text = models.TextField()
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecar — sealed envelope of ``text`` under AAD
    # ``enc_columns.PENDING_EXTRACTION_TEXT``. Ships DARK (nothing reads/writes it
    # yet; PR-2 dual-writes behind ``Tenant.encrypt_journal_writes``, PR-4 reads
    # behind ``read_encrypted_journal``). NULL = not-yet-encrypted; ``b""`` = empty.
    text_enc = models.BinaryField(null=True)
    tags = models.JSONField(default=list)
    confidence = models.CharField(max_length=8, default="medium")  # high | medium
    source_date = models.DateField(null=True, blank=True)  # date of daily note extracted from
    expires_at = models.DateTimeField()
    telegram_message_id = models.CharField(max_length=64, blank=True)
    lesson_id = models.BigIntegerField(null=True, blank=True)
    goal = models.ForeignKey(
        "Goal",
        on_delete=models.SET_NULL,
        related_name="pending_extractions",
        null=True,
        blank=True,
    )
    task = models.ForeignKey(
        "Task",
        on_delete=models.SET_NULL,
        related_name="pending_extractions",
        null=True,
        blank=True,
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "journal_pending_extractions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"], name="journal_pen_tenant__a1d532_idx"),
            models.Index(fields=["tenant", "kind", "status"], name="journal_pen_tenant__44d381_idx"),
            models.Index(fields=["expires_at"], name="journal_pen_expires_396cb6_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.kind}:{str(self.id)[:8]}"


class Goal(models.Model):
    """Typed lifecycle for a user-stated intention with a target outcome.

    Replaces ``Document(kind="goal")``. Goals have state (active/achieved/
    abandoned) and an optional target — encoding them as markdown blobs
    with sidecar ``intent_status``/``target`` fields meant the agent could
    only update them via find-and-replace on prose, which produced the
    contradictory duplicate-doc bug on the canary (one slug saying "paid
    Apr 7 ✅", another saying "payment unconfirmed").

    Narrative reflection still belongs in ``description`` (free markdown);
    lifecycle is row-shaped now.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ACHIEVED = "achieved", "Achieved"
        ABANDONED = "abandoned", "Abandoned"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="goals")
    title = models.CharField(max_length=256)
    description = models.TextField(blank=True, default="")
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecars — ship DARK. AAD:
    # ``enc_columns.GOAL_TITLE`` / ``GOAL_DESCRIPTION``. See PendingExtraction.text_enc.
    title_enc = models.BinaryField(null=True)
    description_enc = models.BinaryField(null=True)
    pillar = models.CharField(max_length=32, choices=Pillar.choices, blank=True, default="")
    topic = models.ForeignKey(
        "insights.TopicRegistry",
        on_delete=models.SET_NULL,
        related_name="goals",
        null=True,
        blank=True,
    )
    target = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    parent_goal = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="sub_goals",
        null=True,
        blank=True,
    )
    # The North Star this goal serves, if any. Optional — most goals have no
    # explicit purpose link; the field lets a confirmed Purpose gather the
    # goals that move the user toward it. SET_NULL so retiring/deleting a
    # Purpose never cascades away the goal itself.
    purpose = models.ForeignKey(
        "Purpose",
        on_delete=models.SET_NULL,
        related_name="goals",
        null=True,
        blank=True,
    )
    target_date = models.DateField(null=True, blank=True)
    achieved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    migrated_from_document = models.ForeignKey(
        "Document",
        on_delete=models.SET_NULL,
        related_name="migrated_goals",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "journal_goals"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "pillar", "status"]),
            models.Index(fields=["tenant", "target_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:goal:{self.title[:32]}"

    def mark_achieved(self) -> None:
        from django.utils import timezone

        self.status = self.Status.ACHIEVED
        self.achieved_at = timezone.now()
        self.save(update_fields=["status", "achieved_at", "updated_at"])

    def abandon(self) -> None:
        self.status = self.Status.ABANDONED
        self.save(update_fields=["status", "updated_at"])


class Purpose(models.Model):
    """A user's North Star — a durable statement of direction or purpose.

    The layer above goals: goals are *what* the user is pursuing; a Purpose is
    *why* — the long-horizon direction those goals serve ("build a life where
    my work funds time with my kids", "become someone others can lean on").

    Consent-first by construction. The assistant may *propose* a purpose
    (``status=PROPOSED``, ``origin=ASSISTANT_PROPOSED``) but a Purpose only
    becomes ``CONFIRMED`` after the user explicitly agrees — never inferred
    silently into a hard fact. ``EVOLVING`` marks a confirmed purpose the user
    is actively reshaping; ``RETIRED`` preserves history without deleting.

    Only ``CONFIRMED`` (and ``EVOLVING``) purposes surface in USER.md's North
    Star section and the session/cron grounding — a proposal is a question, not
    a fact, so it never grounds the assistant's reasoning until the user says
    yes.
    """

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        CONFIRMED = "confirmed", "Confirmed"
        EVOLVING = "evolving", "Evolving"
        RETIRED = "retired", "Retired"

    class Origin(models.TextChoices):
        ASSISTANT_PROPOSED = "assistant_proposed", "Assistant proposed"
        USER_CREATED = "user_created", "User created"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="purposes")
    statement = models.TextField()
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.PURPOSE_STATEMENT``) — ships DARK.
    statement_enc = models.BinaryField(null=True)
    # List of pillar slugs this purpose spans (see apps.insights.pillars.Pillar).
    # A North Star is usually cross-pillar — that breadth is what distinguishes
    # it from a single-pillar goal.
    pillars = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    origin = models.CharField(max_length=24, choices=Origin.choices, default=Origin.USER_CREATED)
    # List of {kind, ref, note} evidence dicts grounding an assistant proposal
    # (e.g. {"kind": "journal", "ref": "2026-06-30", "note": "..."}). Empty for
    # user-created purposes.
    evidence = models.JSONField(default=list, blank=True)
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.PURPOSE_EVIDENCE``, JSON) — ships DARK.
    evidence_enc = models.BinaryField(null=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "journal_purposes"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:purpose:{self.statement[:32]}"

    def confirm(self) -> None:
        from django.utils import timezone

        self.status = self.Status.CONFIRMED
        if self.confirmed_at is None:
            self.confirmed_at = timezone.now()
        self.save(update_fields=["status", "confirmed_at", "updated_at"])

    def retire(self) -> None:
        from django.utils import timezone

        self.status = self.Status.RETIRED
        self.retired_at = timezone.now()
        self.save(update_fields=["status", "retired_at", "updated_at"])


class Task(models.Model):
    """Typed lifecycle for an actionable item with a status.

    Replaces the markdown bullet lines that lived inside ``Document(kind="tasks")``.
    One row per task makes status, due dates, and parent-goal linkage queryable
    — and completing a task is a database UPDATE instead of a textual edit on a
    markdown file, so no stale snapshot ever ships with the agent's context.

    Tasks tied to a specific source-of-truth object (e.g. a FinanceAccount for
    "pay this loan") reference it via ``related_ref`` rather than a typed FK —
    keeps the schema closed while letting tasks point into any pillar's data.
    Shape: ``{"pillar": "gravity", "object_type": "FinanceAccount",
    "object_id": "<uuid>"}``.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        DONE = "done", "Done"
        SKIPPED = "skipped", "Skipped"
        DEFERRED = "deferred", "Deferred"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=256)
    description = models.TextField(blank=True, default="")
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecars — ship DARK. AAD:
    # ``enc_columns.TASK_TITLE`` / ``TASK_DESCRIPTION``. See PendingExtraction.text_enc.
    title_enc = models.BinaryField(null=True)
    description_enc = models.BinaryField(null=True)
    pillar = models.CharField(max_length=32, choices=Pillar.choices, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    parent_goal = models.ForeignKey(
        Goal,
        on_delete=models.SET_NULL,
        related_name="tasks",
        null=True,
        blank=True,
    )
    parent_task = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        related_name="subtasks",
        null=True,
        blank=True,
    )
    related_ref = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    migrated_from_document = models.ForeignKey(
        "Document",
        on_delete=models.SET_NULL,
        related_name="migrated_tasks",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "journal_tasks"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "pillar", "status"]),
            models.Index(fields=["tenant", "due_date"]),
            models.Index(fields=["tenant", "parent_goal"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:task:{self.title[:32]}"

    def complete(self) -> None:
        from django.utils import timezone

        self.status = self.Status.DONE
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at", "updated_at"])

    def skip(self) -> None:
        self.status = self.Status.SKIPPED
        self.save(update_fields=["status", "updated_at"])

    def defer(self) -> None:
        self.status = self.Status.DEFERRED
        self.save(update_fields=["status", "updated_at"])


class PendingTaskAction(models.Model):
    """An auto-applied reconciliation action on a Task or Goal with undo capability.

    Created by the extended nightly extraction pass when journal evidence
    matches an open ``Task`` or active ``Goal``. Mirrors the
    ``PendingExtraction`` pattern: each action is applied immediately at
    21:30 UTC, recorded here with a ``before_state`` snapshot, and the user
    gets a morning Telegram/LINE summary with per-item Remove buttons that
    revert by restoring the snapshot.

    Distinct from ``PendingExtraction``: that table tracks net-new items
    (lessons/goals/tasks) the LLM proposed; this one tracks state changes
    on rows that already existed.
    """

    class Kind(models.TextChoices):
        TASK_COMPLETE = "task_complete", "Task complete"
        TASK_PROGRESS = "task_progress", "Task progress"
        TASK_SKIP = "task_skip", "Task skip"
        TASK_DEFER = "task_defer", "Task defer"
        SUBTASK_CREATE = "subtask_create", "Subtask create"
        GOAL_ACHIEVE = "goal_achieve", "Goal achieve"
        GOAL_ABANDON = "goal_abandon", "Goal abandon"

    class Status(models.TextChoices):
        APPLIED = "applied", "Applied"
        UNDONE = "undone", "Undone"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="pending_task_actions",
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.APPLIED)

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pending_actions",
    )
    goal = models.ForeignKey(
        Goal,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pending_actions",
    )

    evidence = models.TextField(blank=True, default="")
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.PENDING_TASK_ACTION_EVIDENCE``) — ships DARK.
    evidence_enc = models.BinaryField(null=True)
    source_date = models.DateField()
    before_state = models.JSONField(null=True, blank=True)

    telegram_message_id = models.CharField(max_length=64, blank=True, default="")
    line_message_token = models.CharField(max_length=128, blank=True, default="")

    applied_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "journal_pending_task_actions"
        ordering = ["-applied_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "source_date"]),
            models.Index(fields=["tenant", "kind", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.kind}:{str(self.id)[:8]}"


class DocumentChunk(models.Model):
    """A chunked, embedded portion of a Document for vector search.

    Daily notes are split into ~500-token sections and embedded nightly
    so the poller can do contextual recall at session start.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="doc_chunks")
    document = models.ForeignKey("journal.Document", on_delete=models.CASCADE, related_name="chunks")
    chunk_index = models.IntegerField()
    text = models.TextField()
    # P3 receipts for the derived chunk text (key: "text"). A chunk is a COPY of
    # already-authored Document.markdown, but it is re-authored on derivation
    # because the chunk is what leaves for the embedding provider.
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.DOCUMENT_CHUNK_TEXT``) — ships DARK.
    # The ``embedding`` vector stays plaintext (disclosed residual, plan §7.4).
    text_enc = models.BinaryField(null=True)
    embedding = VectorField(dimensions=1536)
    source_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "journal_document_chunks"
        unique_together = ["document", "chunk_index"]
        indexes = [
            models.Index(fields=["tenant", "source_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.document_id}:chunk{self.chunk_index}"


class DocumentIngestion(models.Model):
    """The agreed manifest for one uploaded document — the unit of removal.

    An uploaded file is ephemeral (GC'd ~24h after arrival). What persists is
    the *information* the user agreed to keep, routed to its real destination.
    This row groups every saved item back to the document it came from, so the
    user can later say "forget everything from that PDF" and the server can
    delete exactly those items and nothing else (child ``DocumentIngestionArtifact``
    rows). Mirrors the ``PendingTaskAction`` shape (per-item child state) — a
    JSON blob would force read-modify-write of the whole list under contention.

    The same ledger also records information the assistant kept from a NON-upload
    source it read — a Gmail message, a calendar event, a Reddit post (the
    continuity-directive P3 extension). ``source_kind`` discriminates, and the
    grouping identity for "forget everything from that email" lives in
    ``source_ref`` (``gmail:<id>`` …) in place of a filename. Artifacts validate
    and forget exactly like an upload's; only the source identity differs.

    ``agreed_at`` carries a CHECK constraint copying ``CronJob.user_confirmed_at``'s
    shape. It is **audit hygiene only, not consent enforcement**: the keep endpoint
    always sets ``agreed_at=now()``, so the constraint can never actually fail — it
    guards a NULL the code never produces. Real consent enforcement is the
    behavioral AGENTS.md gate plus the deterministic same-turn write backstop (D8).
    """

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        KEPT = "kept", "Kept"
        PARTIALLY_REMOVED = "partially_removed", "Partially removed"
        REMOVED = "removed", "Removed"
        EXPIRED = "expired", "Expired"

    class SourceKind(models.TextChoices):
        # The source the kept information was derived from. UPLOAD is the original
        # document-keeping path (an ephemeral file). EMAIL/CALENDAR/REDDIT are the
        # continuity-directive P3 extension: the assistant read the info from a
        # Gmail message / calendar event / Reddit post (which never lands as a
        # file), so the identity that groups the saved items lives in ``source_ref``
        # (``gmail:<id>`` / ``gcal:<id>`` / ``reddit:<t3_/t1_-id>``) instead of a
        # filename, and there is no ephemeral file to expire.
        UPLOAD = "upload", "Upload"
        EMAIL = "email", "Email"
        CALENDAR = "calendar", "Calendar"
        REDDIT = "reddit", "Reddit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="doc_ingestions")
    thread = models.ForeignKey(
        "router.ChatThread",
        on_delete=models.SET_NULL,
        related_name="doc_ingestions",
        null=True,
        blank=True,
    )
    # Back-ref to the [Document attached:] upload turn; drives the completeness
    # gap signal (rows created in the marker window vs rows recorded).
    client_msg_id = models.CharField(max_length=64, blank=True, default="")
    source_kind = models.CharField(max_length=16, choices=SourceKind.choices, default=SourceKind.UPLOAD)
    # The stable source identity that groups saved items for "forget everything
    # from that email". Empty for UPLOAD (an upload is identified by its file);
    # ``gmail:<id>`` / ``gcal:<id>`` / ``reddit:<t3_/t1_-id>`` for the P3 sources.
    source_ref = models.CharField(max_length=255, blank=True, default="")
    # Human display label: a filename for UPLOAD, the email subject / event title /
    # post title for the P3 sources. Reused by the list + forget rendering.
    original_filename = models.CharField(max_length=255)
    # P3 receipts (key: "original_filename"). A filename is user content —
    # "alice-contract.pdf" names a person — so it goes through the chokepoint
    # like any other Layer-1 text.
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.DOCUMENT_INGESTION_ORIGINAL_FILENAME``) —
    # ships DARK. ``source_ref`` stays plaintext (it is a source id, not user content).
    original_filename_enc = models.BinaryField(null=True)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    # Copy of the attachment path — dead after the 24h GC, kept for provenance.
    workspace_path = models.CharField(max_length=255, blank=True, default="")
    uploaded_at = models.DateTimeField()
    # uploaded_at + ~24h — drives the honest-expiry copy ("gone in about a day").
    # NULL for non-upload sources: an email/event/post is not an ephemeral file,
    # so there is nothing to expire and the agent must not claim one clears out.
    file_expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.KEPT)
    agreed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "journal_document_ingestions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "created_at"]),
        ]
        constraints = [
            # Audit-only (D6): a kept ingestion must record WHEN agreement was
            # captured. Copies CronJob.user_confirmed_at's CHECK shape.
            models.CheckConstraint(
                condition=(~models.Q(status="kept") | models.Q(agreed_at__isnull=False)),
                name="doc_ingestion_kept_requires_agreed_at",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.original_filename}:{str(self.id)[:8]}"


class DocumentIngestionArtifact(models.Model):
    """One saved item from a document ingestion, with independent removal state.

    Each row points at a real destination object by ``(object_type, object_id)`` —
    both VALIDATED at keep time against a tenant-owned row of a registered type, so
    the ledger can never hold a reference the forget path can't act on. ``removed_at``
    / ``last_error`` flip per row (idempotent, re-entrant partial-failure retry).
    ``removal_strategy`` is SERVER-derived from ``object_type`` at keep time — the
    agent never sets it. ``content_excerpt`` survives deletion of the destination row
    (audit + console + honest forget receipt).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingestion = models.ForeignKey(
        DocumentIngestion,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    # Denormalized for RLS + direct (tenant, object_type) queries.
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="doc_ingestion_artifacts")
    kind = models.CharField(max_length=32)
    object_type = models.CharField(max_length=64)
    object_id = models.CharField(max_length=128)
    destination = models.CharField(max_length=255, blank=True, default="")
    content_excerpt = models.TextField(blank=True, default="")
    # P3 receipts (key: "content_excerpt"). The excerpt is quoted from an
    # uploaded/read source that never passed chat ingress, so this is one of the
    # few places raw unknown names can still enter Layer-1 storage.
    pii_receipts = models.JSONField(default=dict, blank=True)
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.DOCUMENT_INGESTION_ARTIFACT_CONTENT_EXCERPT``) — ships DARK.
    content_excerpt_enc = models.BinaryField(null=True)
    removal_strategy = models.CharField(max_length=32, blank=True, default="")
    removed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "journal_document_ingestion_artifacts"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["tenant", "ingestion"]),
            models.Index(fields=["tenant", "object_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.object_type}:{self.object_id[:16]}"


class Workspace(models.Model):
    """A focused conversation context for a tenant.

    Each workspace maps to a separate OpenClaw session via the `user` param
    in /v1/chat/completions. Messages are routed to the active workspace's
    session, giving each domain (work, personal, translation, etc.) its own
    independent conversation history while sharing the same workspace directory.

    Max 4 per tenant (enforced in application layer).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="workspaces")
    name = models.CharField(max_length=60)
    slug = models.SlugField(max_length=60)
    description = models.TextField(
        blank=True,
        default="",
        help_text="What topics this workspace covers. Used for routing classification.",
    )
    description_embedding = VectorField(
        dimensions=1536,
        null=True,
        blank=True,
        help_text="Embedding of description for message classification.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="The 'General' catch-all workspace. Cannot be deleted.",
    )
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "journal_workspaces"
        unique_together = [("tenant", "slug")]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.slug}"
