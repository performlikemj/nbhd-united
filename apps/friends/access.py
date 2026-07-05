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
from django.utils import timezone

from apps.tenants.models import Tenant

from .models import Friendship, LessonShareGrant, SharedLesson, compute_pair_key


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
