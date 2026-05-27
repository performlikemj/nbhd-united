"""DRF authentication classes that set RLS context."""

import logging

from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication

from .middleware import _tenant_context, set_rls_context

logger = logging.getLogger(__name__)


class JWTAuthenticationWithRLS(JWTAuthentication):
    """Wrap SimpleJWT to set Postgres RLS session variables on success
    and to enforce password-rotation-based force-logout.

    Force-logout protocol: tokens carry a ``pw_iat`` claim (set by
    ``EmailTokenObtainPairSerializer.get_token``) holding the unix
    timestamp of the user's ``password_last_changed_at`` at issue time.
    On every request, we compare it against the current
    ``user.password_last_changed_at`` and reject if the password has
    been rotated since the token was minted. Both access and refresh
    tokens carry the claim, so a rotation invalidates every outstanding
    session without needing the simplejwt token_blacklist app.

    Legacy tokens (issued before this code shipped) carry no claim;
    we treat that as ``pw_iat=0`` and only reject if the user has a
    non-null ``password_last_changed_at`` — meaning a rotation has
    happened since the token's issue.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, token = result

        # Force-logout enforcement.
        pw_changed_at = getattr(user, "password_last_changed_at", None)
        if pw_changed_at is not None:
            token_pw_iat = int(token.get("pw_iat") or 0)
            if token_pw_iat < int(pw_changed_at.timestamp()):
                logger.info(
                    "Rejecting JWT for user %s — token pw_iat=%d < password_last_changed_at=%s",
                    user.id,
                    token_pw_iat,
                    pw_changed_at.isoformat(),
                )
                raise exceptions.AuthenticationFailed(
                    "Session expired due to a security update. Please sign in again.",
                    code="password_rotated",
                )

        tenant = getattr(user, "tenant", None)
        if tenant:
            _tenant_context.tenant = tenant
            set_rls_context(tenant_id=tenant.id, user_id=user.id)

        return result


class PersonalAccessTokenAuthentication(BaseAuthentication):
    """Authenticate requests bearing a ``pat_…`` token.

    Reads ``Authorization: Bearer pat_<secret>``, SHA-256 hashes it,
    looks up the matching PersonalAccessToken row, checks validity,
    sets RLS context, and stamps ``last_used_at``.
    """

    keyword = "Bearer"

    def authenticate_header(self, request):
        return 'Bearer realm="api"'

    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith(f"{self.keyword} pat_"):
            return None  # Not a PAT — let the next auth class try

        raw_token = auth_header[len(self.keyword) + 1 :]  # strip "Bearer "

        from .pat_models import PersonalAccessToken, hash_token

        token_hash = hash_token(raw_token)
        try:
            pat = PersonalAccessToken.objects.select_related("user", "user__tenant").get(
                token_hash=token_hash,
            )
        except PersonalAccessToken.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid token.")

        if not pat.is_valid:
            if pat.revoked_at is not None:
                raise exceptions.AuthenticationFailed("Token has been revoked.")
            raise exceptions.AuthenticationFailed("Token has expired.")

        user = pat.user
        tenant = getattr(user, "tenant", None)
        if tenant:
            _tenant_context.tenant = tenant
            set_rls_context(tenant_id=tenant.id, user_id=user.id)

        # Stamp last_used_at (fire-and-forget UPDATE, no full save)
        pat.touch()

        # Stash the PAT on the request for downstream scope checks
        request.auth_pat = pat
        return (user, pat)
