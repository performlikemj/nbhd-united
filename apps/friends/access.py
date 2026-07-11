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
superuser** today, so the PR8 ``FORCE ROW LEVEL SECURITY`` policies on
``shared_lessons`` / ``lesson_share_grants`` / ``friend_messages`` are INERT
belt-and-suspenders — they start enforcing only if the app connects as a
non-BYPASSRLS role (run ``manage.py check_friends_rls`` for the live verdict).
Until then cross-tenant isolation is 100% the Python filters in this module. A
single missing edge/tenant filter leaks another user's private data with no DB
net. Containing that risk to one audited module is the entire point.

Addressing is always by opaque ``friendship_id`` / ``thread_id`` /
``circle_id``, never a client-supplied ``tenant_id`` — those ids exist only
if the relationship exists, and the accessor *still* re-verifies the caller is
a party (IDOR defeated by construction, design §4.5).
"""

from __future__ import annotations

import contextlib

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from apps.tenants.models import Tenant

from .models import (
    CircleMembership,
    ContentReport,
    FriendMessage,
    Friendship,
    FriendThread,
    FriendThreadMembership,
    LessonShareGrant,
    NeighborProfile,
    SharedGoal,
    SharedLesson,
    SkyMembership,
    WormholeVisit,
    compute_pair_key,
)


def _tenant_id(value):
    """Accept either a ``Tenant`` instance or a raw id and return the id."""
    return getattr(value, "id", value)


@contextlib.contextmanager
def backstop_service_context():
    """Mark this connection ``app.service_role`` for the duration, for trusted
    server-side background work that reads the friends cross-tenant tables
    OUTSIDE a tenant request (the scrub / position-refresh QStash tasks, the
    envelope USER.md push thread, the friend-chat push thread). Under the PR8
    FORCE-RLS policies those tables fail closed on an unset GUC, so without this
    a background read would see zero rows and the feature would break.

    A no-op when ``FRIENDS_DB_BACKSTOP`` is off, and harmless (inert) while the
    app role bypasses RLS. On exit it clears ONLY ``app.service_role`` — never
    ``app.tenant_id`` / ``app.user_id`` — so a middleware-set tenant GUC on an
    in-request caller survives untouched."""
    if not getattr(settings, "FRIENDS_DB_BACKSTOP", True):
        yield
        return
    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)
    try:
        yield
    finally:
        if connection.connection is not None:
            with connection.cursor() as cur:
                cur.execute("SELECT set_config('app.service_role', '', false)")


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


def blocked_counterpart_ids(viewer_tenant) -> set:
    """Tenant ids the viewer has a ``blocked`` edge with, EITHER direction (they
    blocked the viewer, or the viewer blocked them). Inside a shared Circle these
    counterparts are quietly hidden both ways (PR10) — no ejection, no reveal.
    Empty set when there are no blocks."""
    vid = _tenant_id(viewer_tenant)
    out: set = set()
    for requester_id, addressee_id in Friendship.objects.filter(
        Q(requester_id=vid) | Q(addressee_id=vid), status=Friendship.Status.BLOCKED
    ).values_list("requester_id", "addressee_id"):
        out.add(addressee_id if requester_id == vid else requester_id)
    return out


def _is_blocked_pair(a_id, b_id) -> bool:
    """True iff a ``blocked`` edge exists between the two tenants (either who)."""
    edge = Friendship.objects.filter(pair_key=compute_pair_key(a_id, b_id)).first()
    return edge is not None and edge.status == Friendship.Status.BLOCKED


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

    PR7: friendship-grant OR circle-grant (a circle both are ACTIVE members of).
    Reporter-side hidden items (a ContentReport by the viewer) are excluded.
    """
    viewer_id, owner_id = _tenant_id(viewer_tenant), _tenant_id(owner_tenant)
    if viewer_id == owner_id:
        return SharedLesson.objects.none()
    # PR10: a blocked pair sees nothing of each other's, even via a shared Circle
    # (the friendship arm already requires ACCEPTED; this closes the circle arm).
    if _is_blocked_pair(viewer_id, owner_id):
        return SharedLesson.objects.none()

    audience = Q()
    edge = Friendship.objects.filter(
        pair_key=compute_pair_key(viewer_id, owner_id),
        status=Friendship.Status.ACCEPTED,
    ).first()
    if edge is not None:
        audience |= Q(grants__friendship=edge, grants__status=LessonShareGrant.Status.ACTIVE)
    shared_circle_ids = _shared_active_circle_ids(viewer_id, owner_id)
    if shared_circle_ids:
        audience |= Q(grants__circle_id__in=shared_circle_ids, grants__status=LessonShareGrant.Status.ACTIVE)
    if not audience:  # no accepted edge and no shared circle → no visibility
        return SharedLesson.objects.none()

    return (
        SharedLesson.objects.filter(Q(owner_tenant_id=owner_id, scrub_status=SharedLesson.ScrubStatus.READY) & audience)
        .exclude(id__in=_reported_shared_lesson_ids(viewer_id))
        .distinct()
    )


def _shared_active_circle_ids(viewer_id, owner_id) -> list:
    """Circles BOTH tenants are ACTIVE members of (the §2.5 circle visibility rule)."""
    viewer_circles = set(
        CircleMembership.objects.filter(tenant_id=viewer_id, status="active").values_list("circle_id", flat=True)
    )
    if not viewer_circles:
        return []
    return list(
        CircleMembership.objects.filter(tenant_id=owner_id, status="active", circle_id__in=viewer_circles).values_list(
            "circle_id", flat=True
        )
    )


def _reported_shared_lesson_ids(viewer_id) -> list:
    """SharedLesson ids the viewer has reported (reporter-side hide, design §10)."""
    return list(
        ContentReport.objects.filter(
            reporter_tenant_id=viewer_id, status="hidden", shared_lesson__isnull=False
        ).values_list("shared_lesson_id", flat=True)
    )


def spark_counts_by_owner(viewer_tenant) -> dict:
    """``{owner_tenant_id: n}`` — how many READY snapshots each of the viewer's
    neighbors has shared visible to them (friendship OR shared-circle grant), in
    ONE grouped query (no per-neighbor N+1 for the home BFF). A badge count; it
    doesn't subtract the viewer's own reports (negligible for a badge)."""
    viewer_id = _tenant_id(viewer_tenant)
    edge_ids = list(
        Friendship.objects.filter(
            Q(requester_id=viewer_id) | Q(addressee_id=viewer_id), status=Friendship.Status.ACCEPTED
        ).values_list("id", flat=True)
    )
    circle_ids = my_active_circle_ids(viewer_id)
    audience = Q()
    if edge_ids:
        audience |= Q(grants__friendship_id__in=edge_ids)
    if circle_ids:
        audience |= Q(grants__circle_id__in=circle_ids)
    if not audience:
        return {}
    rows = (
        SharedLesson.objects.filter(
            Q(scrub_status=SharedLesson.ScrubStatus.READY, grants__status=LessonShareGrant.Status.ACTIVE) & audience
        )
        .values("owner_tenant_id")
        .annotate(n=Count("id", distinct=True))
    )
    return {row["owner_tenant_id"]: row["n"] for row in rows}


def direct_thread_state(viewer_tenant) -> dict:
    """``{other_party_tenant_id: {"thread_id": str, "has_unread": bool}}`` for the
    viewer's 1:1 threads. One query — unread is an ``Exists`` subquery (a message
    newer than the viewer's read cursor, not sent by the viewer)."""
    viewer_id = _tenant_id(viewer_tenant)
    unread_subq = (
        FriendMessage.objects.filter(
            thread=OuterRef("thread"), deleted_at__isnull=True, seq__gt=OuterRef("last_read_seq")
        )
        .exclude(sender_tenant_id=viewer_id)
        .values("pk")
    )
    memberships = (
        FriendThreadMembership.objects.filter(
            tenant_id=viewer_id, left_at__isnull=True, thread__kind=FriendThread.Kind.DIRECT
        )
        .select_related("thread", "thread__friendship")
        .annotate(has_unread=Exists(unread_subq))
    )
    out: dict = {}
    for membership in memberships:
        other = _thread_other_party_id(membership.thread, viewer_id)
        if other is not None:
            out[other] = {"thread_id": str(membership.thread_id), "has_unread": bool(membership.has_unread)}
    return out


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
    lesson_id resolves to None (never a cross-tenant read).

    Read under the service context. This is the OWNER reading their OWN frozen
    snapshot, and the explicit ``owner_tenant_id`` filter below is the real
    security boundary. Leaving this read to depend on the per-connection
    ``app.tenant_id`` RLS GUC made the owner's own row fail-close to ``None``
    whenever that GUC was momentarily unset on a pooled / reconnected connection
    (e.g. one the scrub task had just borrowed under its own
    ``backstop_service_context``) — surfacing as a spurious 404 on
    ``shares/preview`` mid-scrub while the row was present the whole time. The
    service role removes that fragility without widening exposure: the owner
    filter still scopes the result to the caller's own snapshot."""
    try:
        with backstop_service_context():
            return (
                SharedLesson.objects.filter(source_lesson_id=lesson_id, owner_tenant_id=_tenant_id(owner_tenant))
                .select_related("source_lesson")
                .first()
            )
    except (ValueError, ValidationError):
        return None


def ensure_shared_lesson(lesson, owner_tenant) -> tuple[SharedLesson, bool]:
    """Get-or-create the frozen snapshot for a lesson (OneToOne, pending scrub).
    Returns ``(shared_lesson, created)``.

    Race-safe: a rapid double-share of the same lesson (an iOS double-submit)
    has exactly one request win the ``source_lesson`` OneToOne insert; every
    loser catches the unique violation and returns the winner's row with
    ``created=False`` — never a 500 (prod 2026-07-11, the user's first real
    spark). The loser re-fetches under the service context so an RLS GUC flicker
    on a pooled / reconnected connection can't fail-close the read to a spurious
    miss and re-raise the violation (mirrors :func:`get_shared_lesson_by_lesson_id`;
    this is why the stdlib ``get_or_create`` — whose re-get rides the request
    connection — 500'd instead of self-healing)."""
    from django.db import IntegrityError, transaction

    try:
        return SharedLesson.objects.get(source_lesson=lesson), False
    except SharedLesson.DoesNotExist:
        pass
    try:
        with transaction.atomic():
            return SharedLesson.objects.create(source_lesson=lesson, owner_tenant=owner_tenant), True
    except IntegrityError:
        winner = get_shared_lesson_by_lesson_id(lesson.id, owner_tenant)
        if winner is None:
            raise
        return winner, False


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


def create_grant(shared_lesson, friendship=None, circle=None, granted_by=None) -> LessonShareGrant:
    """Idempotent ACTIVE grant — the freeze+publish step. Exactly one audience:
    a friendship (1:1) XOR a circle. Re-activates a previously-revoked grant
    rather than duplicating."""
    lookup = {"shared_lesson": shared_lesson}
    if circle is not None:
        lookup["circle"] = circle
    else:
        lookup["friendship"] = friendship
    grant, created = LessonShareGrant.objects.get_or_create(
        **lookup, defaults={"granted_by": granted_by, "status": LessonShareGrant.Status.ACTIVE}
    )
    if not created and grant.status != LessonShareGrant.Status.ACTIVE:
        grant.status = LessonShareGrant.Status.ACTIVE
        grant.revoked_at = None
        grant.granted_by = granted_by
        grant.save(update_fields=["status", "revoked_at", "granted_by"])
    return grant


def my_active_circle_ids(tenant) -> list:
    return list(
        CircleMembership.objects.filter(tenant_id=_tenant_id(tenant), status="active").values_list(
            "circle_id", flat=True
        )
    )


def revoke_owner_circle_grants(owner_tenant, circle) -> None:
    """On leave/remove: revoke the departing member's OWN grants scoped to this
    circle (their shared snapshots leave the circle instantly), deleting any
    snapshot left with no remaining active grants."""
    owner_id = _tenant_id(owner_tenant)
    grants = list(
        LessonShareGrant.objects.filter(
            circle=circle, status=LessonShareGrant.Status.ACTIVE, shared_lesson__owner_tenant_id=owner_id
        ).select_related("shared_lesson")
    )
    for grant in grants:
        grant.status = LessonShareGrant.Status.REVOKED
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["status", "revoked_at"])
    for grant in grants:
        delete_shared_lesson_if_orphaned(grant.shared_lesson)


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
    viewer is a party to, OR on a circle the viewer is an active member of, AND
    ``scrub_status='ready'``. ``since`` (a datetime) filters to grants created
    after it (the absorb cursor). Empty when the viewer has no edges/circles —
    never raises.
    """
    viewer_id = _tenant_id(viewer_tenant)
    edge_ids = list(
        Friendship.objects.filter(
            Q(requester_id=viewer_id) | Q(addressee_id=viewer_id),
            status=Friendship.Status.ACCEPTED,
        ).values_list("id", flat=True)
    )
    circle_ids = my_active_circle_ids(viewer_id)
    audience = Q()
    if edge_ids:
        audience |= Q(friendship_id__in=edge_ids)
    if circle_ids:
        audience |= Q(circle_id__in=circle_ids)
    if not audience:
        return LessonShareGrant.objects.none()
    qs = (
        LessonShareGrant.objects.filter(
            status=LessonShareGrant.Status.ACTIVE,
            shared_lesson__scrub_status=SharedLesson.ScrubStatus.READY,
        )
        .filter(audience)
        .select_related("shared_lesson", "shared_lesson__owner_tenant", "circle")
    )
    blocked = blocked_counterpart_ids(viewer_id)  # PR10: don't absorb a blocked counterpart's sparks
    if blocked:
        qs = qs.exclude(shared_lesson__owner_tenant_id__in=blocked)
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


def thread_messages_page(thread, after_seq: int, limit: int, viewer_tenant_id=None) -> list[FriendMessage]:
    """Ascending page of live messages with ``seq > after_seq`` (the caller has
    already run :func:`assert_participant`). Messages the viewer has reported are
    hidden (reporter-side moderation)."""
    qs = FriendMessage.objects.filter(thread=thread, deleted_at__isnull=True, seq__gt=after_seq)
    if viewer_tenant_id is not None:
        hidden = ContentReport.objects.filter(
            reporter_tenant_id=viewer_tenant_id, status="hidden", friend_message__isnull=False
        ).values_list("friend_message_id", flat=True)
        qs = qs.exclude(seq__in=hidden)
        # PR10: quietly hide a blocked counterpart's messages (history included,
        # both directions). The composer stays open — others still see you.
        blocked = blocked_counterpart_ids(viewer_tenant_id)
        if blocked:
            qs = qs.exclude(sender_tenant_id__in=blocked)
    return list(qs.select_related("sender_tenant").order_by("seq")[:limit])


def get_friend_message_by_public_id(public_id) -> FriendMessage | None:
    try:
        return FriendMessage.objects.get(public_id=public_id)
    except (FriendMessage.DoesNotExist, ValueError, ValidationError):
        return None


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
    blocked = blocked_counterpart_ids(viewer_id) | {viewer_id}  # PR10: skip blocked counterparts + self
    out: list[dict] = []
    memberships = FriendThreadMembership.objects.filter(
        tenant_id=viewer_id, left_at__isnull=True, agent_absorb_enabled=True
    ).select_related("thread", "thread__friendship")
    for membership in memberships:
        count = (
            FriendMessage.objects.filter(
                thread=membership.thread, deleted_at__isnull=True, seq__gt=membership.last_absorbed_seq
            )
            .exclude(sender_tenant_id__in=blocked)
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
    blocked = blocked_counterpart_ids(viewer_id) | {viewer_id}  # PR10: never absorb a blocked counterpart
    result: list[dict] = []
    memberships = FriendThreadMembership.objects.filter(
        tenant_id=viewer_id, left_at__isnull=True, agent_absorb_enabled=True
    ).select_related("thread", "thread__friendship")
    for membership in memberships:
        messages = list(
            FriendMessage.objects.filter(
                thread=membership.thread, deleted_at__isnull=True, seq__gt=membership.last_absorbed_seq
            )
            .exclude(sender_tenant_id__in=blocked)
            .select_related("sender_tenant")
            .order_by("seq")[:20]
        )
        if not messages:
            continue
        FriendThreadMembership.objects.filter(id=membership.id).update(last_absorbed_seq=messages[-1].seq)
        result.append(
            {
                "thread_id": str(membership.thread_id),
                "from_id": _thread_other_party_id(membership.thread, viewer_id),  # None for circle threads
                "circle_id": membership.thread.circle_id,  # tag circle-sourced items for scoped purge
                "messages": messages,
            }
        )
    return result


# ── Missions (SharedGoal) data layer — SharedGoal.objects confined here ──────
#
# Only ``SharedGoal`` is chokepoint-confined (SharedGoalMembership /
# SharedGoalUpdate / PendingGoalAction are ordinary friends models used freely
# by services + projection). So the mission's create / read / list / locked-edit
# funnel here; membership + the append-only update stream do not.


def create_mission(creator_tenant, friendship, *, title, description="", pillar="", target=None, target_date=None):
    return SharedGoal.objects.create(
        title=title,
        description=description,
        pillar=pillar,
        friendship=friendship,
        created_by=creator_tenant,
        target=target or {},
        target_date=target_date,
        status=SharedGoal.Status.ACTIVE,
    )


def get_mission(mission_id):
    try:
        return SharedGoal.objects.select_related("friendship").get(id=mission_id)
    except (SharedGoal.DoesNotExist, ValueError, ValidationError):
        return None


def missions_for(tenant):
    """Active missions the tenant is an active member of, newest first."""
    return (
        SharedGoal.objects.filter(memberships__tenant=tenant, memberships__status="active")
        .distinct()
        .order_by("-created_at")
    )


def update_mission(mission, *, expected_version, editor_owner, fields):
    """Optimistic multi-writer edit (Fuel's version/edit-lock pattern). Returns
    ``(mission, result)`` where result is "ok" | "version_conflict" | "locked"."""
    fresh = SharedGoal.objects.get(id=mission.id)
    if expected_version is not None and fresh.version != expected_version:
        return fresh, "version_conflict"
    if (
        fresh.edit_lock_until
        and fresh.edit_lock_until > timezone.now()
        and fresh.edit_lock_owner
        and fresh.edit_lock_owner != editor_owner
    ):
        return fresh, "locked"
    for key, value in fields.items():
        setattr(fresh, key, value)
    fresh.version = fresh.version + 1
    fresh.save()
    return fresh, "ok"


def set_mission_status(mission, status, **extra):
    SharedGoal.objects.filter(id=mission.id).update(status=status, **extra)


# ── "My sky" — the chosen inner circle (Bounded Neighborhood; BN-PR1) ─────────
#
# SkyMembership is a PRIVATE, ONE-WAY, per-(viewer, friendship) curation — the
# polar opposite of a Circle (no consent, no visibility to the other party, no
# group chat). It mirrors WormholeVisit: per-viewer OWN data, lifecycle tied to
# the edge. All reads/writes are self-scoped and confined to THIS module (the AST
# chokepoint guards SkyMembership.objects here), so "whom you keep close" can
# never leak to the person you chose — no moment, no push, no counter they can
# see. Hard-capped at MAX_SKY, enforced server-side at the add action.

MAX_SKY = 12  # hard inner-circle cap (Bounded Neighborhood brief §4.4, accepted 2026-07-07)


def sky_count(viewer_tenant) -> int:
    """How many neighbors the viewer keeps in their sky (drives the hard cap)."""
    return SkyMembership.objects.filter(viewer_tenant_id=_tenant_id(viewer_tenant)).count()


def sky_friendship_ids(viewer_tenant) -> set:
    """The set of friendship ids in the viewer's sky — ONE query, for the additive
    ``in_my_sky`` flag on the home BFF + wormhole payloads. Self-scoped: a viewer
    only ever sees their own picks."""
    return set(
        SkyMembership.objects.filter(viewer_tenant_id=_tenant_id(viewer_tenant)).values_list("friendship_id", flat=True)
    )


def add_to_sky(viewer_tenant, friendship) -> tuple[bool, bool]:
    """Add an accepted-neighbor edge to the viewer's PRIVATE sky. Returns
    ``(created, full)``:

      * ``(True, False)``  — newly added.
      * ``(False, False)`` — already in the sky (idempotent no-op).
      * ``(False, True)``  — rejected: the sky is at the hard ``MAX_SKY`` cap and
        this would be a genuinely NEW add. Nothing is created; the caller renders
        the forced-removal swap. (An already-in-sky edge is never cap-blocked.)

    The caller has already party-checked the accepted edge via
    :func:`assert_neighbors`. One-way + invisible: no signal of any kind reaches
    the other party.
    """
    from django.db import IntegrityError, transaction

    viewer_id = _tenant_id(viewer_tenant)
    if SkyMembership.objects.filter(viewer_tenant_id=viewer_id, friendship=friendship).exists():
        return False, False  # idempotent — already chosen
    # Hard cap — mirrors the circle-member cap (circles.py), but only a genuinely
    # new add at capacity is blocked. A tiny TOCTOU on concurrent adds of
    # *different* edges is benign (a private list momentarily at 13, never a
    # security boundary); the forced-removal UX is the real cap mechanism.
    if SkyMembership.objects.filter(viewer_tenant_id=viewer_id).count() >= MAX_SKY:
        return False, True
    try:
        with transaction.atomic():
            SkyMembership.objects.create(viewer_tenant_id=viewer_id, friendship=friendship)
    except IntegrityError:
        return False, False  # concurrent same-edge add won the unique race — idempotent
    return True, False


def remove_from_sky(viewer_tenant, friendship) -> bool:
    """Remove an edge from the viewer's sky. Idempotent — returns True iff a row
    was deleted. Self-scoped by ``viewer_tenant`` so it can only ever delete the
    caller's OWN pick (never another viewer's). It is NOT an unfriend: the edge
    and everything shared over it are untouched — only the flight gate goes away.
    Works regardless of edge status (so a stale pick can always be tidied).
    ``friendship`` may be a ``Friendship`` instance or a raw friendship id."""
    deleted, _ = SkyMembership.objects.filter(
        viewer_tenant_id=_tenant_id(viewer_tenant), friendship_id=getattr(friendship, "id", friendship)
    ).delete()
    return deleted > 0


def sky_roster(viewer_tenant) -> list[dict]:
    """The viewer's full sky roster (≤ ``MAX_SKY``), newest-added first, INCLUDING
    the quiet in-sky-no-spark slots the spark-gated wormhole payload omits.
    Returns ``[{friendship_id, owner_id, added_at}]``; identity/spark hydration is
    the caller's job (like ``wormhole_targets`` → ``list_wormholes``). Skips any
    row whose edge is no longer an accepted edge the viewer is a party to (a
    revoked/blocked edge can outlive its sky row until the human tidies it,
    exactly as a ``WormholeVisit`` watermark does)."""
    viewer_id = _tenant_id(viewer_tenant)
    rows = SkyMembership.objects.filter(viewer_tenant_id=viewer_id).select_related("friendship").order_by("-added_at")
    out: list[dict] = []
    for row in rows:
        edge = row.friendship
        if edge.status != Friendship.Status.ACCEPTED:
            continue
        if viewer_id not in (edge.requester_id, edge.addressee_id):
            continue
        out.append(
            {
                "friendship_id": str(edge.id),
                "owner_id": other_party_id(edge, viewer_id),
                "added_at": row.added_at,
            }
        )
    return out
