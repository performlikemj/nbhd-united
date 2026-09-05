"""Consumer-facing Neighborhood API (JWT auth, frontend console).

Every view is gated on ``tenant.friends_enabled`` (403 when off) and scoped to
``request.user.tenant``. Addressing is by opaque ``friendship_id`` / invite
``token`` — never a client-supplied ``tenant_id`` (IDOR dead by construction,
design §4.5); the service layer re-verifies party membership on every mutation.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import circles, services
from .serializers import InviteCreateSerializer, NeighborProfileSerializer, WaveCreateSerializer
from .throttling import AdoptDayThrottle, MessageSendHourThrottle, WaveSendDayThrottle


class FriendsView(APIView):
    """Base: authenticated + Neighborhood-enabled, resolves the caller's tenant."""

    permission_classes = [IsAuthenticated]

    def get_tenant(self, request):
        tenant = getattr(request.user, "tenant", None)
        if tenant is None:
            raise NotFound("No tenant for this account.")
        if not tenant.friends_enabled:
            raise PermissionDenied("The Neighborhood is not enabled for this account.")
        return tenant


class NeighborhoodView(FriendsView):
    """GET /api/v1/friends/ — profile + accepted neighbors + pending in/out."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(services.list_neighborhood(tenant))


class WaveCreateView(FriendsView):
    """POST /api/v1/friends/waves/ — send a wave by @handle. Rate-limited."""

    throttle_classes = [WaveSendDayThrottle]

    def post(self, request):
        tenant = self.get_tenant(request)
        serializer = WaveCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        edge, created = services.send_wave(
            tenant, request.user, serializer.validated_data["handle"], serializer.validated_data.get("note", "")
        )
        return Response(_wave_result(edge, tenant), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class WaveRespondView(FriendsView):
    """POST /api/v1/friends/waves/<friendship_id>/{accept|decline|block} — the
    action comes from the URL conf (kwargs), never client-controlled."""

    def post(self, request, friendship_id, action):
        tenant = self.get_tenant(request)
        edge = services.respond_to_wave(tenant, friendship_id, action)
        return Response({"friendship_id": str(edge.id), "status": edge.status})


class UnfriendView(FriendsView):
    """DELETE /api/v1/friends/<friendship_id>/ — revoke the edge."""

    def delete(self, request, friendship_id):
        tenant = self.get_tenant(request)
        edge = services.unfriend(tenant, friendship_id)
        return Response({"friendship_id": str(edge.id), "status": edge.status})


class UnblockView(FriendsView):
    """POST /api/v1/friends/<friendship_id>/unblock/ — the blocker lifts a block.
    Flips the edge to ``revoked`` (re-wave to resume); only the blocker can, and
    a non-blocker / non-party gets 404 (the block was never disclosed to them)."""

    def post(self, request, friendship_id):
        tenant = self.get_tenant(request)
        edge = services.unblock(tenant, friendship_id)
        return Response({"friendship_id": str(edge.id), "status": edge.status})


class ProfileView(FriendsView):
    """GET / PATCH /api/v1/friends/profile/ — the caller's own @handle/bio/hue.
    GET auto-creates a profile with a derived unique handle on first access."""

    def get(self, request):
        tenant = self.get_tenant(request)
        profile = services.ensure_neighbor_profile(tenant, request.user)
        return Response(NeighborProfileSerializer(profile).data)

    def patch(self, request):
        tenant = self.get_tenant(request)
        profile = services.ensure_neighbor_profile(tenant, request.user)
        serializer = NeighborProfileSerializer(profile, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class InviteCreateView(FriendsView):
    """POST /api/v1/friends/invites/ — mint a wave link/QR token."""

    def post(self, request):
        tenant = self.get_tenant(request)
        serializer = InviteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invite = services.create_invite(
            tenant,
            max_uses=serializer.validated_data["max_uses"],
            expires_in_days=serializer.validated_data["expires_in_days"],
        )
        return Response(_invite_result(invite), status=status.HTTP_201_CREATED)


class InviteDetailView(APIView):
    """GET /api/v1/friends/invites/<token>/ — PUBLIC inviter preview for the
    signup/accept page (AllowAny; inviter identity only, nothing private).

    Non-subscriber signup → auto-accept is the documented PR1.5 seam: the signup
    flow (``ensure_tenant_provisioned``, apps/tenants/services.py) would carry
    this token and call ``services.claim_invite`` once the tenant is
    provisioned. PR1 wires only the existing-subscriber claim below."""

    permission_classes = [AllowAny]

    def get(self, request, token):
        return Response(services.invite_metadata(token))


class InviteClaimView(FriendsView):
    """POST /api/v1/friends/invites/<token>/claim/ — an existing subscriber
    claims → the edge resolves to ``accepted`` immediately (design §2.3)."""

    def post(self, request, token):
        tenant = self.get_tenant(request)
        edge = services.claim_invite(tenant, request.user, token)
        return Response(_wave_result(edge, tenant))


# ── response shaping ─────────────────────────────────────────────────────────


def _wave_result(edge, viewer_tenant) -> dict:
    """The other party's public profile + edge status, from the viewer's side."""
    other = edge.addressee if edge.requester_id == viewer_tenant.id else edge.requester
    from .models import NeighborProfile

    profile = NeighborProfile.objects.filter(tenant=other).first()
    return {
        "friendship_id": str(edge.id),
        "status": edge.status,
        "display_name": profile.display_name if profile else (getattr(other.user, "display_name", None) or "Neighbor"),
        "handle": profile.handle if profile else None,
        "avatar_hue": profile.avatar_hue if profile else 210,
    }


def _invite_result(invite) -> dict:
    base = getattr(settings, "FRONTEND_URL", "").rstrip("/")
    return {
        "token": invite.token,
        "url": f"{base}/friends/invite/{invite.token}",
        "expires_at": invite.expires_at,
        "max_uses": invite.max_uses,
        "uses": invite.uses,
    }


# ── Share pipeline (approval queue + preview-before-share) ────────────────────


class SharePreviewView(FriendsView):
    """GET /api/v1/friends/shares/preview/?lesson_id=&friendship_id= — the
    literal, already-scrubbed bytes the neighbor will see. 202 while scrubbing,
    409 if the scrub failed, 200 with ``redacted_text`` + residuals banner."""

    def get(self, request):
        tenant = self.get_tenant(request)
        lesson_id = request.query_params.get("lesson_id")
        friendship_id = request.query_params.get("friendship_id")
        circle_id = request.query_params.get("circle_id")
        if not lesson_id or not (friendship_id or circle_id):
            raise ValidationError("lesson_id and one of friendship_id / circle_id are required.")
        payload, code = services.preview_share(tenant, lesson_id, friendship_id, circle_id)
        response = Response(payload, status=code)
        if code == status.HTTP_202_ACCEPTED:
            # The scrub is async; tell the SharePreviewSheet when to re-poll
            # (design ask #4 — the "getting ready safely" trust beat).
            response["Retry-After"] = "2"
        return response


class PendingSharesView(FriendsView):
    """GET /api/v1/friends/shares/pending/ — the human's approval queue."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(services.list_pending_shares(tenant))


class ShareApproveView(FriendsView):
    """POST /api/v1/friends/shares/<id>/approve {final_text?} — the ONLY path
    that creates a grant. Edit → 202 (re-scrub, preview again); ready → 200."""

    def post(self, request, pending_share_id):
        tenant = self.get_tenant(request)
        payload, code = services.approve_share(tenant, pending_share_id, final_text=request.data.get("final_text"))
        return Response(payload, status=code)


class ShareRejectView(FriendsView):
    """POST /api/v1/friends/shares/<id>/reject — no grant, ever."""

    def post(self, request, pending_share_id):
        tenant = self.get_tenant(request)
        pending = services.reject_share(tenant, pending_share_id)
        return Response({"pending_share_id": str(pending.id), "status": pending.status})


# ── Wormholes & warp (read-only) + the souvenir ──────────────────────────────


class WormholesView(FriendsView):
    """GET /api/v1/friends/wormholes/[?warpable=sky] — warp targets: one per
    accepted neighbor with ≥1 active+ready spark shared to me (friendship_id,
    identity, spark count, new-since-last-visit, ``in_my_sky``). Placement is
    deterministic client-side. ``?warpable=sky`` returns only the CHOSEN inner
    circle (``in_my_sky AND spark_count > 0``) — the web parity for the sky flight."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(services.list_wormholes(tenant, warpable=request.query_params.get("warpable")))


class SkyView(FriendsView):
    """POST / DELETE /api/v1/friends/<friendship_id>/sky/ — add / remove a neighbor
    from MY private inner circle ("my sky"). One-way + invisible: the other party
    is never told. POST is hard-capped at 12 — a 13th returns
    ``409 {"error":"sky_full","cap":12,"sky":[...]}`` carrying the current members
    so the client renders the forced-removal swap. DELETE is a frictionless,
    idempotent un-choose (never an unfriend — the edge is untouched)."""

    def post(self, request, friendship_id):
        tenant = self.get_tenant(request)
        payload, code = services.add_neighbor_to_sky(tenant, friendship_id)
        return Response(payload, status=code)

    def delete(self, request, friendship_id):
        tenant = self.get_tenant(request)
        return Response(services.remove_neighbor_from_sky(tenant, friendship_id))


class SkyRosterView(FriendsView):
    """GET /api/v1/friends/sky/ — MY sky roster (≤12, including quiet no-spark
    slots). Visible ONLY to me: the sky is a private curation, never shown to
    anyone it contains."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(services.list_sky(tenant))


class FriendGalaxyView(FriendsView):
    """GET /api/v1/friends/<friendship_id>/galaxy/ — the neighbor's SHARED
    constellation as GalaxyData, built from frozen SharedLesson snapshots via the
    audited accessor. Read-only; ids namespaced; non-neighbor → 403."""

    def get(self, request, friendship_id):
        tenant = self.get_tenant(request)
        return Response(services.friend_galaxy(tenant, friendship_id))


class WormholeVisitedView(FriendsView):
    """POST /api/v1/friends/<friendship_id>/visited/ — advance the viewer's
    WormholeVisit watermark (kills the "new since last visit" glow)."""

    def post(self, request, friendship_id):
        tenant = self.get_tenant(request)
        return Response(services.mark_wormhole_visited(tenant, friendship_id))


class AdoptShareView(FriendsView):
    """POST /api/v1/friends/shares/<shared_lesson_id>/adopt/ — the souvenir:
    bring a neighbor's spark home as a PENDING lesson in MY tenant. Idempotent."""

    throttle_classes = [AdoptDayThrottle]

    def post(self, request, shared_lesson_id):
        tenant = self.get_tenant(request)
        payload, code = services.adopt_spark(tenant, request.user, shared_lesson_id)
        return Response(payload, status=code)


# ── Transparency ledger ("what my assistant absorbed") ───────────────────────


class AbsorbedListView(FriendsView):
    """GET /api/v1/friends/absorbed/ — the transparency ledger (un-purged)."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(services.list_absorbed(tenant))


class AbsorbedPurgeView(FriendsView):
    """POST /api/v1/friends/absorbed/<id>/purge/ — tombstone one item; the
    envelope + agent context exclude it hereafter."""

    def post(self, request, absorbed_item_id):
        tenant = self.get_tenant(request)
        item = services.purge_absorbed(tenant, absorbed_item_id)
        return Response({"id": str(item.id), "purged": True})


# ── Friend chat (1:1) — poll-is-truth, thread addressed by thread_id only ────


class ThreadsView(FriendsView):
    """GET /api/v1/friends/threads/ — my threads (unread + last message).
    POST /api/v1/friends/threads/ {friendship_id} — open (get-or-create) a thread."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(services.list_threads(tenant))

    def post(self, request):
        tenant = self.get_tenant(request)
        friendship_id = request.data.get("friendship_id")
        if not friendship_id:
            raise ValidationError("friendship_id is required.")
        thread = services.open_thread(tenant, friendship_id)
        return Response(
            {
                "thread_id": str(thread.id),
                "friendship_id": str(thread.friendship_id) if thread.friendship_id else None,
            },
            status=status.HTTP_200_OK,
        )


class ThreadMessagesView(FriendsView):
    """GET /api/v1/friends/threads/<id>/messages/?since=<cursor>&limit= — keyset feed.
    POST — send (idempotent client_msg_id)."""

    def get_throttles(self):
        # Throttle SENDS (POST) only — polling the feed (GET) must stay free, or
        # a lively conversation would rate-limit its own readers.
        if self.request.method == "POST":
            return [MessageSendHourThrottle()]
        return []

    def get(self, request, thread_id):
        tenant = self.get_tenant(request)
        return Response(
            services.get_thread_messages(
                tenant, thread_id, request.query_params.get("since"), request.query_params.get("limit")
            )
        )

    def post(self, request, thread_id):
        tenant = self.get_tenant(request)
        message, created = services.send_friend_message(
            tenant, request.user, thread_id, request.data.get("client_msg_id", ""), request.data.get("text", "")
        )
        return Response(
            {"public_id": str(message.public_id), "seq": message.seq, "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ThreadReadView(FriendsView):
    """POST /api/v1/friends/threads/<id>/read/ — advance my last_read_seq."""

    def post(self, request, thread_id):
        tenant = self.get_tenant(request)
        return Response(services.mark_thread_read(tenant, thread_id))


class ThreadMembershipView(FriendsView):
    """PATCH /api/v1/friends/threads/<id>/membership/ — toggle muted / agent_absorb_enabled."""

    def patch(self, request, thread_id):
        tenant = self.get_tenant(request)
        return Response(
            services.patch_thread_membership(
                tenant,
                thread_id,
                muted=request.data.get("muted"),
                agent_absorb_enabled=request.data.get("agent_absorb_enabled"),
            )
        )


# ── Missions (shared goals + crew projection) ────────────────────────────────


def _parse_iso_date(raw):
    from datetime import date

    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Dates must be YYYY-MM-DD.") from exc


class MissionsView(FriendsView):
    """GET /api/v1/friends/missions/ — my missions.
    POST — create a 1:1 mission on an accepted friendship (creator = owner)."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(
            services.list_missions(tenant, include_invited=_is_truthy(request.query_params.get("include_invited")))
        )

    def post(self, request):
        tenant = self.get_tenant(request)
        friendship_id = request.data.get("friendship_id")
        if not friendship_id:
            raise ValidationError("friendship_id is required.")
        mission = services.create_mission(
            tenant,
            request.user,
            friendship_id,
            title=request.data.get("title", ""),
            description=request.data.get("description", ""),
            pillar=request.data.get("pillar", ""),
            target=request.data.get("target") or {},
            target_date=_parse_iso_date(request.data.get("target_date")),
        )
        return Response({"mission_id": str(mission.id)}, status=status.HTTP_201_CREATED)


class MissionDetailView(FriendsView):
    """GET /api/v1/friends/missions/<id>/ — mission + crew projection.
    PATCH — optimistic edit (409 on version/lock conflict)."""

    def get(self, request, mission_id):
        tenant = self.get_tenant(request)
        return Response(services.get_mission_detail(tenant, mission_id))

    def patch(self, request, mission_id):
        tenant = self.get_tenant(request)
        fields = {}
        if "title" in request.data:
            fields["title"] = str(request.data.get("title") or "").strip()
        if "target" in request.data:
            fields["target"] = request.data.get("target") or {}
        if "target_date" in request.data:
            fields["target_date"] = _parse_iso_date(request.data.get("target_date"))
        payload, code = services.update_mission(
            tenant, mission_id, expected_version=request.data.get("version"), fields=fields
        )
        return Response(payload, status=code)


class MissionJoinView(FriendsView):
    def post(self, request, mission_id):
        tenant = self.get_tenant(request)
        return Response(services.join_mission(tenant, request.user, mission_id, request.data.get("commitment", "")))


class MissionDeclineView(FriendsView):
    def post(self, request, mission_id):
        return Response(services.decline_mission(self.get_tenant(request), mission_id))


class MissionLeaveView(FriendsView):
    def post(self, request, mission_id):
        tenant = self.get_tenant(request)
        return Response(services.leave_mission(tenant, mission_id))


class MissionUpdatesView(FriendsView):
    """POST /api/v1/friends/missions/<id>/updates/ — a human note/progress/milestone."""

    def post(self, request, mission_id):
        tenant = self.get_tenant(request)
        return Response(
            services.add_mission_update(
                tenant, request.user, mission_id, request.data.get("kind", "note"), request.data.get("text", "")
            )
        )


class MissionTasksView(FriendsView):
    """POST /api/v1/friends/missions/<id>/tasks/ — mint the caller's OWN journal
    Task linked to the mission + append task_added."""

    def post(self, request, mission_id):
        tenant = self.get_tenant(request)
        return Response(
            services.add_mission_task(
                tenant,
                request.user,
                mission_id,
                title=request.data.get("title", ""),
                description=request.data.get("description", ""),
                due_date=_parse_iso_date(request.data.get("due_date")),
            ),
            status=status.HTTP_201_CREATED,
        )


class GoalActionsView(FriendsView):
    """GET /api/v1/friends/mission-actions/ — my agent-proposed Mission tasks."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(services.list_pending_goal_actions(tenant))


class GoalActionApproveView(FriendsView):
    def post(self, request, action_id):
        tenant = self.get_tenant(request)
        return Response(services.approve_goal_action(tenant, action_id))


class GoalActionRejectView(FriendsView):
    def post(self, request, action_id):
        tenant = self.get_tenant(request)
        return Response(services.reject_goal_action(tenant, action_id))


# ── Circles (groups) + moderation ────────────────────────────────────────────


def _is_truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class NetworkCapabilitiesView(FriendsView):
    def get(self, request):
        self.get_tenant(request)
        return Response(
            {
                "version": 1,
                "circle_authors": True,
                "project_invitations": True,
                "project_history": True,
                "circle_assistant_choice": True,
            }
        )


def _assistant_choice(request, default=None):
    value = request.data.get("agent_absorb_enabled", default)
    if value is not None and not isinstance(value, bool):
        raise ValidationError("agent_absorb_enabled must be a boolean.")
    return value


class CirclesView(FriendsView):
    """GET /api/v1/friends/circles/ — my circles. POST — create one (I'm admin)."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(circles.list_circles(tenant))

    def post(self, request):
        tenant = self.get_tenant(request)
        circle = circles.create_circle(
            tenant,
            request.user,
            name=request.data.get("name", ""),
            description=request.data.get("description", ""),
            hue=request.data.get("hue", 210),
            agent_absorb_enabled=_assistant_choice(request, True),
        )
        return Response({"circle_id": str(circle.id)}, status=status.HTTP_201_CREATED)


class CircleJoinView(FriendsView):
    """POST /api/v1/friends/circles/join/ {invite_code} — join via code (must be a
    neighbor of the circle creator)."""

    def post(self, request):
        tenant = self.get_tenant(request)
        return Response(
            circles.join_circle(
                tenant,
                request.user,
                request.data.get("invite_code", ""),
                agent_absorb_enabled=_assistant_choice(request),
            )
        )


class CircleDetailView(FriendsView):
    def get(self, request, circle_id):
        tenant = self.get_tenant(request)
        return Response(circles.get_circle_detail(tenant, circle_id))


class CircleMembersView(FriendsView):
    """POST /api/v1/friends/circles/<id>/members/ {handle} — wave a neighbor in."""

    def post(self, request, circle_id):
        tenant = self.get_tenant(request)
        return Response(circles.add_circle_member(tenant, request.user, circle_id, request.data.get("handle", "")))


class CircleLeaveView(FriendsView):
    """POST /api/v1/friends/circles/<id>/leave/ {keep?} — leave; purge my
    circle-absorbed items by default, or keep=true to retain them."""

    def post(self, request, circle_id):
        tenant = self.get_tenant(request)
        return Response(circles.leave_circle(tenant, circle_id, purge=not _is_truthy(request.data.get("keep"))))


class CircleRemoveView(FriendsView):
    """POST /api/v1/friends/circles/<id>/remove/ {handle} — admin removes a member."""

    def post(self, request, circle_id):
        tenant = self.get_tenant(request)
        return Response(circles.remove_circle_member(tenant, circle_id, request.data.get("handle", "")))


class CircleInviteCodeView(FriendsView):
    """POST /api/v1/friends/circles/<id>/invite-code/ — admin regenerates the code."""

    def post(self, request, circle_id):
        tenant = self.get_tenant(request)
        return Response(circles.regenerate_invite_code(tenant, circle_id))


class ReportView(FriendsView):
    """POST /api/v1/friends/report/ {target_kind, target_id?, reason, detail?} —
    hide the reported item for the reporter (shared_lesson / friend_message), OR
    record a ``general`` support concern (no content id; App Review #3)."""

    def post(self, request):
        tenant = self.get_tenant(request)
        return Response(
            circles.report_content(
                tenant,
                request.user,
                target_kind=request.data.get("target_kind", ""),
                target_id=request.data.get("target_id", ""),
                reason=request.data.get("reason", ""),
                detail=request.data.get("detail", ""),
            )
        )


class ConsentView(FriendsView):
    """POST /api/v1/friends/consent/ {terms_version?} — record the Neighborhood
    EULA acknowledgment on the caller's profile (App Review 1.2 #4). Idempotent."""

    def post(self, request):
        tenant = self.get_tenant(request)
        return Response(services.record_consent(tenant, request.user, request.data.get("terms_version", "")))


class BlockedListView(FriendsView):
    """GET /api/v1/friends/blocked/ — neighbors the caller has blocked, for the
    iOS Settings Blocked-list (unblock is POST <friendship_id>/unblock/)."""

    def get(self, request):
        tenant = self.get_tenant(request)
        return Response(services.blocked_list(tenant))


class NeighborhoodHomeView(FriendsView):
    """GET /api/v1/friends/home/?since=<iso> — the aggregated home + decision-
    moments BFF (one call for the iOS home + moments dock; design ask #2)."""

    def get(self, request):
        from django.utils.dateparse import parse_datetime

        tenant = self.get_tenant(request)
        raw_since = request.query_params.get("since")
        since = parse_datetime(raw_since) if raw_since else None
        return Response(services.neighborhood_home(tenant, since=since))
