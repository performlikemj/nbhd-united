"""
Telegram linking API endpoints.

- POST /api/v1/tenants/telegram/generate-link/  → QR code + deep link
- POST /api/v1/tenants/telegram/unlink/         → Remove Telegram link
- GET  /api/v1/tenants/telegram/status/         → Link status
"""
import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import telegram_service as svc

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def telegram_generate_link(request):
    """Generate a one-time QR code + deep link for Telegram linking."""
    user = request.user

    # If already linked, tell them
    if user.telegram_user_id:
        return Response(
            {"error": "Telegram already linked. Unlink first to reconnect."},
            status=400,
        )

    try:
        token = svc.generate_link_token(user)
        return Response({
            "deep_link": svc.get_deep_link(token.token),
            "qr_code": svc.get_qr_code_data_url(token.token),
            "expires_at": token.expires_at.isoformat(),
        })
    except Exception as e:
        logger.exception("Error generating Telegram link for user %s: %s", user.id, e)
        return Response({"error": "Failed to generate link."}, status=500)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def telegram_unlink(request):
    """Unlink the user's Telegram account."""
    success = svc.unlink_telegram(request.user)
    if success:
        return Response({"success": True})
    return Response({"error": "Telegram not linked."}, status=400)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def telegram_status(request):
    """Get the user's Telegram link status."""
    return Response(svc.get_telegram_status(request.user))
