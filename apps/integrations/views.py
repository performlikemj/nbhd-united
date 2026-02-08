"""Integration views — list, connect (OAuth callback), disconnect."""
import logging
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.core import signing
from django.shortcuts import redirect
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import User
from .models import Integration
from .serializers import IntegrationSerializer
from .services import OAUTH_PROVIDERS, connect_integration, disconnect_integration, get_provider_config

logger = logging.getLogger(__name__)

OAUTH_CLIENT_CREDENTIALS = {
    "google": {
        "client_id": lambda: settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": lambda: settings.GOOGLE_OAUTH_CLIENT_SECRET,
    },
    "sautai": {
        "client_id": lambda: settings.SAUTAI_OAUTH_CLIENT_ID,
        "client_secret": lambda: settings.SAUTAI_OAUTH_CLIENT_SECRET,
    },
}


def _get_credentials(provider: str) -> tuple[str, str]:
    config = get_provider_config(provider)
    group = config["provider_group"]
    creds = OAUTH_CLIENT_CREDENTIALS[group]
    return creds["client_id"](), creds["client_secret"]()


def _get_callback_url(provider: str) -> str:
    api_base = getattr(settings, "API_BASE_URL", "http://localhost:8000")
    return f"{api_base}/api/v1/integrations/callback/{provider}/"


class IntegrationViewSet(viewsets.ReadOnlyModelViewSet):
    """List and manage integrations for the current tenant."""
    serializer_class = IntegrationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if hasattr(self.request.user, "tenant"):
            return Integration.objects.filter(tenant=self.request.user.tenant)
        return Integration.objects.none()

    @action(detail=True, methods=["post"], url_path="disconnect")
    def disconnect(self, request, pk=None):
        """Disconnect an integration — revokes tokens."""
        integration = self.get_object()
        disconnect_integration(integration.tenant, integration.provider)
        return Response({"status": "disconnected"}, status=status.HTTP_200_OK)


class OAuthAuthorizeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, provider):
        try:
            config = get_provider_config(provider)
        except ValueError:
            return Response({"detail": f"Unknown provider: {provider}"}, status=400)

        client_id, _ = _get_credentials(provider)
        if not client_id:
            return Response(
                {"detail": f"OAuth not configured for {provider}."},
                status=400,
            )

        state = signing.dumps(
            {"user_id": str(request.user.id)},
            salt="oauth",
        )

        params = {
            "client_id": client_id,
            "redirect_uri": _get_callback_url(provider),
            "response_type": "code",
            "scope": " ".join(config["scopes"]),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }

        auth_url = f"{config['auth_url']}?{urlencode(params)}"
        return Response({"url": auth_url})


class OAuthCallbackView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, provider):
        frontend_url = settings.FRONTEND_URL
        error = request.query_params.get("error")
        if error:
            return redirect(f"{frontend_url}/integrations?error={error}")

        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")

        if not state or not code:
            return redirect(f"{frontend_url}/integrations?error=missing_params")

        try:
            data = signing.loads(state, salt="oauth", max_age=600)
        except signing.BadSignature:
            return redirect(f"{frontend_url}/integrations?error=invalid_state")

        try:
            user = User.objects.get(id=data["user_id"])
        except User.DoesNotExist:
            return redirect(f"{frontend_url}/integrations?error=user_not_found")

        if not hasattr(user, "tenant"):
            return redirect(f"{frontend_url}/integrations?error=no_tenant")

        try:
            config = get_provider_config(provider)
            client_id, client_secret = _get_credentials(provider)

            resp = httpx.post(
                config["token_url"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _get_callback_url(provider),
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=20.0,
            )
            resp.raise_for_status()
            tokens = resp.json()

            connect_integration(
                tenant=user.tenant,
                provider=provider,
                tokens=tokens,
            )

            return redirect(f"{frontend_url}/integrations?connected={provider}")
        except Exception:
            logger.exception("OAuth callback failed for %s", provider)
            return redirect(f"{frontend_url}/integrations?error=exchange_failed")
