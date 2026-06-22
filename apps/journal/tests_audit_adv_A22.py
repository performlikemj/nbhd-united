"""
Adversarial-audit regression tests — cluster A22 (FA-0744).

Pins the get_permissions() override on SessionDetailView so that a future
revert of the DELETE branch to the read scope would fail the suite rather
than pass silently.

These tests complement SessionScopeEnforcementTest in tests_sessions.py;
they intentionally duplicate the _pat_with_scopes helper and session fixture
so this file is self-contained and safe to run in isolation.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.pat_models import PersonalAccessToken, generate_pat
from apps.tenants.services import create_tenant

from .session_models import Session


class SessionDeleteScopeRegressionTest(TestCase):
    """
    FA-0744: regression guard for the privilege-escalation fix on
    SessionDetailView.get_permissions() (apps/journal/session_views.py:198-203).

    A PAT with only sessions:read must be rejected (403) on DELETE.
    A PAT with sessions:write must succeed (204).
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="A22 Scope User",
            telegram_chat_id=9022,
        )
        self.user = self.tenant.user
        self.client = APIClient()
        self.session = Session.objects.create(
            tenant=self.tenant,
            source="audit/1.0.0",
            project="a22-test",
            session_start="2026-01-01T10:00:00Z",
            session_end="2026-01-01T11:00:00Z",
            summary="a22 regression fixture",
        )

    def _pat_with_scopes(self, scopes):
        raw, prefix, token_hash = generate_pat()
        PersonalAccessToken.objects.create(
            user=self.user,
            name=f"A22 PAT {scopes}",
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=scopes,
        )
        return raw

    def test_read_scope_cannot_delete(self):
        """sessions:read PAT must receive 403 on DELETE and leave the session intact."""
        raw = self._pat_with_scopes(["sessions:read"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = self.client.delete(f"/api/v1/sessions/{self.session.id}/")
        self.assertEqual(response.status_code, 403)
        # Session must still exist.
        self.assertTrue(Session.objects.filter(pk=self.session.pk).exists())

    def test_write_scope_can_delete(self):
        """sessions:write PAT must receive 204 on DELETE and remove the session."""
        raw = self._pat_with_scopes(["sessions:write"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = self.client.delete(f"/api/v1/sessions/{self.session.id}/")
        self.assertEqual(response.status_code, 204)
        # Session must be gone.
        self.assertFalse(Session.objects.filter(pk=self.session.pk).exists())
