"""PAT management views — create, list, revoke personal access tokens."""

from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .pat_models import PersonalAccessToken, generate_pat
from .permissions import ALLOWED_PAT_SCOPES
from .throttling import UserPATMintHourThrottle

DEFAULT_PAT_SCOPES: list[str] = ["sessions:write"]


class PATCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    scopes = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    expires_in_days = serializers.IntegerField(required=False, min_value=1, max_value=365)

    def validate_scopes(self, value: list[str]) -> list[str]:
        invalid = [s for s in value if s not in ALLOWED_PAT_SCOPES]
        if invalid:
            raise serializers.ValidationError(
                f"Unknown scopes: {invalid}. Allowed: {sorted(ALLOWED_PAT_SCOPES)}"
            )
        return value


class PATListSerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonalAccessToken
        fields = (
            "id",
            "name",
            "token_prefix",
            "scopes",
            "last_used_at",
            "expires_at",
            "revoked_at",
            "created_at",
        )
        read_only_fields = fields


class PATListView(APIView):
    """List all PATs for the authenticated user."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        pats = PersonalAccessToken.objects.filter(user=request.user, revoked_at__isnull=True)
        return Response(PATListSerializer(pats, many=True).data)


class PATCreateView(APIView):
    """Create a new PAT. Returns the raw token exactly once."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [UserPATMintHourThrottle]

    def post(self, request):
        serializer = PATCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        raw_token, prefix, token_hash = generate_pat()

        expires_at = None
        if "expires_in_days" in data:
            from datetime import timedelta

            expires_at = timezone.now() + timedelta(days=data["expires_in_days"])

        scopes = data.get("scopes") or list(DEFAULT_PAT_SCOPES)

        pat = PersonalAccessToken.objects.create(
            user=request.user,
            name=data["name"],
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=scopes,
            expires_at=expires_at,
        )

        return Response(
            {
                "id": str(pat.id),
                "name": pat.name,
                "token": raw_token,
                "token_prefix": pat.token_prefix,
                "scopes": pat.scopes,
                "expires_at": pat.expires_at,
                "created_at": pat.created_at,
                "warning": "Store this token securely — it will not be shown again.",
            },
            status=status.HTTP_201_CREATED,
        )


class PATRevokeView(APIView):
    """Revoke a PAT by ID."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, token_id):
        try:
            pat = PersonalAccessToken.objects.get(id=token_id, user=request.user)
        except PersonalAccessToken.DoesNotExist:
            return Response({"detail": "Token not found."}, status=status.HTTP_404_NOT_FOUND)

        if pat.revoked_at is not None:
            return Response({"detail": "Token already revoked."}, status=status.HTTP_400_BAD_REQUEST)

        pat.revoked_at = timezone.now()
        pat.save(update_fields=["revoked_at"])

        return Response(status=status.HTTP_204_NO_CONTENT)
