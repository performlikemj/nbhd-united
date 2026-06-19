"""OAuth/PKCE web→app handoff: begin (authorize) + exchange.

Two endpoints back the iOS "Create an account" handoff (see
``oauth_models.py`` for the protocol overview):

* ``POST /api/v1/auth/authorize/`` — Bearer-authed. The web SPA calls this
  the moment it holds a fresh access token (just after register/sign-in) to
  mint a one-time code for the already-authenticated user.
* ``POST /api/v1/auth/exchange/`` — ``AllowAny``. The app swaps the code +
  its PKCE verifier for a SimpleJWT pair. EVERY failure collapses to an
  identical ``400 {"error": "invalid_grant"}`` (no oracle); reasons are logged
  server-side only.
"""

import hmac
import logging
import re
from datetime import timedelta

from django.conf import settings as dj
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .oauth_models import (
    OAuthAuthorizationCode,
    generate_authorization_code,
    hash_authorization_code,
    pkce_s256,
)
from .serializers import EmailTokenObtainPairSerializer
from .throttling import AuthorizeMintMinuteThrottle, ExchangeMinuteThrottle

logger = logging.getLogger(__name__)

# base64url alphabet (RFC 4648 §5, no padding) — the shape of a valid S256
# code_challenge. Reject anything else at the begin step so a malformed
# challenge can never be stored (and so the constant-time compare at exchange
# only ever sees ASCII).
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _invalid_grant() -> Response:
    """A fresh generic-failure response per call.

    Never share a single ``Response`` instance across requests — DRF mutates it
    during rendering (``.accepted_renderer`` etc.), so a module-level singleton
    would break on the second request.
    """
    return Response({"error": "invalid_grant"}, status=status.HTTP_400_BAD_REQUEST)


class AuthorizeBeginView(APIView):
    """POST (Bearer) → mint a one-time PKCE code for the authenticated web user."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [AuthorizeMintMinuteThrottle]

    def post(self, request):
        code_challenge = (request.data.get("code_challenge") or "").strip()
        method = (request.data.get("code_challenge_method") or "").strip()
        redirect_uri = (request.data.get("redirect_uri") or "").strip()

        if (
            method != "S256"
            or not code_challenge
            or not _B64URL_RE.match(code_challenge)
            or redirect_uri not in dj.AUTH_ALLOWED_REDIRECT_URIS
        ):
            return Response(
                {"error": "invalid_request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw, code_hash = generate_authorization_code()
        OAuthAuthorizationCode.objects.create(
            user=request.user,
            code_hash=code_hash,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            redirect_uri=redirect_uri,
            client=(request.data.get("client") or "ios"),
            expires_at=timezone.now() + timedelta(seconds=dj.AUTH_EXCHANGE_CODE_TTL_SECONDS),
        )
        return Response({"code": raw}, status=status.HTTP_200_OK)


class ExchangeView(APIView):
    """POST {code, code_verifier, redirect_uri} → {access, refresh}.

    ALL failures collapse to a generic ``400 invalid_grant`` (no oracle).
    """

    permission_classes = [AllowAny]
    throttle_classes = [ExchangeMinuteThrottle]

    def post(self, request):
        code = (request.data.get("code") or "").strip()
        verifier = (request.data.get("code_verifier") or "").strip()
        redirect_uri = (request.data.get("redirect_uri") or "").strip()
        if not code or not verifier or not redirect_uri:
            logger.info("auth.exchange.invalid reason=missing_fields")
            return _invalid_grant()

        code_hash = hash_authorization_code(code)
        with transaction.atomic():
            try:
                row = (
                    OAuthAuthorizationCode.objects.select_for_update(of=("self",))
                    .select_related("user")
                    .get(code_hash=code_hash)
                )
            except OAuthAuthorizationCode.DoesNotExist:
                logger.info("auth.exchange.invalid reason=not_found")
                return _invalid_grant()

            # Single-use + TTL, re-checked inside the lock.
            if row.consumed_at is not None or timezone.now() >= row.expires_at:
                logger.info(
                    "auth.exchange.invalid reason=%s code_hash=%s",
                    "consumed" if row.consumed_at is not None else "expired",
                    code_hash[:8],
                )
                return _invalid_grant()

            # Bind to the redirect_uri it was minted for (constant-time).
            if not hmac.compare_digest(redirect_uri, row.redirect_uri):
                logger.info(
                    "auth.exchange.invalid reason=redirect_mismatch code_hash=%s",
                    code_hash[:8],
                )
                return _invalid_grant()

            # Verify PKCE. Always run compare_digest (no short-circuit) so a
            # non-ASCII verifier — which can't encode to a base64url challenge,
            # yielding "" — is timing-indistinguishable from a plain mismatch.
            try:
                computed_challenge = pkce_s256(verifier)
            except (UnicodeEncodeError, ValueError):
                computed_challenge = ""
            if not hmac.compare_digest(computed_challenge, row.code_challenge):
                logger.info(
                    "auth.exchange.invalid reason=pkce_mismatch code_hash=%s",
                    code_hash[:8],
                )
                return _invalid_grant()

            user = row.user
            if not user.is_active:
                logger.info(
                    "auth.exchange.invalid reason=inactive_user code_hash=%s",
                    code_hash[:8],
                )
                return _invalid_grant()

            # Consume inside the lock — committing marks it used even though the
            # (DB-free) token mint happens after the transaction. A save()
            # failure here rolls back and propagates (a 500 surfacing the real
            # fault) rather than silently burning the code.
            row.consumed_at = timezone.now()
            row.save(update_fields=["consumed_at"])

        # pw_iat-carrying mint — NOT RefreshToken.for_user (which omits the
        # claim and would be rejected by JWTAuthenticationWithRLS for any user
        # who has ever rotated their password). Matches PasswordResetConfirmView.
        refresh = EmailTokenObtainPairSerializer.get_token(user)
        logger.info("auth.exchange.success user_id=%s", user.id)
        return Response(
            {"access": str(refresh.access_token), "refresh": str(refresh)},
            status=status.HTTP_200_OK,
        )
