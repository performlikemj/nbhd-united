"""Tests for the decrypt-audit principal boundaries (PR H).

Wiring under test: ``TenantContextMiddleware`` sets the ambient decrypt-audit
principal to "system" at request entry and resets it at teardown; the JWT and
PAT auth classes upgrade an authenticated subscriber to "owner_request". See
``apps/crypto/audit.py``'s module docstring for the full boundary map.
"""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from apps.crypto import audit
from apps.tenants.authentication import JWTAuthenticationWithRLS, PersonalAccessTokenAuthentication
from apps.tenants.middleware import TenantContextMiddleware, reset_rls_context
from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import PersonalAccessToken, generate_pat
from apps.tenants.serializers import EmailTokenObtainPairSerializer


def _make_user_and_tenant(email: str) -> tuple[User, Tenant]:
    user = User.objects.create(username=email, email=email, display_name="Test")
    user.set_password("pw-initial")
    user.save()
    tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
    return user, tenant


class _PrincipalResetMixin:
    """Snapshot + restore the ambient principal so test order never matters."""

    def setUp(self):
        super().setUp()
        token = audit._PRINCIPAL.set("system")
        self.addCleanup(audit._PRINCIPAL.reset, token)


class MiddlewarePrincipalBoundaryTest(_PrincipalResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.mw = TenantContextMiddleware(get_response=lambda r: HttpResponse())

    def test_process_request_clears_a_stale_principal_from_a_prior_request(self):
        # Simulate a reused gunicorn worker whose ContextVar still holds the
        # previous request's owner_request. process_request must reset to system
        # BEFORE auth, so this request never inherits the stale attribution.
        audit.set_principal("owner_request")
        request = self.factory.get("/")
        request.user = AnonymousUser()

        self.mw.process_request(request)

        self.assertEqual(audit.get_principal(), "system")

    def test_process_response_resets_the_principal(self):
        request = self.factory.get("/")
        request.user = AnonymousUser()
        # Auth upgraded it mid-request; teardown must clear it.
        audit.set_principal("owner_request")

        self.mw.process_response(request, HttpResponse())

        self.assertEqual(audit.get_principal(), "system")


class AuthPrincipalUpgradeTest(_PrincipalResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        # The auth classes call set_rls_context (a session-scoped SET on the
        # reused test connection). Clear it after each test so RLS state never
        # bleeds into the next test on this connection.
        self.addCleanup(lambda: reset_rls_context(force=True))

    def test_jwt_auth_upgrades_to_owner_request(self):
        user, _tenant = _make_user_and_tenant("jwt-principal@test.com")
        access = str(EmailTokenObtainPairSerializer.get_token(user).access_token)
        request = self.factory.get("/", HTTP_AUTHORIZATION=f"Bearer {access}")

        result = JWTAuthenticationWithRLS().authenticate(request)

        self.assertIsNotNone(result)
        self.assertEqual(audit.get_principal(), "owner_request")

    def test_pat_auth_upgrades_to_owner_request(self):
        user, _tenant = _make_user_and_tenant("pat-principal@test.com")
        raw_token, prefix, token_hash = generate_pat()
        PersonalAccessToken.objects.create(user=user, name="test", token_prefix=prefix, token_hash=token_hash)
        request = self.factory.get("/", HTTP_AUTHORIZATION=f"Bearer {raw_token}")

        result = PersonalAccessTokenAuthentication().authenticate(request)

        self.assertIsNotNone(result)
        self.assertEqual(audit.get_principal(), "owner_request")
