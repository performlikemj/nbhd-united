"""BYOK LLM configuration API views."""
import logging

import requests as http_requests
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .crypto import decrypt_api_key, encrypt_api_key
from .models import Tenant, UserLLMConfig
from .provider_models import fetch_models

logger = logging.getLogger(__name__)


class LLMConfigSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=UserLLMConfig.Provider.choices)
    model_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=True)
    # Read-only fields
    key_masked = serializers.SerializerMethodField()
    has_key = serializers.SerializerMethodField()

    def get_key_masked(self, obj):
        if not obj.encrypted_api_key:
            return ""
        try:
            raw = decrypt_api_key(obj.encrypted_api_key)
            if len(raw) <= 8:
                return "***"
            return f"{raw[:3]}...{raw[-3:]}"
        except Exception:
            return "***"

    def get_has_key(self, obj):
        return bool(obj.encrypted_api_key)


class LLMConfigView(APIView):
    """GET/PUT the authenticated user's BYOK LLM configuration."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            config = request.user.llm_config
        except UserLLMConfig.DoesNotExist:
            return Response(
                {"provider": "anthropic", "model_id": "", "key_masked": "", "has_key": False}
            )
        return Response(LLMConfigSerializer(config).data)

    def put(self, request):
        serializer = LLMConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        config, _created = UserLLMConfig.objects.get_or_create(
            user=request.user,
            defaults={
                "provider": data["provider"],
                "model_id": data.get("model_id", ""),
            },
        )

        config.provider = data["provider"]
        config.model_id = data.get("model_id", "") or config.model_id

        api_key = data.get("api_key")
        if api_key:
            config.encrypted_api_key = encrypt_api_key(api_key)

        config.save()

        try:
            tenant = request.user.tenant
            if tenant.status == Tenant.Status.ACTIVE:
                tenant.bump_pending_config()
        except Tenant.DoesNotExist:
            pass

        return Response(LLMConfigSerializer(config).data)


class FetchModelsView(APIView):
    """POST: fetch available models from a provider using the user's API key."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Only BYOK users can fetch models
        try:
            tenant = request.user.tenant
            if tenant.model_tier != "byok":
                return Response(
                    {"error": "Only available on the BYOK plan."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        except Tenant.DoesNotExist:
            return Response(
                {"error": "No active subscription."},
                status=status.HTTP_403_FORBIDDEN,
            )

        provider = request.data.get("provider", "").strip()
        api_key = request.data.get("api_key", "").strip()

        if not provider:
            return Response(
                {"error": "Provider is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fall back to stored key if none provided
        if not api_key:
            try:
                config = request.user.llm_config
                if config.encrypted_api_key:
                    api_key = decrypt_api_key(config.encrypted_api_key)
            except UserLLMConfig.DoesNotExist:
                pass

        if not api_key:
            return Response(
                {"error": "No API key provided and no key stored."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            models = fetch_models(provider, api_key)
            return Response({"models": models})
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except http_requests.HTTPError as exc:
            resp = exc.response
            if resp is not None and resp.status_code == 401:
                return Response(
                    {"error": "Invalid API key."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            logger.warning("Provider API error for %s: %s", provider, exc)
            return Response(
                {"error": "Could not reach provider. Try again later."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except http_requests.Timeout:
            return Response(
                {"error": "Provider did not respond in time."},
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
