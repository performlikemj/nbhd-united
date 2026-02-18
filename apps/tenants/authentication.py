"""DRF authentication class that sets RLS context after JWT auth."""
from rest_framework_simplejwt.authentication import JWTAuthentication

from .middleware import _tenant_context, set_rls_context


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
