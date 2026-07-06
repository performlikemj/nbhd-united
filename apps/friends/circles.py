"""Circles (groups) service layer (design §2.11).

A Circle is a named set of accepted neighbors, built ON edges: you join only via
an invite code OR by being added by a member you're already neighbors with.
Membership IS the consent grant inside a Circle. Leaving/removal is honest —
your circle-scoped shares are revoked and your circle-scoped absorbed items are
purged (default) or kept (your explicit choice). None of Circle /
CircleMembership / ContentReport is chokepoint-confined (only SharedGoal is among
the four), so this module uses their managers freely; cross-tenant SharedLesson /
LessonShareGrant / FriendMessage access still routes through apps.friends.access.
"""

from __future__ import annotations

import secrets

from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from . import access
from .models import (
    AbsorbedItem,
    Circle,
    CircleMembership,
    ContentReport,
    FriendThread,
    FriendThreadMembership,
    NeighborProfile,
)

MAX_CIRCLES_PER_TENANT = 8
MAX_CIRCLE_MEMBERS = 50


def _assert_circle_has_room(circle, tenant) -> None:
    """Block a NEW active member once the circle is full. An existing active
    member (idempotent re-join / re-add) is never blocked."""
    if CircleMembership.objects.filter(circle=circle, tenant=tenant, status="active").exists():
        return
    if CircleMembership.objects.filter(circle=circle, status="active").count() >= MAX_CIRCLE_MEMBERS:
        raise ValidationError(f"This circle is full — it already has the maximum of {MAX_CIRCLE_MEMBERS} members.")


def _clamp_hue(hue) -> int:
    try:
        return max(0, min(359, int(hue)))
    except (TypeError, ValueError):
        return 210


def _assert_circle_member(tenant, circle_id):
    """Return (circle, active membership) or raise NotFound (no-reveal IDOR)."""
    try:
        circle = Circle.objects.get(id=circle_id)
    except (Circle.DoesNotExist, ValueError, ValidationError) as exc:
        raise NotFound("No such circle.") from exc
    membership = CircleMembership.objects.filter(circle=circle, tenant=tenant, status="active").first()
    if membership is None:
        raise NotFound("No such circle.")
    return circle, membership


def _assert_circle_admin(tenant, circle_id):
    circle, membership = _assert_circle_member(tenant, circle_id)
    if membership.role != "admin":
        raise PermissionDenied("Only a circle admin can do that.")
    return circle, membership


def _circle_thread(circle):
    return FriendThread.objects.filter(circle=circle, kind=FriendThread.Kind.CIRCLE).first()


def _sync_circle_thread_membership(circle, tenant, user, *, active):
    thread = _circle_thread(circle)
    if thread is None:
        return
    if active:
        membership, created = FriendThreadMembership.objects.get_or_create(
            thread=thread, tenant=tenant, defaults={"user": user}
        )
        if not created and membership.left_at is not None:
            membership.left_at = None
            membership.save(update_fields=["left_at"])
    else:
        FriendThreadMembership.objects.filter(thread=thread, tenant=tenant, left_at__isnull=True).update(
            left_at=timezone.now()
        )


def _add_active_member(circle, tenant, user, role="member"):
    membership, created = CircleMembership.objects.get_or_create(
        circle=circle, tenant=tenant, defaults={"user": user, "role": role, "status": "active"}
    )
    if not created and membership.status != "active":
        membership.status = "active"
        membership.left_at = None
        membership.save(update_fields=["status", "left_at"])
    _sync_circle_thread_membership(circle, tenant, user, active=True)
    return membership


def create_circle(tenant, user, *, name, description="", hue=210) -> Circle:
    name = (name or "").strip()
    if not name:
        raise ValidationError("A circle name is required.")
    if CircleMembership.objects.filter(tenant=tenant, status="active").count() >= MAX_CIRCLES_PER_TENANT:
        raise ValidationError(f"You're already in the maximum of {MAX_CIRCLES_PER_TENANT} circles.")
    circle = Circle.objects.create(
        name=name[:120],
        description=(description or "").strip(),
        hue=_clamp_hue(hue),
        created_by=tenant,
        invite_code=secrets.token_urlsafe(16),
    )
    CircleMembership.objects.create(circle=circle, tenant=tenant, user=user, role="admin", status="active")
    thread = FriendThread.objects.create(
        kind=FriendThread.Kind.CIRCLE, circle=circle, title=name[:160], created_by=tenant
    )
    FriendThreadMembership.objects.create(thread=thread, tenant=tenant, user=user)
    return circle


def list_circles(tenant) -> list[dict]:
    memberships = CircleMembership.objects.filter(tenant=tenant, status="active").select_related("circle")
    out: list[dict] = []
    for membership in memberships:
        circle = membership.circle
        out.append(
            {
                "circle_id": str(circle.id),
                "name": circle.name,
                "hue": circle.hue,
                "member_count": CircleMembership.objects.filter(circle=circle, status="active").count(),
                "my_role": membership.role,
                "invite_code": circle.invite_code if membership.role == "admin" else None,
            }
        )
    return out


def get_circle_detail(tenant, circle_id) -> dict:
    circle, membership = _assert_circle_member(tenant, circle_id)
    members = []
    for other in CircleMembership.objects.filter(circle=circle, status="active"):
        profile = NeighborProfile.objects.filter(tenant_id=other.tenant_id).first()
        members.append(
            {
                "handle": profile.handle if profile else None,
                "display_name": profile.display_name if profile else "Neighbor",
                "avatar_hue": profile.avatar_hue if profile else 210,
                "role": other.role,
                "is_me": other.tenant_id == tenant.id,
            }
        )
    thread = _circle_thread(circle)
    return {
        "circle_id": str(circle.id),
        "name": circle.name,
        "description": circle.description,
        "hue": circle.hue,
        "members": members,
        "my_role": membership.role,
        "thread_id": str(thread.id) if thread else None,
        "invite_code": circle.invite_code if membership.role == "admin" else None,
    }


def join_circle(tenant, user, invite_code) -> dict:
    circle = Circle.objects.filter(invite_code=(invite_code or "").strip()).first()
    if circle is None:
        raise NotFound("Invite code not found.")
    # Built ON edges: a joiner must already be an accepted neighbor of the circle
    # creator (the inviter) — you can't reach into a circle of strangers.
    if not access.are_neighbors(tenant, circle.created_by_id):
        raise PermissionDenied("You can only join a circle through a neighbor you're connected with.")
    _assert_circle_has_room(circle, tenant)
    _add_active_member(circle, tenant, user, role="member")
    return {"circle_id": str(circle.id), "status": "active"}


def add_circle_member(tenant, user, circle_id, handle) -> dict:
    """A member waves a neighbor into the circle — the target must be an accepted
    neighbor of the adder (built on edges)."""
    circle, _membership = _assert_circle_member(tenant, circle_id)
    profile = (
        NeighborProfile.objects.select_related("tenant", "tenant__user")
        .filter(handle=(handle or "").strip().lower())
        .first()
    )
    if profile is None:
        raise NotFound("No neighbor with that handle.")
    if not access.are_neighbors(tenant, profile.tenant_id):
        raise PermissionDenied("You can only add a neighbor you're connected with.")
    _assert_circle_has_room(circle, profile.tenant)
    _add_active_member(circle, profile.tenant, profile.tenant.user, role="member")
    return {"circle_id": str(circle.id), "added": profile.handle}


def _depart(circle, tenant, membership, *, purge, new_status):
    membership.status = new_status
    membership.left_at = timezone.now()
    membership.save(update_fields=["status", "left_at"])
    _sync_circle_thread_membership(circle, tenant, tenant.user, active=False)
    # My circle-scoped shares leave the circle instantly (zero residue).
    access.revoke_owner_circle_grants(tenant, circle)
    if purge:
        # Default: purge what my agent absorbed FROM this circle. Keep = my choice.
        AbsorbedItem.objects.filter(tenant=tenant, circle=circle, purged_at__isnull=True).update(
            purged_at=timezone.now()
        )


def leave_circle(tenant, circle_id, *, purge=True) -> dict:
    circle, membership = _assert_circle_member(tenant, circle_id)
    _depart(circle, tenant, membership, purge=purge, new_status="left")
    return {"circle_id": str(circle.id), "status": "left", "purged": bool(purge)}


def remove_circle_member(tenant, circle_id, handle) -> dict:
    """Admin removes a member by @handle. Same as leave with a default purge of
    the removed member's circle-scoped absorbed items."""
    circle, _membership = _assert_circle_admin(tenant, circle_id)
    profile = NeighborProfile.objects.filter(handle=(handle or "").strip().lower()).first()
    if profile is None:
        raise NotFound("No such member.")
    if profile.tenant_id == tenant.id:
        raise ValidationError("Use leave to remove yourself.")
    target = (
        CircleMembership.objects.select_related("tenant", "tenant__user")
        .filter(circle=circle, tenant_id=profile.tenant_id, status="active")
        .first()
    )
    if target is None:
        raise NotFound("No such member.")
    _depart(circle, target.tenant, target, purge=True, new_status="removed")  # removal defaults to purge
    return {"circle_id": str(circle.id), "removed": profile.handle}


def regenerate_invite_code(tenant, circle_id) -> dict:
    circle, _membership = _assert_circle_admin(tenant, circle_id)
    circle.invite_code = secrets.token_urlsafe(16)
    circle.save(update_fields=["invite_code"])
    return {"circle_id": str(circle.id), "invite_code": circle.invite_code}


def report_content(tenant, user, *, target_kind, target_id="", reason="", detail="") -> dict:
    """Reporter-side moderation + support intake. A content report (shared_lesson
    / friend_message) hides the item for the reporter and records it (design §10).
    A ``general`` report has no content id — it's the Settings → Support "Report a
    concern" destination (App Review #3); nothing is hidden, it lands as an open
    support row."""
    if target_kind not in ("shared_lesson", "friend_message", "general"):
        raise ValidationError("target_kind must be shared_lesson, friend_message, or general.")
    is_content = target_kind in ("shared_lesson", "friend_message")
    text = (reason or "").strip()
    if detail:
        text = (text + " — " + str(detail).strip()).strip(" —")
    report = ContentReport(
        reporter_tenant=tenant,
        reporter_user=user,
        target_kind=target_kind,
        reason=text[:280],
        status="hidden" if is_content else "open",
    )
    if target_kind == "shared_lesson":
        shared_lesson = access.get_shared_lesson(target_id)
        if shared_lesson is None:
            raise NotFound("No such spark.")
        report.shared_lesson = shared_lesson
    elif target_kind == "friend_message":
        message = access.get_friend_message_by_public_id(target_id)
        if message is None:
            raise NotFound("No such message.")
        report.friend_message = message
    report.save()
    return {"report_id": str(report.id), "hidden": is_content}
