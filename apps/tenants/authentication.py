"""DRF authentication classes that set RLS context."""

import logging

from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication

from .middleware import _tenant_context, set_rls_context

logger = logging.getLogger(__name__)


class JWTAuthenticationWithRLS(JWTAuthentication):
    """Wrap SimpleJWT to set Postgres RLS session variables on success."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, token = result
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
