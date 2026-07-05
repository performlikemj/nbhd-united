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

from django.core.exceptions import PermissionDenied

from apps.tenants.models import Tenant

from .models import Friendship, compute_pair_key


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

    Lands in PR2 with ``SharedLesson`` + ``LessonShareGrant``.
    """
    raise NotImplementedError("shared_star_qs lands in PR2 with SharedLesson")


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
