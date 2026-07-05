"""Neighborhood service layer — waves, responses, profiles, invites.

All the edge-state logic lives here so the DRF views (console) and the router
callbacks (Telegram/LINE wave buttons) share one implementation. PR1 touches
only ``Friendship`` / ``NeighborProfile`` / ``FriendInvite`` (not restricted by
the chokepoint); any "are these two neighbors" question routes through
:mod:`apps.friends.access`.

Invariant that binds every path: the friendship edge is deduped at the DATABASE
(``pair_key`` unique), never in a service check — a reciprocal or concurrent
wave collides on the same row (see :func:`send_wave`).
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from .models import FriendInvite, Friendship, NeighborProfile, compute_pair_key

# Handles people can never claim (impersonation / support-desk confusion).
RESERVED_HANDLES = frozenset({"admin", "nbhd", "neighborhood", "support", "mj"})

_VALID_HANDLE_RE = re.compile(r"^[a-z0-9_]{3,30}$")
_HANDLE_STRIP_RE = re.compile(r"[^a-z0-9_]")

MAX_INVITE_USES = 50
MAX_INVITE_DAYS = 90
DEFAULT_INVITE_DAYS = 14


# ── Profiles ────────────────────────────────────────────────────────────────


def derive_unique_handle(base: str) -> str:
    """A DB-unique, non-reserved handle seeded from ``base`` (display name /
    username). Sanitized to ``[a-z0-9_]``, 3-30 chars, numeric suffix on
    collision."""
    slug = _HANDLE_STRIP_RE.sub("", (base or "").lower()) or "neighbor"
    slug = slug[:26]  # leave headroom for a disambiguating suffix
    if len(slug) < 3:
        slug = (slug + "neighbor")[:26]
    candidate = slug
    i = 0
    while candidate in RESERVED_HANDLES or NeighborProfile.objects.filter(handle=candidate).exists():
        i += 1
        candidate = f"{slug}{i}"[:30]
    return candidate


def ensure_neighbor_profile(tenant, user=None) -> NeighborProfile:
    """Get-or-create the tenant's ``NeighborProfile`` with a derived unique
    handle. Called on any action that needs the actor to be visible to a
    neighbor (wave send, invite claim, profile GET)."""
    profile = NeighborProfile.objects.filter(tenant=tenant).first()
    if profile is not None:
        return profile
    user = user or tenant.user
    display = (getattr(user, "display_name", None) or getattr(user, "username", None) or "Neighbor").strip()[:80]
    for _attempt in range(5):
        handle = derive_unique_handle(display or "neighbor")
        try:
            with transaction.atomic():
                return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=display or "Neighbor")
        except IntegrityError:
            # Lost a race — either the profile now exists (OneToOne) or the
            # handle was taken concurrently. Re-check the profile, else retry.
            existing = NeighborProfile.objects.filter(tenant=tenant).first()
            if existing is not None:
                return existing
    # Extremely unlikely: fall back to a random handle.
    return NeighborProfile.objects.create(
        tenant=tenant, handle=f"neighbor_{secrets.token_hex(6)}", display_name=display or "Neighbor"
    )


def validate_handle(handle: str, tenant) -> str:
    """Normalize + validate a user-chosen handle. Raises DRF ``ValidationError``
    (→ 400) with a human message."""
    handle = (handle or "").strip().lower()
    if not _VALID_HANDLE_RE.match(handle):
        raise ValidationError("Handle must be 3-30 characters: lowercase letters, numbers, or underscore.")
    if handle in RESERVED_HANDLES:
        raise ValidationError("That handle is reserved.")
    if NeighborProfile.objects.filter(handle=handle).exclude(tenant=tenant).exists():
        raise ValidationError("That handle is already taken.")
    return handle


# ── Neighborhood listing ─────────────────────────────────────────────────────


def _profile_entry(tenant, profiles_by_id: dict) -> dict:
    """display_name / handle / avatar_hue for a tenant, from a prefetched map,
    falling back to the auth user's display name when they have no profile."""
    profile = profiles_by_id.get(tenant.id)
    if profile is not None:
        return {
            "display_name": profile.display_name,
            "handle": profile.handle,
            "avatar_hue": profile.avatar_hue,
        }
    return {
        "display_name": (getattr(tenant.user, "display_name", None) or "Neighbor"),
        "handle": None,
        "avatar_hue": 210,
    }


def list_neighborhood(tenant) -> dict:
    """Accepted neighbors + pending waves in/out, plus the caller's own profile.
    Addresses only by ``friendship_id`` — never leaks a neighbor's tenant_id."""
    me = ensure_neighbor_profile(tenant, tenant.user)
    edges = list(
        Friendship.objects.filter(Q(requester=tenant) | Q(addressee=tenant)).select_related(
            "requester", "requester__user", "addressee", "addressee__user"
        )
    )
    other_ids = [(e.addressee_id if e.requester_id == tenant.id else e.requester_id) for e in edges]
    profiles_by_id = {p.tenant_id: p for p in NeighborProfile.objects.filter(tenant_id__in=other_ids)}

    neighbors, pending_incoming, pending_outgoing = [], [], []
    for edge in edges:
        other = edge.addressee if edge.requester_id == tenant.id else edge.requester
        entry = _profile_entry(other, profiles_by_id)
        if edge.status == Friendship.Status.ACCEPTED:
            neighbors.append(
                {"friendship_id": str(edge.id), "status": edge.status, "since": edge.responded_at, **entry}
            )
        elif edge.status == Friendship.Status.PENDING:
            bucket = pending_incoming if edge.addressee_id == tenant.id else pending_outgoing
            bucket.append(
                {
                    "friendship_id": str(edge.id),
                    "note": edge.invite_note,
                    "created_at": edge.created_at,
                    **entry,
                }
            )
        # declined / revoked / blocked are intentionally invisible

    neighbors.sort(key=lambda n: (n["display_name"] or "").lower())
    return {
        "profile": {
            "handle": me.handle,
            "display_name": me.display_name,
            "bio": me.bio,
            "avatar_hue": me.avatar_hue,
        },
        "neighbors": neighbors,
        "pending_incoming": pending_incoming,
        "pending_outgoing": pending_outgoing,
    }


# ── Waves ────────────────────────────────────────────────────────────────────


def send_wave(from_tenant, from_user, handle: str, note: str = "") -> tuple[Friendship, bool]:
    """Send a wave to the neighbor at ``@handle``. Returns ``(friendship,
    created)``.

    Idempotent + race-safe by construction: dedup is the ``pair_key`` unique
    constraint, so a concurrent duplicate collides at the DB and we return the
    winning row. A ``blocked`` edge is treated as "no such neighbor" (no-reveal).
    A re-wave after decline/revoke REUSES the row (flip to pending, set the
    current sender as requester). Waving back a pending incoming wave accepts it.
    """
    handle = (handle or "").strip().lower()
    target_profile = NeighborProfile.objects.select_related("tenant", "tenant__user").filter(handle=handle).first()
    if target_profile is None:
        raise NotFound("No neighbor with that handle.")
    target = target_profile.tenant
    if target.id == from_tenant.id:
        raise ValidationError("You can't wave to yourself.")

    ensure_neighbor_profile(from_tenant, from_user)  # so the addressee can see who waved
    pair = compute_pair_key(from_tenant.id, target.id)
    existing = Friendship.objects.filter(pair_key=pair).first()

    if existing is not None:
        if existing.status == Friendship.Status.BLOCKED:
            raise NotFound("No neighbor with that handle.")  # no-reveal
        if existing.status == Friendship.Status.ACCEPTED:
            return existing, False
        if existing.status == Friendship.Status.PENDING:
            if existing.addressee_id == from_tenant.id:
                # They already waved me — waving back accepts it (mutual consent).
                existing.status = Friendship.Status.ACCEPTED
                existing.responded_at = timezone.now()
                existing.save(update_fields=["status", "responded_at"])
                return existing, False
            return existing, False  # I already sent this wave — idempotent
        # declined or revoked → reuse the row, flip to pending in the new direction
        existing.requester = from_tenant
        existing.addressee = target
        existing.requested_by = from_user
        existing.status = Friendship.Status.PENDING
        existing.responded_at = None
        existing.revoked_at = None
        existing.blocked_by = None
        existing.invite_note = note or ""
        existing.requested_via = "handle"
        existing.save()
        _notify_wave_received(existing)
        return existing, False

    try:
        with transaction.atomic():
            edge = Friendship.objects.create(
                requester=from_tenant,
                addressee=target,
                requested_by=from_user,
                status=Friendship.Status.PENDING,
                invite_note=note or "",
                requested_via="handle",
            )
    except IntegrityError:
        # A concurrent wave won the race to the unique pair_key — return it.
        edge = Friendship.objects.filter(pair_key=pair).first()
        return edge, False
    _notify_wave_received(edge)
    return edge, True


def respond_to_wave(tenant, friendship_id, action: str) -> Friendship:
    """accept / decline (addressee only) or block (either party). Non-party →
    404 (no-reveal); requester trying to accept their own wave → 403. Idempotent
    on the terminal state."""
    edge = _load_edge_for_party(tenant, friendship_id)
    is_addressee = edge.addressee_id == tenant.id
    now = timezone.now()

    if action in ("accept", "decline"):
        if not is_addressee:
            raise PermissionDenied("Only the neighbor who was waved can respond to this wave.")
        target_status = Friendship.Status.ACCEPTED if action == "accept" else Friendship.Status.DECLINED
        if edge.status == target_status:
            return edge  # idempotent
        if edge.status != Friendship.Status.PENDING:
            raise ValidationError("This wave can no longer be answered.")
        edge.status = target_status
        edge.responded_at = now
        edge.save(update_fields=["status", "responded_at"])
        return edge

    if action == "block":
        if edge.status == Friendship.Status.BLOCKED:
            return edge  # idempotent
        edge.status = Friendship.Status.BLOCKED
        edge.blocked_by = tenant
        edge.responded_at = now
        edge.save(update_fields=["status", "blocked_by", "responded_at"])
        return edge

    raise ValidationError("Unknown action.")


def unfriend(tenant, friendship_id) -> Friendship:
    """Revoke an accepted/pending edge. A ``blocked`` edge is left intact (you
    can't un-block by unfriending). Idempotent."""
    edge = _load_edge_for_party(tenant, friendship_id)
    if edge.status in (Friendship.Status.ACCEPTED, Friendship.Status.PENDING):
        edge.status = Friendship.Status.REVOKED
        edge.revoked_at = timezone.now()
        edge.save(update_fields=["status", "revoked_at"])
    return edge


def _load_edge_for_party(tenant, friendship_id) -> Friendship:
    """Load the edge, or raise ``NotFound`` if it doesn't exist OR the caller
    isn't a party (no-reveal for both cases)."""
    try:
        edge = Friendship.objects.select_related("requester", "addressee").get(id=friendship_id)
    except (Friendship.DoesNotExist, ValueError, ValidationError) as exc:
        raise NotFound("No such wave.") from exc
    if tenant.id not in (edge.requester_id, edge.addressee_id):
        raise NotFound("No such wave.")
    return edge


# ── Invites ──────────────────────────────────────────────────────────────────


def create_invite(tenant, max_uses: int = 1, expires_in_days: int = DEFAULT_INVITE_DAYS) -> FriendInvite:
    """A high-entropy, expiring wave link (reuses the linking UX)."""
    try:
        max_uses = max(1, min(int(max_uses), MAX_INVITE_USES))
    except (TypeError, ValueError):
        max_uses = 1
    try:
        days = max(1, min(int(expires_in_days), MAX_INVITE_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_INVITE_DAYS
    return FriendInvite.objects.create(
        inviter=tenant,
        token=secrets.token_urlsafe(32),
        max_uses=max_uses,
        expires_at=timezone.now() + timedelta(days=days),
    )


def invite_metadata(token: str) -> dict:
    """Public (AllowAny) invite preview for the signup/accept page — inviter
    identity only, never anything private."""
    invite = FriendInvite.objects.select_related("inviter", "inviter__user").filter(token=token).first()
    if invite is None:
        raise NotFound("Invite not found.")
    profile = ensure_neighbor_profile(invite.inviter, invite.inviter.user)
    valid = invite.expires_at > timezone.now() and invite.uses < invite.max_uses
    return {
        "inviter_display_name": profile.display_name,
        "inviter_handle": profile.handle,
        "inviter_hue": profile.avatar_hue,
        "valid": valid,
    }


def claim_invite(tenant, user, token: str) -> Friendship:
    """An existing subscriber claims an invite → the edge resolves to
    ``accepted`` immediately (design §2.3). Non-subscriber signup→auto-accept
    is the documented PR1.5 seam (see views.InviteDetailView)."""
    invite = FriendInvite.objects.select_related("inviter", "inviter__user").filter(token=token).first()
    if invite is None:
        raise NotFound("Invite not found.")
    if invite.expires_at <= timezone.now():
        raise ValidationError("This invite has expired.")
    if invite.uses >= invite.max_uses:
        raise ValidationError("This invite has already been used.")
    inviter = invite.inviter
    if inviter.id == tenant.id:
        raise ValidationError("You can't claim your own invite.")

    ensure_neighbor_profile(tenant, user)
    ensure_neighbor_profile(inviter, inviter.user)
    pair = compute_pair_key(inviter.id, tenant.id)
    with transaction.atomic():
        edge = Friendship.objects.select_for_update().filter(pair_key=pair).first()
        if edge is None:
            edge = Friendship.objects.create(
                requester=inviter,
                addressee=tenant,
                requested_by=inviter.user,
                status=Friendship.Status.ACCEPTED,
                requested_via="link",
                invite=invite,
                responded_at=timezone.now(),
            )
        else:
            if edge.status == Friendship.Status.BLOCKED:
                raise NotFound("Invite not found.")  # no-reveal
            edge.status = Friendship.Status.ACCEPTED
            edge.responded_at = timezone.now()
            edge.invite = invite
            edge.save()
        FriendInvite.objects.filter(id=invite.id).update(uses=F("uses") + 1)
    return edge


# ── Notifications (thin wrapper; defensive, never raises into a request) ──────


def _notify_wave_received(friendship: Friendship) -> None:
    from .notifications import notify_wave_received

    notify_wave_received(friendship)
