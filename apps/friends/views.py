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

from . import services
from .serializers import InviteCreateSerializer, NeighborProfileSerializer, WaveCreateSerializer
from .throttling import WaveSendDayThrottle


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
        if not lesson_id or not friendship_id:
            raise ValidationError("lesson_id and friendship_id are required.")
        payload, code = services.preview_share(tenant, lesson_id, friendship_id)
        return Response(payload, status=code)


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
