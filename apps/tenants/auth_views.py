"""Authentication views — signup, login, logout, me, password reset."""

import logging

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.common.cache import tenant_cache

from .models import Tenant
from .serializers import EmailTokenObtainPairSerializer, TenantSerializer, UserSerializer

User = get_user_model()

logger = logging.getLogger(__name__)

# Per-IP and per-email rate limits for password-reset requests. Tuned to
# stop credential-stuffing-style enumeration without being painful for a
# legitimate user who fat-fingers their email a few times.
PASSWORD_RESET_RATE_LIMIT_PER_IP = 5
PASSWORD_RESET_RATE_LIMIT_PER_EMAIL = 3
PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 hour


def _client_ip(request) -> str:
    """Extract the originating client IP, honouring the Container Apps proxy header."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _rate_limited(key: str, limit: int) -> bool:
    """Increment a per-window counter at `key` and report whether it exceeded `limit`."""
    try:
        count = cache.get(key, 0) + 1
        cache.set(key, count, timeout=PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS)
    except Exception:
        # Fail open if the cache is unreachable — a transient Redis blip
        # shouldn't lock real users out of recovery.
        logger.warning("password_reset.rate_limit.cache_unavailable", exc_info=True)
        return False
    return count > limit


def _send_password_reset_email(user, uid: str, token: str) -> None:
    """Send the reset link to the user. Failures are logged but do not surface."""
    frontend_url = getattr(django_settings, "FRONTEND_URL", "").rstrip("/")
    reset_url = f"{frontend_url}/reset-password?uid={uid}&token={token}"
    context = {
        "user": user,
        "reset_url": reset_url,
        "display_name": getattr(user, "display_name", None) or user.email,
    }
    subject = render_to_string("email/password_reset_subject.txt", context).strip()
    text_body = render_to_string("email/password_reset_body.txt", context)
    html_body = render_to_string("email/password_reset_body.html", context)
    try:
        send_mail(
            subject=subject,
            message=text_body,
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            recipient_list=[user.email],
            html_message=html_body,
            fail_silently=False,
        )
    except Exception:
        # We deliberately don't surface email failures to the caller — that
        # would leak existence/non-existence of the account.
        logger.exception("password_reset.email_send_failed user_id=%s", user.pk)


class PasswordResetRequestView(APIView):
    """POST {email} → always 200. Sends a reset email if the user exists."""

    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        ip = _client_ip(request)

        # Rate-limit before doing any DB work. Both buckets are checked so
        # an attacker can't pivot between IPs to enumerate one email, or
        # between emails from one IP.
        if email and _rate_limited(f"pwd_reset_email:{email}", PASSWORD_RESET_RATE_LIMIT_PER_EMAIL):
            return Response(status=status.HTTP_429_TOO_MANY_REQUESTS)
        if ip and _rate_limited(f"pwd_reset_ip:{ip}", PASSWORD_RESET_RATE_LIMIT_PER_IP):
            return Response(status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Always do the lookup so timing doesn't reveal account existence.
        user = User.objects.filter(email__iexact=email).first() if email else None
        if user is not None and user.is_active:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            _send_password_reset_email(user, uid, token)

        # Constant-shape response regardless of whether the email existed.
        return Response(
            {"detail": ("If an account exists for that email, a reset link is on its way.")},
            status=status.HTTP_200_OK,
        )


class PasswordResetConfirmView(APIView):
    """POST {uid, token, new_password} → set password + return a fresh JWT pair."""

    permission_classes = [AllowAny]

    def post(self, request):
        uid = request.data.get("uid") or ""
        token = request.data.get("token") or ""
        new_password = request.data.get("new_password") or ""

        if not uid or not token or not new_password:
            return Response(
                {"detail": "uid, token, and new_password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user_pk = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=user_pk)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response(
                {"detail": "Reset link is invalid or has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not default_token_generator.check_token(user, token):
            # Tokens auto-invalidate on password change (because last_login
            # / password hash feed the token's HMAC) and after
            # PASSWORD_RESET_TIMEOUT (3 days by default).
            return Response(
                {"detail": "Reset link is invalid or has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            return Response(
                {"detail": " ".join(exc.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(new_password)
        # Persist the password_last_changed_at stamp that the custom
        # ``set_password`` override bumps in memory. Omitting it from
        # update_fields silently drops the bump, which (a) defeats
        # force-logout-on-rotation (old JWTs survive the reset) and
        # (b) leaves a stale stamp that would reject the token we mint below.
        user.save(update_fields=["password", "password_last_changed_at"])

        # Sign the user back in so they land on the dashboard without a
        # second login step. Mint via the serializer's ``get_token`` so the
        # token carries the ``pw_iat`` claim — ``RefreshToken.for_user``
        # omits it, and JWTAuthenticationWithRLS then rejects the brand-new
        # token as "issued before the last password change" for any user
        # with a non-null password_last_changed_at.
        refresh = EmailTokenObtainPairSerializer.get_token(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_200_OK,
        )


class SignupView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")
        display_name = request.data.get("display_name", "Friend")

        if not email or not password:
            return Response(
                {"detail": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        required_code = getattr(django_settings, "PREVIEW_ACCESS_KEY", "")
        if required_code:
            invite_code = request.data.get("invite_code", "")
            if invite_code != required_code:
                return Response(
                    {"detail": "A valid invite code is required to create an account."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        if User.objects.filter(email=email).exists():
            return Response(
                {"detail": "A user with this email already exists."},
                status=status.HTTP_409_CONFLICT,
            )

        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            display_name=display_name,
        )

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_201_CREATED,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response(
                {"detail": "Refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except TokenError:
            return Response(
                {"detail": "Invalid refresh token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @tenant_cache(ttl=60, tag="tenant")
    def get(self, request):
        user = request.user
        user_data = UserSerializer(user).data

        try:
            tenant = user.tenant
            user_data["tenant"] = TenantSerializer(tenant).data
        except Tenant.DoesNotExist:
            user_data["tenant"] = None

        return Response(user_data)
