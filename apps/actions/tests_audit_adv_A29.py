"""
Adversarial audit tests for cluster A29.

actions#1 — admin search_fields used tenant__owner__email which doesn't exist
on the Tenant model; the FK is `user`, so the correct path is tenant__user__email.
Django resolves search_fields lazily, so the changelist renders fine but a 500
is raised the instant any operator types in the search box.
"""

from django.contrib.admin.sites import AdminSite
from django.test import TestCase

from apps.actions.admin import ActionAuditLogAdmin, GatePreferenceAdmin, PendingActionAdmin
from apps.actions.models import ActionAuditLog, GatePreference, PendingAction


class ActionsAdminSearchFieldsTest(TestCase):
    """Ensure search_fields on every actions admin class use valid ORM paths."""

    def _invalid_paths(self, search_fields):
        return [f for f in search_fields if "owner" in f]

    def test_pending_action_admin_no_owner_path(self):
        admin_instance = PendingActionAdmin(PendingAction, AdminSite())
        bad = self._invalid_paths(admin_instance.search_fields)
        self.assertEqual(
            bad,
            [],
            f"PendingActionAdmin.search_fields contains invalid 'owner' path: {bad}",
        )

    def test_pending_action_admin_uses_user_email(self):
        admin_instance = PendingActionAdmin(PendingAction, AdminSite())
        self.assertIn(
            "tenant__user__email",
            admin_instance.search_fields,
            "PendingActionAdmin.search_fields must include tenant__user__email",
        )

    def test_gate_preference_admin_no_owner_path(self):
        admin_instance = GatePreferenceAdmin(GatePreference, AdminSite())
        bad = self._invalid_paths(admin_instance.search_fields)
        self.assertEqual(
            bad,
            [],
            f"GatePreferenceAdmin.search_fields contains invalid 'owner' path: {bad}",
        )

    def test_gate_preference_admin_uses_user_email(self):
        admin_instance = GatePreferenceAdmin(GatePreference, AdminSite())
        self.assertIn(
            "tenant__user__email",
            admin_instance.search_fields,
            "GatePreferenceAdmin.search_fields must include tenant__user__email",
        )

    def test_action_audit_log_admin_no_owner_path(self):
        admin_instance = ActionAuditLogAdmin(ActionAuditLog, AdminSite())
        bad = self._invalid_paths(admin_instance.search_fields)
        self.assertEqual(
            bad,
            [],
            f"ActionAuditLogAdmin.search_fields contains invalid 'owner' path: {bad}",
        )

    def test_action_audit_log_admin_uses_user_email(self):
        admin_instance = ActionAuditLogAdmin(ActionAuditLog, AdminSite())
        self.assertIn(
            "tenant__user__email",
            admin_instance.search_fields,
            "ActionAuditLogAdmin.search_fields must include tenant__user__email",
        )
