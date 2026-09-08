from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib import admin
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.tenants.models import Tenant, User

from .admin import ContentReportAdmin
from .models import ContentReport


@override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class ContentReportAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.moderator = User.objects.create_superuser(username="moderator", email="moderator@example.com")
        reporter = User.objects.create_user(username="reporter", email="private-reporter@example.com")
        cls.tenant = Tenant.objects.create(user=reporter, status="active")
        cls.report = ContentReport.objects.create(
            reporter_tenant=cls.tenant,
            reporter_user=reporter,
            target_kind="general",
            reason="Private report details for the moderator",
        )

    def setUp(self):
        self.client.force_login(self.moderator)

    def test_registered_admin_lists_reporter_id_without_email(self):
        self.assertIsInstance(admin.site.get_model_admin(ContentReport), ContentReportAdmin)

        response = self.client.get(reverse("admin:friends_contentreport_changelist"))

        self.assertContains(response, str(self.tenant.pk))
        self.assertNotContains(response, "private-reporter@example.com")
        self.assertNotContains(response, self.report.reason)
        self.assertEqual(
            response.context["cl"].list_display,
            ["action_checkbox", "id", "target_kind", "status", "created_at", "resolved_at", "reporter_tenant_id"],
        )
        self.assertEqual(response.context["cl"].list_filter, ("status", "target_kind"))

    def test_change_page_shows_reason_and_only_status_is_editable(self):
        url = reverse("admin:friends_contentreport_change", args=[self.report.pk])

        response = self.client.get(url)

        self.assertContains(response, self.report.reason)
        self.assertEqual(list(response.context["adminform"].form.fields), ["status"])

        response = self.client.post(url, {"status": "dismissed", "reason": "tampered text", "_save": "Save"})

        self.assertEqual(response.status_code, 302)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, "dismissed")
        self.assertEqual(self.report.reason, "Private report details for the moderator")

    def test_actions_resolve_only_selected_reports(self):
        unselected = ContentReport.objects.create(
            reporter_tenant=self.tenant, target_kind="general", reason="Unselected report"
        )
        resolved_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
        for action, status in (("mark_hidden", "hidden"), ("mark_dismissed", "dismissed")):
            with self.subTest(action=action):
                ContentReport.objects.filter(pk=self.report.pk).update(status="open", resolved_at=None)
                with patch("apps.friends.admin.timezone.now", return_value=resolved_at):
                    response = self.client.post(
                        reverse("admin:friends_contentreport_changelist"),
                        {"action": action, "_selected_action": [str(self.report.pk)]},
                    )

                self.assertEqual(response.status_code, 302)
                self.report.refresh_from_db()
                self.assertEqual(self.report.status, status)
                self.assertEqual(self.report.resolved_at, resolved_at)
                unselected.refresh_from_db()
                self.assertEqual(unselected.status, "open")
                self.assertIsNone(unselected.resolved_at)
