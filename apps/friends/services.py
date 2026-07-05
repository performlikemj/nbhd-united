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
from collections import Counter
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.tenants.models import Tenant

from . import access
from .models import FriendInvite, Friendship, NeighborProfile, PendingShare, SharedLesson, compute_pair_key
from .scrub import _content_hash

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
    except (Friendship.DoesNotExist, ValueError, DjangoValidationError, ValidationError) as exc:
        # DjangoValidationError: a malformed UUID reaches here from callback
        # paths (the console URLs are <uuid:>-guarded, callbacks are not).
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


# ── Share pipeline (propose → scrub → preview → approve → freeze → publish) ───

RESIDUALS_BANNER = "We hide names — but not amounts, dates, or company names."

# Mechanical share-never list, independent of the LLM (design §4.7). Lessons in
# these pillars refuse to share by default; MJ can opt a pillar in later.
SHARE_BLOCKED_PILLARS = frozenset({"gravity", "core"})

# NOTE: ``lessons.Lesson`` has no ``pillar`` column, so pillar is inferred from
# tags/an explicit attr as a conservative, over-blocking heuristic. A first-
# class provenance signal (a pillar field, or finance/core lesson origin) should
# replace this — flagged for the reviewer. Over-blocking is the safe direction.
_GRAVITY_MARKERS = frozenset({"gravity", "finance", "money", "debt", "budget", "savings", "loan", "salary", "invoice"})
_CORE_MARKERS = frozenset({"core", "mindfulness", "meditation", "mental-health", "mental health", "therapy"})


def lesson_pillar(lesson) -> str:
    explicit = getattr(lesson, "pillar", None)
    if explicit:
        return str(explicit).lower()
    tags = {str(t).strip().lower() for t in (getattr(lesson, "tags", None) or [])}
    if tags & _GRAVITY_MARKERS:
        return "gravity"
    if tags & _CORE_MARKERS:
        return "core"
    return "lessons"


def assert_shareable_pillar(lesson) -> None:
    if lesson_pillar(lesson) in SHARE_BLOCKED_PILLARS:
        raise PermissionDenied(
            "Finance and mindfulness lessons stay private by default — they can't be shared to your Neighborhood."
        )


def _enqueue_scrub(shared_lesson, pending_share_id=None) -> None:
    from apps.cron.publish import publish_task

    kwargs = {}
    if pending_share_id is not None:
        kwargs["pending_share_id"] = str(pending_share_id)
    publish_task("scrub_shared_lesson", str(shared_lesson.id), **kwargs)


def share_lesson(owner_tenant, owner_user, lesson, friendship_id) -> PendingShare:
    """Human-initiated share intent → a ``PendingShare(proposed_by="user")`` +
    an ensured ``SharedLesson`` + an enqueued fail-closed scrub. **No grant is
    created here** — the grant is created only at approve-after-preview (design
    §3.2: "no preview → no grant" binds every path, including this one)."""
    if lesson.tenant_id != owner_tenant.id:
        raise NotFound("No such lesson.")
    assert_shareable_pillar(lesson)  # mechanical gravity/core block → 403
    edge = access.assert_neighbors(owner_tenant, friendship_id)  # party + accepted, else 403

    shared_lesson = access.ensure_shared_lesson(lesson, owner_tenant)
    current_hash = _content_hash(lesson.text or "", lesson.context or "")
    if shared_lesson.scrub_status != SharedLesson.ScrubStatus.READY or shared_lesson.content_hash != current_hash:
        access.mark_scrub_pending(shared_lesson)
        _enqueue_scrub(shared_lesson)  # content_hash drift or first scrub → (re)scrub

    return PendingShare.objects.create(
        tenant=owner_tenant,
        source_lesson=lesson,
        proposed_by="user",
        target_friendship=edge,
        status=PendingShare.Status.PENDING,
        expires_at=timezone.now() + timedelta(days=7),
    )


def preview_share(owner_tenant, lesson_id, friendship_id) -> tuple[dict, int]:
    """Preview-before-share: the LITERAL ``redacted_text`` the neighbor will see.
    202 while scrubbing, 409 if the scrub failed (fail-closed), 200 when ready."""
    edge = access.assert_neighbors(owner_tenant, friendship_id)
    shared_lesson = access.get_shared_lesson_by_lesson_id(lesson_id, owner_tenant)
    if shared_lesson is None:
        raise NotFound("No share in progress for this lesson.")
    if shared_lesson.scrub_status == SharedLesson.ScrubStatus.PENDING:
        return {"detail": "Preparing your preview safely — try again in a moment."}, 202
    if shared_lesson.scrub_status == SharedLesson.ScrubStatus.FAILED:
        return {"detail": "We couldn't prepare this share safely, so nothing will be shared."}, 409
    return {
        "redacted_text": shared_lesson.redacted_text,
        "redacted_context": shared_lesson.redacted_context,
        "audience": _audience_label(owner_tenant, edge),
        "residuals_banner": RESIDUALS_BANNER,
    }, 200


def list_pending_shares(tenant) -> list[dict]:
    shares = (
        PendingShare.objects.filter(tenant=tenant, status=PendingShare.Status.PENDING)
        .select_related("source_lesson", "target_friendship")
        .order_by("-created_at")
    )
    out: list[dict] = []
    for pending in shares:
        edge = pending.target_friendship
        out.append(
            {
                "id": str(pending.id),
                "lesson_id": pending.source_lesson_id,
                "lesson_preview": (pending.source_lesson.text or "")[:140],
                "proposed_by": pending.proposed_by,
                "friendship_id": str(edge.id) if edge else None,
                "audience": _audience_label(tenant, edge) if edge else None,
                "created_at": pending.created_at,
            }
        )
    return out


def approve_share(tenant, pending_share_id, final_text=None) -> tuple[dict, int]:
    """Human approve. **The only path that creates a grant.** Idempotent.
    An edit re-scrubs the edited text fail-closed (returns 202 → preview again →
    approve); a ready snapshot freezes + publishes the grant."""
    pending = _load_pending_share(tenant, pending_share_id)
    if pending.status in (PendingShare.Status.APPROVED, PendingShare.Status.EDITED):
        return {"pending_share_id": str(pending.id), "status": pending.status}, 200
    if pending.status != PendingShare.Status.PENDING:
        raise ValidationError("This share can no longer be approved.")
    if pending.target_friendship_id is None:
        raise ValidationError("This share has no audience.")

    shared_lesson = access.get_shared_lesson_by_lesson_id(pending.source_lesson_id, tenant)
    if shared_lesson is None:
        raise ValidationError("No prepared snapshot to approve.")

    # Edit → re-scrub the edited text fail-closed, then the human previews +
    # approves again. This keeps "no preview → no grant" intact for edits too.
    edited = (final_text or "").strip()
    if edited and edited != (pending.final_text or "").strip():
        pending.final_text = edited
        pending.save(update_fields=["final_text"])
        access.mark_scrub_pending(shared_lesson)
        _enqueue_scrub(shared_lesson, pending_share_id=pending.id)
        return {
            "pending_share_id": str(pending.id),
            "status": "rescrubbing",
            "detail": "We re-scrubbed your edit — preview it, then approve to share.",
        }, 202

    if shared_lesson.scrub_status == SharedLesson.ScrubStatus.PENDING:
        return {"detail": "Still preparing this share safely — try again in a moment."}, 202
    if shared_lesson.scrub_status == SharedLesson.ScrubStatus.FAILED:
        pending.status = PendingShare.Status.BLOCKED
        pending.resolved_at = timezone.now()
        pending.save(update_fields=["status", "resolved_at"])
        return {"detail": "We couldn't prepare this share safely, so nothing was shared."}, 409

    # READY → freeze + publish (create the grant).
    grant = access.create_grant(shared_lesson, pending.target_friendship, granted_by=tenant.user)
    pending.status = PendingShare.Status.EDITED if pending.final_text else PendingShare.Status.APPROVED
    pending.preview_text = shared_lesson.redacted_text
    pending.resolved_at = timezone.now()
    pending.save(update_fields=["status", "preview_text", "resolved_at"])
    return {"pending_share_id": str(pending.id), "status": pending.status, "grant_id": str(grant.id)}, 200


def reject_share(tenant, pending_share_id) -> PendingShare:
    pending = _load_pending_share(tenant, pending_share_id)
    if pending.status == PendingShare.Status.PENDING:
        pending.status = PendingShare.Status.REJECTED
        pending.resolved_at = timezone.now()
        pending.save(update_fields=["status", "resolved_at"])
    return pending


def revoke_share(owner_tenant, lesson, grant_id) -> None:
    """Revoke one share → the spark leaves the neighbor's wormhole + absorb pull
    instantly (read-through, zero residue)."""
    grant = access.get_grant(grant_id)
    if (
        grant is None
        or grant.shared_lesson.owner_tenant_id != owner_tenant.id
        or grant.shared_lesson.source_lesson_id != lesson.id
    ):
        raise NotFound("No such share.")
    access.revoke_grant(grant)


def _load_pending_share(tenant, pending_share_id) -> PendingShare:
    try:
        pending = PendingShare.objects.select_related("target_friendship").get(id=pending_share_id, tenant=tenant)
    except (PendingShare.DoesNotExist, ValueError, DjangoValidationError) as exc:
        raise NotFound("No such share.") from exc
    return pending


def _audience_label(viewer_tenant, edge) -> str:
    """The neighbor's display name for a friendship edge (owner's view)."""
    if edge is None:
        return "a neighbor"
    other_id = edge.addressee_id if edge.requester_id == viewer_tenant.id else edge.requester_id
    profile = NeighborProfile.objects.filter(tenant_id=other_id).first()
    return profile.display_name if profile else "a neighbor"


# ── Wormholes & warp (PR3) ────────────────────────────────────────────────────


def list_wormholes(viewer_tenant) -> list[dict]:
    """Warp targets for the home galaxy: one gate per accepted neighbor with ≥1
    active+ready spark shared to the viewer. Addressed only by ``friendship_id``
    — never a neighbor's tenant_id. Gate placement is deterministic client-side
    from a stable hash of ``friendship_id``; the payload carries the neighbor's
    identity, spark count, and the "new since last visit" glow count.
    """
    targets = access.wormhole_targets(viewer_tenant)
    owner_ids = [t["owner_id"] for t in targets]
    profiles = {p.tenant_id: p for p in NeighborProfile.objects.filter(tenant_id__in=owner_ids)}
    out: list[dict] = []
    for target in targets:
        profile = profiles.get(target["owner_id"])
        out.append(
            {
                "friendship_id": str(target["friendship"].id),
                "display_name": profile.display_name if profile else "Neighbor",
                "handle": profile.handle if profile else None,
                "avatar_hue": profile.avatar_hue if profile else 210,
                "spark_count": target["spark_count"],
                "new_since_last_visit": target["new_since_last_visit"],
            }
        )
    out.sort(key=lambda w: (w["display_name"] or "").lower())
    return out


def friend_galaxy(viewer_tenant, friendship_id) -> dict:
    """The neighbor's SHARED constellation as the exact ``GalaxyData`` shape the
    game consumes (``{stars, edges, clusters}``), built server-side from
    ``SharedLesson`` snapshots via the audited accessor — READ-ONLY.

    Only ``ready`` + active-granted snapshots are returned (``shared_star_qs``);
    the raw ``Lesson`` corpus, connections, and star journals are never touched.
    Star ids are namespaced ``f:<friendship_id>:<shared_lesson_id>`` so they can
    never collide with home-galaxy ``Lesson`` PKs or be replayed against
    owner-scoped endpoints. Edges are OMITTED for MVP (one less leak surface);
    clusters are derived from the shared subset's ``cluster_label`` values only.
    """
    edge = access.assert_neighbors(viewer_tenant, friendship_id)  # party + accepted, else 403
    owner_id = access.other_party_id(edge, viewer_tenant)
    snapshots = list(access.shared_star_qs(viewer_tenant, owner_id))

    # Synthesize a stable integer cluster_id per distinct non-empty label within
    # the shared subset (SharedLesson carries a scrubbed label, not a numeric id).
    labels = sorted({s.cluster_label for s in snapshots if s.cluster_label})
    label_to_id = {label: idx for idx, label in enumerate(labels)}

    stars = []
    for snap in snapshots:
        cluster_id = label_to_id.get(snap.cluster_label) if snap.cluster_label else None
        stars.append(
            {
                "id": f"f:{friendship_id}:{snap.id}",
                "shared_lesson_id": str(snap.id),
                "text": snap.redacted_text,
                "tags": list(snap.tags or []),
                "cluster_id": cluster_id,
                "cluster_label": snap.cluster_label,
                "star_stage": snap.star_stage or "proto",
                "x": snap.position_x,
                "y": snap.position_y,
            }
        )

    counts: dict[str, int] = {}
    tag_bags: dict[str, list] = {}
    for snap in snapshots:
        if not snap.cluster_label:
            continue
        counts[snap.cluster_label] = counts.get(snap.cluster_label, 0) + 1
        tag_bags.setdefault(snap.cluster_label, []).extend(snap.tags or [])
    clusters = [
        {
            "id": label_to_id[label],
            "label": label,
            "count": counts.get(label, 0),
            "tags": [tag for tag, _n in Counter(tag_bags.get(label, [])).most_common(3)],
        }
        for label in labels
    ]
    return {"stars": stars, "edges": [], "clusters": clusters}


def mark_wormhole_visited(viewer_tenant, friendship_id) -> dict:
    """Advance the viewer's ``WormholeVisit`` watermark for a friendship (kills
    the "new since last visit" glow). Party + accepted checked via the accessor."""
    edge = access.assert_neighbors(viewer_tenant, friendship_id)
    visit = access.upsert_wormhole_visit(viewer_tenant, edge)
    return {"friendship_id": str(edge.id), "last_visited_at": visit.last_visited_at}


def adopt_spark(viewer_tenant, viewer_user, shared_lesson_id) -> tuple[dict, int]:
    """Souvenir: bring a neighbor's spark home as a PENDING lesson in the viewer's
    own tenant (design §8). Idempotent per snapshot. 201 on create, 200 on an
    existing adopt; 400 when adopting your own snapshot; 403 with no active grant.
    """
    try:
        lesson, created = access.adopt_shared_lesson(viewer_tenant, viewer_user, shared_lesson_id)
    except ValueError:
        raise ValidationError("You can't bring home your own spark — it's already in your galaxy.")
    return (
        {"lesson_id": lesson.id, "status": lesson.status, "created": created},
        201 if created else 200,
    )


def refresh_shared_positions(tenant_id) -> dict:
    """QStash task body: copy-forward a tenant's current lesson coords onto their
    ready shared snapshots (coords only). Debounced after a constellation
    recluster (see apps/lessons/clustering.refresh_constellation)."""
    tenant = Tenant.objects.filter(id=tenant_id).first()
    if tenant is None:
        return {"updated": 0, "reason": "no such tenant"}
    updated = access.refresh_shared_positions_for_owner(tenant)
    return {"updated": updated}
