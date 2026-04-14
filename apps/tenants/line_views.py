"""
LINE linking API endpoints.

- POST /api/v1/tenants/line/generate-link/  → QR code + deep link
- POST /api/v1/tenants/line/unlink/         → Remove LINE link
- GET  /api/v1/tenants/line/status/         → Link status
- PATCH /api/v1/tenants/preferred-channel/  → Set preferred channel
"""

import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.router import line_service as svc

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def line_generate_link(request):
    """Generate a one-time QR code + deep link for LINE linking."""
    user = request.user

    # If already linked, tell them
    if user.line_user_id:
        return Response(
            {"error": "LINE already linked. Unlink first to reconnect."},
            status=400,
        )

    try:
        token = svc.generate_link_token(user)
        return Response(
            {
                "deep_link": svc.get_deep_link(token.token),
                "qr_code": svc.get_qr_code_data_url(token.token),
                "expires_at": token.expires_at.isoformat(),
            }
        )
    except Exception as e:
        logger.exception("Error generating LINE link for user %s: %s", user.id, e)
        return Response({"error": "Failed to generate link."}, status=500)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def line_unlink(request):
    """Unlink the user's LINE account."""
    success = svc.unlink_line(request.user)
    if success:
        return Response({"success": True})
    return Response({"error": "LINE not linked."}, status=400)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def line_status(request):
    """Get the user's LINE link status."""
    return Response(svc.get_line_status(request.user))


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def line_set_preferred_channel(request):
    """Set the user's preferred messaging channel."""
    channel = request.data.get("preferred_channel")
    if channel not in ("telegram", "line"):
        return Response(
            {"error": "Invalid channel. Must be 'telegram' or 'line'."},
            status=400,
        )

    user = request.user

    # Verify the chosen channel is actually linked
    if channel == "line" and not user.line_user_id:
        return Response(
            {"error": "LINE is not linked. Connect LINE first."},
            status=400,
        )
    if channel == "telegram" and not user.telegram_chat_id:
        return Response(
            {"error": "Telegram is not linked. Connect Telegram first."},
            status=400,
        )

    user.preferred_channel = channel
    user.save(update_fields=["preferred_channel"])

    return Response(
        {
            "preferred_channel": channel,
            "message": f"Preferred channel set to {channel.title()}.",
        }
    )
