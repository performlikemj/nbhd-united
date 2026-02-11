"""Integration views — list, connect (OAuth callback), disconnect."""
import logging
import secrets
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.core.cache import cache
from django.core import signing
from django.http import HttpResponse
from django.shortcuts import redirect
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import User
from .models import Integration
from .serializers import IntegrationSerializer
from .services import connect_integration, disconnect_integration, get_provider_config

logger = logging.getLogger(__name__)
OAUTH_STATE_MAX_AGE_SECONDS = 600
OAUTH_STATE_NONCE_CACHE_KEY_PREFIX = "oauth-state-nonce"

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


def _redirect_to_integrations(frontend_url: str, params: dict[str, str]) -> HttpResponse:
    query = urlencode(params)
    return redirect(f"{frontend_url}/integrations?{query}")


def _state_nonce_cache_key(nonce: str) -> str:
    return f"{OAUTH_STATE_NONCE_CACHE_KEY_PREFIX}:{nonce}"


def _consume_state_nonce(nonce: str) -> bool:
    key = _state_nonce_cache_key(nonce)
    if cache.get(key) is None:
        return False
    cache.delete(key)
    return True


def _build_oauth_state(user_id: str, provider: str) -> str:
    nonce = secrets.token_urlsafe(24)
    cache.set(_state_nonce_cache_key(nonce), "1", timeout=OAUTH_STATE_MAX_AGE_SECONDS)
    return signing.dumps(
        {"user_id": user_id, "provider": provider, "nonce": nonce},
        salt="oauth",
    )


def _load_oauth_state(state: str, provider: str) -> dict[str, str]:
    data = signing.loads(state, salt="oauth", max_age=OAUTH_STATE_MAX_AGE_SECONDS)
    if data.get("provider") != provider:
        raise signing.BadSignature("provider mismatch")
    nonce = data.get("nonce", "")
    if not isinstance(nonce, str) or not nonce or not _consume_state_nonce(nonce):
        raise signing.BadSignature("state nonce missing/invalid")
    return data


def _get_credentials(provider: str) -> tuple[str, str]:
    config = get_provider_config(provider)
    group = config["provider_group"]
    creds = OAUTH_CLIENT_CREDENTIALS[group]
    return creds["client_id"](), creds["client_secret"]()


def _get_callback_url(provider: str) -> str:
    api_base = getattr(settings, "API_BASE_URL", "http://localhost:8000")
    return f"{api_base}/api/v1/integrations/callback/{provider}/"


def _fetch_google_email(access_token: str) -> str | None:
    try:
        resp = httpx.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Failed to fetch Google userinfo email")
        return None

    payload = resp.json()
    email = payload.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip().lower()
    return None


def fetch_provider_email(provider: str, tokens: dict) -> str | None:
    access_token = tokens.get("access_token")
    if not access_token:
        return None

    config = get_provider_config(provider)
    if config.get("provider_group") == "google":
        return _fetch_google_email(access_token)

    return None


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

        client_id, client_secret = _get_credentials(provider)
        if not client_id or not client_secret:
            return Response(
                {"detail": f"OAuth not configured for {provider}."},
                status=400,
            )

        state = _build_oauth_state(str(request.user.id), provider)

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
            return _redirect_to_integrations(frontend_url, {"error": error})

        try:
            get_provider_config(provider)
        except ValueError:
            return _redirect_to_integrations(frontend_url, {"error": "unknown_provider"})

        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")

        if not state or not code:
            return _redirect_to_integrations(frontend_url, {"error": "missing_params"})

        try:
            data = _load_oauth_state(state, provider)
        except signing.BadSignature:
            return _redirect_to_integrations(frontend_url, {"error": "invalid_state"})

        try:
            user = User.objects.get(id=data["user_id"])
        except User.DoesNotExist:
            return _redirect_to_integrations(frontend_url, {"error": "user_not_found"})

        if not hasattr(user, "tenant"):
            return _redirect_to_integrations(frontend_url, {"error": "no_tenant"})

        try:
            config = get_provider_config(provider)
            client_id, client_secret = _get_credentials(provider)
            if not client_id or not client_secret:
                return _redirect_to_integrations(
                    frontend_url,
                    {"error": "oauth_not_configured"},
                )

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
            provider_email = fetch_provider_email(provider, tokens)

            connect_integration(
                tenant=user.tenant,
                provider=provider,
                tokens=tokens,
                provider_email=provider_email,
            )

            return _redirect_to_integrations(frontend_url, {"connected": provider})
        except httpx.HTTPError:
            logger.exception("OAuth token exchange failed for %s", provider)
            return _redirect_to_integrations(frontend_url, {"error": "exchange_failed"})
        except Exception:
            logger.exception("OAuth callback failed for %s", provider)
            return _redirect_to_integrations(frontend_url, {"error": "callback_failed"})
