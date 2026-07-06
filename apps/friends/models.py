"""Neighborhood (Friends) layer — consent atoms.

PR0 ships only the invisible foundation: the identity (``NeighborProfile``),
the consent edge (``Friendship``), and the referral link (``FriendInvite``).
Everything cross-tenant (``SharedLesson``, ``FriendMessage``, ``SharedGoal*``,
``LessonShareGrant``, ``Circle*``) lands in later PRs and is read exclusively
through :mod:`apps.friends.access` — the single audited accessor guarded by
``apps.friends.test_access_chokepoint``.

Product surface names ("Neighborhood", "Neighbors", "wave", "spark") are copy
only; the code stays ``friends_*`` for consistency with the existing
``finance_enabled`` / ``fuel_enabled`` / ``core_enabled`` /
``site_publishing_enabled`` flag block.
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.tenants.models import Tenant, User


def compute_pair_key(a_id, b_id) -> str:
    """Canonical unordered-pair key for a friendship edge.

    ``f"{min}:{max}"`` of the two tenant UUID strings, so a wave ``A→B`` and a
    reciprocal wave ``B→A`` compute the **same** key and collide on the
    ``uq_friendship_pair`` UniqueConstraint. This is the ONLY dedup for the
    edge — never a service-layer check: two concurrent waves would both pass a
    Python check and race into two rows; only the DB unique constraint
    serializes them.
    """
    lo, hi = sorted((str(a_id), str(b_id)))
    return f"{lo}:{hi}"


class NeighborProfile(models.Model):
    """Who you are to neighbors.

    OneToOne on ``Tenant`` so it gates on ``friends_enabled`` and never bloats
    the auth ``User`` row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="neighbor_profile")
    handle = models.CharField(max_length=30, unique=True)  # lowercased [a-z0-9_]; the @handle you wave to
    display_name = models.CharField(max_length=80)  # defaults from User.display_name; overridable
    bio = models.CharField(max_length=280, blank=True)
    avatar_hue = models.IntegerField(default=210)  # 0-359; seeds friend-galaxy tint + wormhole-gate color
    # Auditable EULA acknowledgment (App Review 1.2 #4). A local-only flag is
    # fragile across reinstall, so consent lives server-side on the profile.
    accepted_terms_at = models.DateTimeField(null=True, blank=True)
    accepted_terms_version = models.CharField(max_length=20, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "neighbor_profiles"
        indexes = [models.Index(fields=["handle"])]

    def __str__(self) -> str:
        return f"@{self.handle}"


class Friendship(models.Model):
    """The consent atom — one row per unordered pair (``pair_key``).

    Direction is preserved for "who waved whom" and for the asymmetric block
    state. ``accepted`` unlocks mutual visibility; ``blocked`` freezes it and
    forbids re-invite. ``are_neighbors`` (in :mod:`apps.friends.access`) is
    True iff an ``accepted`` row exists for the pair AND it is not ``blocked``.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        REVOKED = "revoked", "Revoked"
        BLOCKED = "blocked", "Blocked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requester = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="waves_sent")
    addressee = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="waves_received")
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")  # audit
    # DB-enforced single edge: f"{min(a,b)}:{max(a,b)}" of the two tenant UUIDs
    # (36+1+36 = 73 chars). Computed on save. The ONLY dedup — never
    # service-layer-only (that races to dup edges).
    pair_key = models.CharField(max_length=73, unique=True, editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    blocked_by = models.ForeignKey(Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    requested_via = models.CharField(max_length=16, default="handle")  # handle | link | qr | referral
    invite = models.ForeignKey("FriendInvite", on_delete=models.SET_NULL, null=True, blank=True)
    invite_note = models.CharField(max_length=280, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friendships"
        constraints = [
            models.UniqueConstraint(fields=["pair_key"], name="uq_friendship_pair"),
            models.CheckConstraint(
                condition=~models.Q(requester=models.F("addressee")),
                name="friendship_no_self",
            ),
        ]
        indexes = [
            models.Index(fields=["addressee", "status"]),
            models.Index(fields=["requester", "status"]),
        ]

    def save(self, *args, **kwargs):
        # ``pair_key`` is DB-unique + editable=False; always recompute it from
        # the FKs so it can never drift from requester/addressee.
        self.pair_key = compute_pair_key(self.requester_id, self.addressee_id)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.pair_key} ({self.status})"


class FriendInvite(models.Model):
    """Bring a neighbor in via link/QR — including non-subscribers (the
    referral loop). High-entropy, single-use by default, expiring.

    The ``circle`` FK from the full design (§2.3) lands with PR7 when the
    ``Circle`` model exists; PR0 ships the handle/link path only.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inviter = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="friend_invites")
    token = models.CharField(max_length=64, unique=True)  # secrets.token_urlsafe(32), high-entropy
    circle = models.ForeignKey("Circle", on_delete=models.CASCADE, null=True, blank=True)  # optional: into a Circle
    prefill_email = models.EmailField(blank=True)
    max_uses = models.PositiveIntegerField(default=1)
    uses = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "friend_invites"
        indexes = [models.Index(fields=["token"])]

    def __str__(self) -> str:
        return f"invite:{self.token[:8]}…"


class SharedLesson(models.Model):
    """A FROZEN, PII-scrubbed snapshot of one ``Lesson``, safe to show ANY
    neighbor. NO rehydration map is ever attached — the recipient must be
    structurally unable to un-scrub it (design §2.4).

    One scrub serves every audience (the neutralized text carries no
    per-recipient info), so this is OneToOne on the source lesson and
    friend-agnostic. WHO may see it is a separate concern (``LessonShareGrant``).
    Every ``.objects`` query for this model lives in :mod:`apps.friends.access`
    (chokepoint-enforced).
    """

    class ScrubStatus(models.TextChoices):
        PENDING = "pending", "Scrub pending"
        READY = "ready", "Scrubbed & publishable"
        FAILED = "failed", "Blocked (fail-closed)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_lesson = models.OneToOneField("lessons.Lesson", on_delete=models.CASCADE, related_name="shared_snapshot")
    owner_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="shared_lessons")

    # ── FROZEN, NEUTRALIZED payload (every [TYPE_N] → a generic word; NO map) ──
    redacted_text = models.TextField(blank=True)
    redacted_context = models.TextField(blank=True)
    tags = ArrayField(models.CharField(max_length=100), default=list)  # allowlisted safe subset
    cluster_label = models.CharField(max_length=200, blank=True)  # scrubbed

    # ── Snapshot galaxy geometry (owner's tenant-local coords, COPIED at freeze) ──
    position_x = models.FloatField(null=True, blank=True)
    position_y = models.FloatField(null=True, blank=True)
    star_stage = models.CharField(max_length=20, default="proto")

    # ── Fail-closed scrub lifecycle ──
    content_hash = models.CharField(max_length=64, blank=True)  # sha256(text+ctx) → drift → re-scrub
    scrub_status = models.CharField(max_length=10, choices=ScrubStatus.choices, default=ScrubStatus.PENDING)
    scrub_model_version = models.CharField(max_length=40, blank=True)  # NER model version → re-scrub sweep
    scrub_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    scrubbed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "shared_lessons"
        indexes = [models.Index(fields=["owner_tenant", "scrub_status"])]

    def __str__(self) -> str:
        return f"shared_lesson:{self.id} ({self.scrub_status})"


class LessonShareGrant(models.Model):
    """WHO may see a ``SharedLesson``. Exactly one audience per row. Revocation
    is per-grant — flip status=revoked and access dies instantly with zero
    residue (read-through model, design §2.5).

    PR2 ships the ``friendship`` audience only; the ``circle`` FK + the full
    ``friendship XOR circle`` XOR constraint land in PR7. The named constraint
    below is intentionally kept so PR7 widens it (condition change) rather than
    introducing a new one. Every ``.objects`` query lives in
    :mod:`apps.friends.access` (chokepoint-enforced).
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REVOKED = "revoked", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_lesson = models.ForeignKey(SharedLesson, on_delete=models.CASCADE, related_name="grants")
    friendship = models.ForeignKey(
        Friendship, on_delete=models.CASCADE, null=True, blank=True, related_name="lesson_grants"
    )
    circle = models.ForeignKey("Circle", on_delete=models.CASCADE, null=True, blank=True, related_name="lesson_grants")
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "lesson_share_grants"
        constraints = [
            # Exactly one audience — a friendship XOR a circle (design §2.5 final form).
            models.CheckConstraint(
                condition=models.Q(friendship__isnull=False) ^ models.Q(circle__isnull=False),
                name="grant_exactly_one_audience",
            ),
            # Partial-unique per audience → one grant per (lesson, friendship) and per (lesson, circle).
            models.UniqueConstraint(
                fields=["shared_lesson", "friendship"],
                name="uq_grant_friendship",
                condition=models.Q(friendship__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["shared_lesson", "circle"],
                name="uq_grant_circle",
                condition=models.Q(circle__isnull=False),
            ),
        ]
        indexes = [
            models.Index(fields=["friendship", "status"]),
            models.Index(fields=["circle", "status"]),
        ]

    def __str__(self) -> str:
        return f"grant:{self.id} ({self.status})"


class PendingShare(models.Model):
    """Agent proposes / human approves a share (design §2.6). The agent writes
    ``proposed_by="agent", status="pending"`` and can NEVER flip it to approved
    — a human approve is the only path that creates a ``LessonShareGrant``.

    MVP: ``source_lesson`` is REQUIRED (an existing star only; the
    propose-share-NEW path is deferred). ``target_circle`` lands in PR7.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        EDITED = "edited", "Edited & approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        BLOCKED = "blocked", "Blocked (scrub failed)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="pending_shares"
    )  # author's HUMAN approves
    source_lesson = models.ForeignKey("lessons.Lesson", on_delete=models.CASCADE, related_name="pending_shares")
    proposed_by = models.CharField(max_length=8, default="agent")  # agent | user
    source_context = models.TextField(blank=True)  # agent's private "why" — never egressed
    preview_text = models.TextField(blank=True)  # convenience mirror of the scrubbed text at approve time
    final_text = models.TextField(blank=True)  # what the human actually approved (post-edit)
    target_friendship = models.ForeignKey(Friendship, on_delete=models.CASCADE, null=True, blank=True)
    target_circle = models.ForeignKey("Circle", on_delete=models.CASCADE, null=True, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    line_message_token = models.CharField(max_length=120, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)  # APNs one-push claim
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()  # +7d, like PendingExtraction

    class Meta:
        db_table = "pending_shares"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["target_friendship", "status"]),
        ]

    def __str__(self) -> str:
        return f"pending_share:{self.id} ({self.status})"


class WormholeVisit(models.Model):
    """The "new since last visit" watermark for a viewer's wormhole (design §2.12).

    A wormhole is a DERIVED query (one gate per accepted neighbor with ≥1
    active+ready grant to the viewer), never a materialized render table. The
    only persisted piece is this tiny per-(viewer, friendship) watermark, so we
    can show "N new since last visit": ``new`` = count of active+ready grants to
    the viewer for that friendship whose ``created_at > last_visited_at``.
    Gate PLACEMENT is deterministic client-side from a stable hash of
    ``friendship_id`` — never stored here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    viewer_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="wormhole_visits")
    friendship = models.ForeignKey(Friendship, on_delete=models.CASCADE, related_name="+")
    last_visited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friend_wormhole_visits"
        constraints = [
            models.UniqueConstraint(fields=["viewer_tenant", "friendship"], name="uq_wormhole_visit"),
        ]

    def __str__(self) -> str:
        return f"wormhole_visit:{self.viewer_tenant_id}:{self.friendship_id}"


class AbsorbedItem(models.Model):
    """Transparency + purge ledger (design §2.8). STRICTLY a pointer ledger —
    there is deliberately NO ``summary``/knowledge field. The knowledge lives
    only in the source rows (``SharedLesson`` / ``FriendMessage``); the agent's
    awareness is re-derived from the live accessor-filtered rows each envelope
    render. ``label`` is a display-only denormalized title, not the knowledge.

    Purging sets ``purged_at`` (a tombstone) → the envelope render excludes it
    and the agent stops surfacing it. Because the knowledge itself lives only in
    the source rows, purge + source-revocation together give complete, honest
    control.
    """

    class SourceKind(models.TextChoices):
        SHARED_LESSON = "shared_lesson", "Shared spark"
        FRIEND_MESSAGE = "friend_message", "Chat"  # PR5

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="absorbed_items")  # the absorbER
    source_kind = models.CharField(max_length=16, choices=SourceKind.choices)
    source_id = models.UUIDField()  # SharedLesson.id or FriendMessage.public_id — the REAL row
    from_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    circle = models.ForeignKey(
        "Circle", on_delete=models.SET_NULL, null=True, blank=True
    )  # circle-scoped purge/leak-tag
    label = models.CharField(max_length=200, blank=True)  # display-only denormalized title (NOT the knowledge)
    absorbed_at = models.DateTimeField(auto_now_add=True)
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friend_absorbed_items"
        constraints = [
            # Idempotent absorb: one ledger row per (absorber, source) — the
            # since-cursor + this constraint make re-absorb a no-op.
            models.UniqueConstraint(fields=["tenant", "source_kind", "source_id"], name="uq_absorbed_item"),
        ]
        indexes = [
            models.Index(fields=["tenant", "purged_at"]),
            models.Index(fields=["tenant", "from_tenant"]),
        ]

    def __str__(self) -> str:
        return f"absorbed:{self.tenant_id}:{self.source_kind}:{self.source_id}"


class FriendThread(models.Model):
    """A cross-tenant 1:1 chat thread (design §2.7). ``router.ChatThread`` is
    tenant-FK'd + ``is_main``-per-tenant, so it actively forbids a cross-tenant
    thread — hence a new control-plane table. Circle threads land in PR7."""

    class Kind(models.TextChoices):
        DIRECT = "direct", "1:1"
        CIRCLE = "circle", "Circle"  # PR7

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.DIRECT)
    friendship = models.ForeignKey(Friendship, on_delete=models.CASCADE, null=True, blank=True, related_name="thread")
    circle = models.ForeignKey("Circle", on_delete=models.CASCADE, null=True, blank=True, related_name="threads")
    title = models.CharField(max_length=160, blank=True)
    created_by = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="threads_started")
    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "friend_threads"
        constraints = [
            # One direct thread per edge (PR7 adds circle threads under kind=circle).
            models.UniqueConstraint(
                fields=["friendship"], name="uq_direct_thread", condition=models.Q(friendship__isnull=False)
            ),
        ]

    def __str__(self) -> str:
        return f"thread:{self.id} ({self.kind})"


class FriendThreadMembership(models.Model):
    """A tenant's membership in a FriendThread. Carries the per-member absorb +
    read cursors and the mute/absorb toggles."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(FriendThread, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="thread_memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=8, default="member")  # member | admin
    muted = models.BooleanField(default=False)  # mute APNs nudges
    agent_absorb_enabled = models.BooleanField(default=True)  # mute MY agent's absorption of THIS thread
    last_read_seq = models.BigIntegerField(default=0)  # unread counts
    last_absorbed_seq = models.BigIntegerField(default=0)  # idempotent agent-absorb cursor
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friend_thread_memberships"
        constraints = [models.UniqueConstraint(fields=["thread", "tenant"], name="uq_thread_member")]
        indexes = [models.Index(fields=["tenant", "left_at"])]

    def __str__(self) -> str:
        return f"member:{self.thread_id}:{self.tenant_id}"


class FriendMessage(models.Model):
    """Plain human-authored text (design §2.7/§4.6). NOT agent-scrubbed — a human
    chose these words for another human (consent by typing); it carries no
    per-tenant ``[PERSON_N]`` placeholders. Absorption into each member's agent
    applies THAT tenant's own egress redaction fresh. Every ``.objects`` query
    is confined to :mod:`apps.friends.access` (chokepoint)."""

    seq = models.BigAutoField(primary_key=True)  # monotonic → cheap keyset tiebreaker
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)  # client-facing id
    thread = models.ForeignKey(FriendThread, on_delete=models.CASCADE, related_name="messages")
    sender_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    sender_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    client_msg_id = models.CharField(max_length=64)  # offline-outbox idempotency
    text = models.TextField()
    notified_at = models.DateTimeField(null=True, blank=True)  # coarse one-push claim (isnull→now)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)  # self-delete / moderation

    class Meta:
        db_table = "friend_messages"
        constraints = [
            models.UniqueConstraint(fields=["sender_tenant", "client_msg_id"], name="uq_friend_msg_idem"),
        ]
        indexes = [models.Index(fields=["thread", "created_at", "seq"])]  # keyset feed

    def __str__(self) -> str:
        return f"msg:{self.seq}:{self.thread_id}"


class SharedGoal(models.Model):
    """A cross-tenant shared goal — product name **Mission** (design §2.9). NOT a
    stretched ``journal.Goal`` (RLS + same-tenant validate() forbid a cross-tenant
    FK). Each member's contribution stays as their OWN local ``journal.Task`` rows
    linked by ``Task.related_ref`` — zero schema change to journal.Task.

    Every ``SharedGoal.objects`` query lives in :mod:`apps.friends.access`
    (chokepoint). PR6 ships the ``friendship`` audience (1:1 missions); the
    ``circle`` FK + circle missions land in PR7."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ACHIEVED = "achieved", "Achieved"
        ABANDONED = "abandoned", "Abandoned"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    pillar = models.CharField(max_length=20, blank=True)
    friendship = models.ForeignKey(Friendship, on_delete=models.SET_NULL, null=True, blank=True)  # 1:1 mission
    circle = models.ForeignKey(
        "Circle", on_delete=models.SET_NULL, null=True, blank=True, related_name="missions"
    )  # circle mission
    created_by = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="missions_created")
    target = models.JSONField(default=dict)  # {metric, unit, cadence:"daily", value:10000}
    target_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    # Object-level multi-writer safety = Fuel's optimistic concurrency (Workout pattern).
    version = models.PositiveIntegerField(default=0)
    edit_lock_until = models.DateTimeField(null=True, blank=True)
    edit_lock_owner = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    achieved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "shared_goals"

    def __str__(self) -> str:
        return f"mission:{self.id} ({self.status})"


class SharedGoalMembership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_goal = models.ForeignKey(SharedGoal, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="mission_memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=8, default="member")  # owner | member
    status = models.CharField(max_length=8, default="active")  # invited | active | left
    commitment = models.CharField(max_length=200, blank=True)  # "what I'll do"
    # Idempotency for the weekly digest — compare-and-set per (member, iso-week).
    last_digest_window = models.CharField(max_length=24, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "shared_goal_memberships"
        constraints = [models.UniqueConstraint(fields=["shared_goal", "tenant"], name="uq_mission_member")]
        indexes = [models.Index(fields=["tenant", "left_at"])]

    def __str__(self) -> str:
        return f"mission_member:{self.shared_goal_id}:{self.tenant_id}"


class SharedGoalUpdate(models.Model):
    """Append-only activity log — THE single stream that feeds the status
    projection, the digest, AND the envelope (design §2.9). Crew progress reads
    from control-plane data only, never a cross-tenant Task scan in a request."""

    class Kind(models.TextChoices):
        JOINED = "joined", "Joined"
        TASK_ADDED = "task_added", "Task added"
        TASK_COMPLETED = "task_completed", "Task completed"
        MILESTONE = "milestone", "Milestone"
        NOTE = "note", "Note"
        PROGRESS = "progress", "Progress"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_goal = models.ForeignKey(SharedGoal, on_delete=models.CASCADE, related_name="updates")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    text = models.TextField(blank=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "shared_goal_updates"

    def __str__(self) -> str:
        return f"mission_update:{self.shared_goal_id}:{self.kind}"


class PendingGoalAction(models.Model):
    """Agent proposes a Mission task for ITS OWN human (design §2.10). Human-gated
    — approve mints the member's own local ``journal.Task``. The agent NEVER
    writes another human's task."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="pending_goal_actions")
    shared_goal = models.ForeignKey(SharedGoal, on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, default="add_task")  # add_task | complete_task | note
    suggested = models.JSONField(default=dict)  # {title, description, due_date}
    status = models.CharField(max_length=10, default="pending")  # pending | approved | rejected | expired
    task = models.ForeignKey("journal.Task", on_delete=models.SET_NULL, null=True, blank=True)  # minted on approve
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    line_message_token = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "pending_goal_actions"
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self) -> str:
        return f"pending_goal_action:{self.id} ({self.status})"


class Circle(models.Model):
    """A named set of accepted neighbors (the blueprint's Group; design §2.11).
    Built ON edges — you must be a member's neighbor, or claim an invite, to join.
    Membership itself is the consent grant inside a Circle."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    hue = models.PositiveSmallIntegerField(default=210)  # galaxy tint
    created_by = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="circles_created")
    invite_code = models.CharField(max_length=64, unique=True)  # link/QR (reuse linking pattern)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "friend_circles"

    def __str__(self) -> str:
        return f"circle:{self.id} ({self.name})"


class CircleMembership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    circle = models.ForeignKey(Circle, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="circle_memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=8, default="member")  # member | admin
    share_preferences = models.JSONField(default=dict)  # categories the agent MAY suggest sharing
    agent_absorb_enabled = models.BooleanField(default=True)
    muted = models.BooleanField(default=False)
    status = models.CharField(max_length=8, default="active")  # active | left | removed
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friend_circle_memberships"
        constraints = [models.UniqueConstraint(fields=["circle", "tenant"], name="uq_circle_tenant")]
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self) -> str:
        return f"circle_member:{self.circle_id}:{self.tenant_id}"


class ContentReport(models.Model):
    """MVP moderation: report + block + owner-unshare (design §2.10). No global
    queue at launch scale — shares are scoped + human-approved + identity-scrubbed,
    so the blast radius is small. A report hides the item for the REPORTER."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporter_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    reporter_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    target_kind = models.CharField(
        max_length=16,
        choices=[
            ("shared_lesson", "Shared spark"),
            ("friend_message", "Chat"),
            ("general", "General / support"),  # Settings → Support "Report a concern" (no content id)
        ],
    )
    shared_lesson = models.ForeignKey(SharedLesson, on_delete=models.CASCADE, null=True, blank=True)
    friend_message = models.ForeignKey(FriendMessage, on_delete=models.CASCADE, null=True, blank=True)
    reason = models.CharField(max_length=280)
    status = models.CharField(max_length=12, default="open")  # open | hidden | dismissed
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "content_reports"
        indexes = [models.Index(fields=["reporter_tenant", "status"])]

    def __str__(self) -> str:
        return f"report:{self.id} ({self.target_kind})"
