"""The single audited cross-tenant accessor for the Neighborhood layer.

EVERY cross-tenant read in the whole feature — console, runtime, wormhole,
chat, Missions — routes through these functions. Nothing else hand-rolls a
cross-tenant query. The architectural CI test
:mod:`apps.friends.test_access_chokepoint` fails the build if any friends
module (or a friends runtime view) touches the cross-tenant model managers
``SharedLesson`` / ``FriendMessage`` / ``SharedGoal`` / ``LessonShareGrant``
``.objects`` outside this file, or references ``Lesson.objects`` anywhere
under ``apps/friends/``.

Why this is load-bearing: Django connects to Postgres as a **BYPASSRLS
superuser**, so RLS is NOT a tenant backstop today — cross-tenant isolation
is 100% the Python filters in this module until the PR8 ``FORCE ROW LEVEL
SECURITY`` role hardening. A single missing edge/tenant filter leaks another
user's private data with no DB net. Containing that risk to one audited
module is the entire point.

Addressing is always by opaque ``friendship_id`` / ``thread_id`` /
``circle_id``, never a client-supplied ``tenant_id`` — those ids exist only
if the relationship exists, and the accessor *still* re-verifies the caller is
a party (IDOR defeated by construction, design §4.5).
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone

from apps.tenants.models import Tenant

from .models import (
    FriendMessage,
    Friendship,
    FriendThread,
    FriendThreadMembership,
    LessonShareGrant,
    NeighborProfile,
    SharedLesson,
    WormholeVisit,
    compute_pair_key,
)


def _tenant_id(value):
    """Accept either a ``Tenant`` instance or a raw id and return the id."""
    return getattr(value, "id", value)


def are_neighbors(a: Tenant, b: Tenant) -> bool:
    """True iff an ``accepted`` Friendship exists for the pair AND no
    ``blocked`` row exists either direction.

    Direct edge ONLY — never transitive (no friend-of-friend). A ``blocked``
    edge supersedes ``accepted`` and makes this False. Self-pairs, missing
    edges, and pending/declined/revoked edges are all False. Never raises.

    ``pair_key`` is DB-unique, so there is at most one row per pair: when a
    side blocks, its ``status`` flips to ``blocked`` (not ``accepted``), so a
    single ``status == accepted`` check inherently excludes blocked in either
    direction.
    """
    if a is None or b is None:
        return False
    a_id, b_id = _tenant_id(a), _tenant_id(b)
    if a_id == b_id:
        return False
    edge = Friendship.objects.filter(pair_key=compute_pair_key(a_id, b_id)).first()
    if edge is None:
        return False
    return edge.status == Friendship.Status.ACCEPTED


def assert_neighbors(viewer_tenant, friendship_id) -> Friendship:
    """Return the ``accepted`` Friendship the viewer is a party to, or raise
    ``PermissionDenied``.

    Direct edge ONLY — never transitive. ``blocked`` (and any non-``accepted``)
    edge denies. This is the IDOR backstop: swapping in a stranger's
    ``friendship_id`` resolves a real row but fails the party check.
    """
    viewer_id = _tenant_id(viewer_tenant)
    try:
        edge = Friendship.objects.get(id=friendship_id)
    except Friendship.DoesNotExist as exc:
        raise PermissionDenied("No such friendship") from exc
    if viewer_id not in (edge.requester_id, edge.addressee_id):
        raise PermissionDenied("Not a party to this friendship")
    if edge.status != Friendship.Status.ACCEPTED:
        raise PermissionDenied("Friendship is not active")
    return edge


def shared_star_qs(viewer_tenant, owner_tenant):
    """``SharedLesson`` rows of ``owner_tenant`` visible to ``viewer_tenant``.

    Visibility = an ACTIVE ``LessonShareGrant`` exists whose ``friendship`` is
    the accepted edge between the two tenants, OR whose ``circle`` is one both
    are members of — AND ``scrub_status='ready'``. Returns an EMPTY queryset
    when no edge exists — it never raises into a data leak. This is the ONLY
    code in the whole feature permitted to omit a ``tenant=`` filter, because
    it substitutes the edge/grant check for that filter; everywhere else,
    dropping ``tenant=`` is a leak.

    Reads touch ONLY ``SharedLesson`` (ready) — never ``Lesson``,
    ``LessonConnection``, ``StarJournalEntry``, ``Document``, or ``journal``.

    PR2: the friendship-grant path. The circle path lands in PR7.
    """
    viewer_id, owner_id = _tenant_id(viewer_tenant), _tenant_id(owner_tenant)
    if viewer_id == owner_id:
        return SharedLesson.objects.none()
    edge = Friendship.objects.filter(
        pair_key=compute_pair_key(viewer_id, owner_id),
        status=Friendship.Status.ACCEPTED,
    ).first()
    if edge is None:
        return SharedLesson.objects.none()
    return SharedLesson.objects.filter(
        owner_tenant_id=owner_id,
        scrub_status=SharedLesson.ScrubStatus.READY,
        grants__friendship=edge,
        grants__status=LessonShareGrant.Status.ACTIVE,
    ).distinct()


def assert_can_write(viewer_tenant, target_tenant, *, allow_hibernated: bool = True) -> Tenant:
    """Gate a cross-tenant write (chat delivery, Mission nudge) from
    ``viewer_tenant`` into ``target_tenant``. Returns the target ``Tenant``.

    Requires an accepted (non-blocked) edge between the two (a shared Circle
    lands with PR7). Then gates on the target's lifecycle:

    * target ``SUSPENDED`` → raise ``PermissionDenied`` (store-only; never
      touch a lapsed tenant's container).
    * target ``HIBERNATED`` (``status=active`` with ``hibernated_at`` set) →
      allowed when ``allow_hibernated`` (default True); the caller decides
      whether to wake.
    """
    if not are_neighbors(viewer_tenant, target_tenant):
        raise PermissionDenied("Not neighbors — cross-tenant write forbidden")
    target = target_tenant if isinstance(target_tenant, Tenant) else Tenant.objects.get(id=_tenant_id(target_tenant))
    if target.status == Tenant.Status.SUSPENDED:
        raise PermissionDenied("Target tenant is suspended (store-only, no container touch)")
    if not allow_hibernated and target.hibernated_at is not None:
        raise PermissionDenied("Target tenant is hibernated and waking is not allowed here")
    return target


# ── SharedLesson + LessonShareGrant data layer ────────────────────────────────
#
# The chokepoint (test_access_chokepoint) forbids ``SharedLesson`` /
# ``LessonShareGrant`` ``.objects`` in every friends module EXCEPT this one, so
# the share pipeline's reads AND writes to those two models all funnel here.
# services.py / scrub.py / views.py call these; they never touch the managers
# directly. (``Lesson`` is still never touched here — snapshots reach the owner's
# lesson via the ``source_lesson`` FK on a SharedLesson instance.)


def get_shared_lesson(shared_lesson_id) -> SharedLesson | None:
    try:
        return SharedLesson.objects.select_related("source_lesson", "owner_tenant").get(id=shared_lesson_id)
    except (SharedLesson.DoesNotExist, ValueError, ValidationError):
        return None


def get_shared_lesson_for_lesson(lesson) -> SharedLesson | None:
    return SharedLesson.objects.filter(source_lesson=lesson).select_related("source_lesson").first()


def get_shared_lesson_by_lesson_id(lesson_id, owner_tenant) -> SharedLesson | None:
    """The owner's SharedLesson for a lesson id — owner-scoped, so a foreign
    lesson_id resolves to None (never a cross-tenant read)."""
    try:
        return (
            SharedLesson.objects.filter(source_lesson_id=lesson_id, owner_tenant_id=_tenant_id(owner_tenant))
            .select_related("source_lesson")
            .first()
        )
    except (ValueError, ValidationError):
        return None


def ensure_shared_lesson(lesson, owner_tenant) -> SharedLesson:
    """Get-or-create the frozen snapshot for a lesson (OneToOne, pending scrub)."""
    shared_lesson, _ = SharedLesson.objects.get_or_create(source_lesson=lesson, defaults={"owner_tenant": owner_tenant})
    return shared_lesson


def mark_scrub_pending(shared_lesson) -> None:
    SharedLesson.objects.filter(id=shared_lesson.id).update(
        scrub_status=SharedLesson.ScrubStatus.PENDING, scrub_error=""
    )


def save_scrub_ready(shared_lesson, **fields) -> None:
    """Persist a successful, verified scrub → status=ready."""
    for key, value in fields.items():
        setattr(shared_lesson, key, value)
    shared_lesson.scrub_status = SharedLesson.ScrubStatus.READY
    shared_lesson.scrub_error = ""
    shared_lesson.scrubbed_at = timezone.now()
    shared_lesson.save()


def save_scrub_failed(shared_lesson, error: str) -> None:
    """Fail-closed: never publishable, records why."""
    shared_lesson.scrub_status = SharedLesson.ScrubStatus.FAILED
    shared_lesson.scrub_error = (error or "")[:2000]
    shared_lesson.scrubbed_at = None
    shared_lesson.save(update_fields=["scrub_status", "scrub_error", "scrubbed_at", "updated_at"])


def create_grant(shared_lesson, friendship, granted_by=None) -> LessonShareGrant:
    """Idempotent ACTIVE grant for (shared_lesson, friendship) — the freeze+publish
    step. Re-activates a previously-revoked grant rather than duplicating."""
    grant, created = LessonShareGrant.objects.get_or_create(
        shared_lesson=shared_lesson,
        friendship=friendship,
        defaults={"granted_by": granted_by, "status": LessonShareGrant.Status.ACTIVE},
    )
    if not created and grant.status != LessonShareGrant.Status.ACTIVE:
        grant.status = LessonShareGrant.Status.ACTIVE
        grant.revoked_at = None
        grant.granted_by = granted_by
        grant.save(update_fields=["status", "revoked_at", "granted_by"])
    return grant


def get_grant(grant_id) -> LessonShareGrant | None:
    try:
        return LessonShareGrant.objects.select_related("shared_lesson", "shared_lesson__owner_tenant").get(id=grant_id)
    except (LessonShareGrant.DoesNotExist, ValueError, ValidationError):
        return None


def revoke_grant(grant: LessonShareGrant) -> None:
    """Revoke one grant → access dies instantly (read-through). Deletes the
    SharedLesson snapshot when it has no remaining active grants (zero residue)."""
    if grant.status == LessonShareGrant.Status.ACTIVE:
        grant.status = LessonShareGrant.Status.REVOKED
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["status", "revoked_at"])
    delete_shared_lesson_if_orphaned(grant.shared_lesson)


def delete_shared_lesson_if_orphaned(shared_lesson) -> None:
    if not LessonShareGrant.objects.filter(shared_lesson=shared_lesson, status=LessonShareGrant.Status.ACTIVE).exists():
        SharedLesson.objects.filter(id=shared_lesson.id).delete()


# ── Wormholes & warp (PR3) ────────────────────────────────────────────────────
#
# A wormhole is a DERIVED query — one gate per accepted neighbor with ≥1 active +
# ready grant TO the viewer — never a materialized table. The grant/SharedLesson
# reads all live here (chokepoint); the tiny WormholeVisit watermark is per-viewer
# own data, so it's read here only to compute "new since last visit".


def other_party_id(edge: Friendship, viewer_tenant) -> str:
    """The tenant id of the OTHER party on an edge, from the viewer's side."""
    viewer_id = _tenant_id(viewer_tenant)
    return edge.addressee_id if edge.requester_id == viewer_id else edge.requester_id


def wormhole_targets(viewer_tenant) -> list[dict]:
    """One entry per accepted neighbor with ≥1 active+ready grant TO the viewer.

    Returns ``[{friendship, owner_id, spark_count, new_since_last_visit}]``.
    ``spark_count`` counts the OTHER party's shares visible to the viewer (a
    grant on the accepted edge whose snapshot is owned by the neighbor and
    ``scrub_status='ready'``) — never the viewer's own shares to that neighbor.
    ``new_since_last_visit`` counts those grants created after the viewer's
    ``WormholeVisit`` watermark (all grants when there's no watermark yet).
    Neighbors with zero ready grants are omitted (no gate).
    """
    viewer_id = _tenant_id(viewer_tenant)
    edges = Friendship.objects.filter(
        Q(requester_id=viewer_id) | Q(addressee_id=viewer_id),
        status=Friendship.Status.ACCEPTED,
    )
    watermarks = {v.friendship_id: v.last_visited_at for v in WormholeVisit.objects.filter(viewer_tenant_id=viewer_id)}
    out: list[dict] = []
    for edge in edges:
        owner_id = other_party_id(edge, viewer_id)
        grants = LessonShareGrant.objects.filter(
            friendship=edge,
            status=LessonShareGrant.Status.ACTIVE,
            shared_lesson__owner_tenant_id=owner_id,
            shared_lesson__scrub_status=SharedLesson.ScrubStatus.READY,
        )
        spark_count = grants.count()
        if spark_count == 0:
            continue
        last = watermarks.get(edge.id)
        new_since = grants.filter(created_at__gt=last).count() if last else spark_count
        out.append(
            {
                "friendship": edge,
                "owner_id": owner_id,
                "spark_count": spark_count,
                "new_since_last_visit": new_since,
            }
        )
    return out


def upsert_wormhole_visit(viewer_tenant, friendship) -> WormholeVisit:
    """Advance the viewer's watermark for a friendship to now (idempotent upsert
    on the ``(viewer_tenant, friendship)`` unique constraint)."""
    visit, _created = WormholeVisit.objects.update_or_create(
        viewer_tenant_id=_tenant_id(viewer_tenant),
        friendship=friendship,
        defaults={"last_visited_at": timezone.now()},
    )
    return visit


def refresh_shared_positions_for_owner(owner_tenant) -> int:
    """Copy-forward each ready SharedLesson's SOURCE lesson coords onto the frozen
    snapshot — COORDS ONLY, no new PII crosses (design §8 geometry freshness).

    Reads the owner's own lesson coords via the ``source_lesson`` FK (never
    ``Lesson.objects`` — the chokepoint forbids the raw corpus under
    apps/friends). Writes only ``position_x`` / ``position_y`` back onto the
    snapshot; ``redacted_text`` / tags / ``star_stage`` stay frozen at their
    scrubbed values. Returns the number of snapshots whose coords moved.
    """
    owner_id = _tenant_id(owner_tenant)
    snaps = SharedLesson.objects.filter(
        owner_tenant_id=owner_id, scrub_status=SharedLesson.ScrubStatus.READY
    ).select_related("source_lesson")
    updates = []
    for snap in snaps:
        src = snap.source_lesson
        if src is None:
            continue
        if snap.position_x != src.position_x or snap.position_y != src.position_y:
            snap.position_x = src.position_x
            snap.position_y = src.position_y
            updates.append(snap)
    if updates:
        SharedLesson.objects.bulk_update(updates, ["position_x", "position_y"])
    return len(updates)


def _owner_handle(owner_id) -> str | None:
    profile = NeighborProfile.objects.filter(tenant_id=owner_id).only("handle").first()
    return profile.handle if profile else None


def adopt_shared_lesson(viewer_tenant, viewer_user, shared_lesson_id):
    """SOUVENIR — "bring a spark home" (design §8). Create a PENDING ``Lesson`` in
    the VIEWER'S OWN tenant from a neighbor's frozen, scrubbed snapshot.

    ⚠️ This is the ONE legitimate ``Lesson`` WRITE in any friends path, and it
    writes ONLY the viewer's own tenant — NEVER the owner's. It goes through the
    viewer's reverse relation ``viewer_tenant.lessons`` (tenant-scoped by
    construction) and NOT ``Lesson.objects``, so the chokepoint's "no raw Lesson
    corpus under apps/friends" rule stays intact and meaningful: that rule guards
    cross-tenant READS of raw names; this is a scoped WRITE of already-neutralized
    text into the reader's own galaxy, entering their normal pending-approve gate.

    Idempotent per ``(viewer, shared_lesson)`` via a ``source_ref`` lookup over
    the viewer's LIVE copies (pending/approved) — a repeated adopt returns the
    existing lesson, never a duplicate. A DISMISSED copy does not block:
    dismissing a souvenir means "not now", and the spark is still sitting in the
    friend's galaxy inviting adoption — re-adopt mints a fresh pending lesson
    (otherwise the "added to your pending stars" toast would lie).

    Returns ``(lesson, created)``. Raises:
      * ``ValueError('own_snapshot')`` if the viewer IS the snapshot's owner (→ 400).
      * ``PermissionDenied`` if the viewer has no active+ready grant (→ 403).
    """
    snapshot = get_shared_lesson(shared_lesson_id)
    if snapshot is None:
        raise PermissionDenied("No such shared spark")
    owner_id = snapshot.owner_tenant_id
    viewer_id = _tenant_id(viewer_tenant)
    if owner_id == viewer_id:
        raise ValueError("own_snapshot")
    # Accessor gate: the viewer must actually be able to SEE this snapshot — an
    # active grant on the accepted edge + a ready scrub. shared_star_qs is the
    # single audited visibility check; a non-neighbor / revoked / failed snapshot
    # yields an empty queryset and this denies.
    if not shared_star_qs(viewer_tenant, owner_id).filter(id=snapshot.id).exists():
        raise PermissionDenied("This spark isn't shared with you")

    source_ref = f"shared_lesson:{snapshot.id}"
    existing = (
        viewer_tenant.lessons.filter(source_ref=source_ref).exclude(status="dismissed").order_by("created_at").first()
    )
    if existing is not None:
        return existing, False

    handle = _owner_handle(owner_id)
    attribution = (
        f"Brought home from your Neighborhood — via @{handle}" if handle else "Brought home from your Neighborhood"
    )
    lesson = viewer_tenant.lessons.create(
        text=snapshot.redacted_text or "",
        context=attribution,
        tags=list(snapshot.tags or []),
        source_type="shared",
        source_ref=source_ref,
        status="pending",
        star_stage="proto",
    )
    return lesson, True


# ── Absorb read side (agent context + envelope) ──────────────────────────────
#
# The cross-tenant read that surfaces sparks shared TO the viewer, across ALL
# owners (not per-owner like shared_star_qs). Lives here because it touches
# LessonShareGrant/SharedLesson (chokepoint-confined). Returns grants (each
# carrying its ready shared_lesson + created_at) so callers can log/paginate.


def inbound_shared_grants(viewer_tenant, since=None):
    """Active grants of READY sparks shared TO ``viewer_tenant``, newest first.

    Visibility = an active ``LessonShareGrant`` on an accepted friendship the
    viewer is a party to, AND ``scrub_status='ready'``. ``since`` (a datetime)
    filters to grants created after it (the absorb cursor). Returns an EMPTY
    queryset when the viewer has no accepted edges — never raises.
    """
    from django.db.models import Q

    viewer_id = _tenant_id(viewer_tenant)
    edge_ids = list(
        Friendship.objects.filter(
            Q(requester_id=viewer_id) | Q(addressee_id=viewer_id),
            status=Friendship.Status.ACCEPTED,
        ).values_list("id", flat=True)
    )
    if not edge_ids:
        return LessonShareGrant.objects.none()
    qs = LessonShareGrant.objects.filter(
        status=LessonShareGrant.Status.ACTIVE,
        friendship_id__in=edge_ids,
        shared_lesson__scrub_status=SharedLesson.ScrubStatus.READY,
    ).select_related("shared_lesson", "shared_lesson__owner_tenant")
    if since is not None:
        qs = qs.filter(created_at__gt=since)
    return qs.order_by("-created_at")


# ── Friend chat data layer (FriendMessage confined here; §2.7/§6) ────────────


def assert_participant(viewer_tenant, thread_id) -> FriendThread:
    """Return the FriendThread the viewer is an ACTIVE member of, or raise
    DRF ``NotFound`` (404 — no-reveal, like the PR1 wave IDOR path). A swapped
    ``thread_id`` resolves a real row but fails the membership check and 404s
    without confirming the thread exists. Reads gate on MEMBERSHIP (not edge
    status), so a blocked/revoked neighbor can still read the history — only
    SENDS freeze (see services.send_friend_message)."""
    from rest_framework.exceptions import NotFound

    viewer_id = _tenant_id(viewer_tenant)
    try:
        thread = FriendThread.objects.get(id=thread_id)
    except (FriendThread.DoesNotExist, ValueError, ValidationError) as exc:
        raise NotFound("No such thread.") from exc
    is_member = FriendThreadMembership.objects.filter(thread=thread, tenant_id=viewer_id, left_at__isnull=True).exists()
    if not is_member:
        raise NotFound("No such thread.")
    return thread


def thread_messages_page(thread, after_seq: int, limit: int) -> list[FriendMessage]:
    """Ascending page of live messages with ``seq > after_seq`` (the caller has
    already run :func:`assert_participant`)."""
    return list(
        FriendMessage.objects.filter(thread=thread, deleted_at__isnull=True, seq__gt=after_seq)
        .select_related("sender_tenant")
        .order_by("seq")[:limit]
    )


def create_friend_message(thread, sender_tenant, sender_user, client_msg_id, text) -> tuple[FriendMessage, bool]:
    """Idempotent insert on ``(sender_tenant, client_msg_id)`` — an offline-outbox
    retry returns the existing row. Returns ``(message, created)``."""
    message, created = FriendMessage.objects.get_or_create(
        sender_tenant=sender_tenant,
        client_msg_id=client_msg_id,
        defaults={"thread": thread, "sender_user": sender_user, "text": text},
    )
    return message, created


def claim_message_notified(message) -> bool:
    """Atomic one-push claim: only the first delivery to reach the row returns
    True (``notified_at__isnull`` makes a re-drain a no-op)."""
    return (
        FriendMessage.objects.filter(seq=message.seq, notified_at__isnull=True).update(notified_at=timezone.now()) == 1
    )


def unread_count(thread, last_read_seq: int, viewer_tenant_id) -> int:
    return (
        FriendMessage.objects.filter(thread=thread, deleted_at__isnull=True, seq__gt=last_read_seq)
        .exclude(sender_tenant_id=viewer_tenant_id)
        .count()
    )


def latest_message(thread) -> FriendMessage | None:
    return FriendMessage.objects.filter(thread=thread, deleted_at__isnull=True).order_by("-seq").first()


def _thread_other_party_id(thread, viewer_id):
    edge = thread.friendship
    if edge is None:
        return None
    return edge.addressee_id if edge.requester_id == viewer_id else edge.requester_id


def chat_absorb_pending_counts(viewer_tenant) -> list[dict]:
    """Read-only (no cursor advance) — [{thread_id, from_handle, count}] for
    absorb-enabled threads with un-absorbed messages from the OTHER party. Feeds
    the envelope POINTER (never message text; USER.md is on the share)."""
    viewer_id = _tenant_id(viewer_tenant)
    out: list[dict] = []
    memberships = FriendThreadMembership.objects.filter(
        tenant_id=viewer_id, left_at__isnull=True, agent_absorb_enabled=True
    ).select_related("thread", "thread__friendship")
    for membership in memberships:
        count = (
            FriendMessage.objects.filter(
                thread=membership.thread, deleted_at__isnull=True, seq__gt=membership.last_absorbed_seq
            )
            .exclude(sender_tenant_id=viewer_id)
            .count()
        )
        if count:
            out.append(
                {
                    "thread_id": str(membership.thread_id),
                    "from_handle": _owner_handle(_thread_other_party_id(membership.thread, viewer_id)),
                    "count": count,
                }
            )
    return out


def absorb_pending_chat(viewer_tenant) -> list[dict]:
    """Collect un-absorbed messages per absorb-enabled thread from the OTHER
    party, ADVANCE each membership's ``last_absorbed_seq`` (idempotent cursor),
    and return raw messages for the caller to redact-fresh + log. FriendMessage
    access is confined here."""
    viewer_id = _tenant_id(viewer_tenant)
    result: list[dict] = []
    memberships = FriendThreadMembership.objects.filter(
        tenant_id=viewer_id, left_at__isnull=True, agent_absorb_enabled=True
    ).select_related("thread", "thread__friendship")
    for membership in memberships:
        messages = list(
            FriendMessage.objects.filter(
                thread=membership.thread, deleted_at__isnull=True, seq__gt=membership.last_absorbed_seq
            )
            .exclude(sender_tenant_id=viewer_id)
            .select_related("sender_tenant")
            .order_by("seq")[:20]
        )
        if not messages:
            continue
        FriendThreadMembership.objects.filter(id=membership.id).update(last_absorbed_seq=messages[-1].seq)
        result.append(
            {
                "thread_id": str(membership.thread_id),
                "from_id": _thread_other_party_id(membership.thread, viewer_id),
                "messages": messages,
            }
        )
    return result
