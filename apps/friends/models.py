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
