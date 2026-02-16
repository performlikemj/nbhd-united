"""BYOK LLM configuration API views."""
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .crypto import decrypt_api_key, encrypt_api_key
from .models import UserLLMConfig


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
        return Response(LLMConfigSerializer(config).data)
