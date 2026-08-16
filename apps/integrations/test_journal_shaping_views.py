"""Runtime endpoint tests for flag-gated default journal template shaping."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.models import NoteTemplate
from apps.journal.services import seed_default_templates_for_tenant
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class JournalShapingViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="JournalShape", telegram_chat_id=944001)
        seed_internal_key(self.tenant)
        self.other = create_tenant(display_name="OtherShape", telegram_chat_id=944002)

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _get_url(self):
        return f"/api/v1/integrations/runtime/{self.tenant.id}/journal/template/"

    def _update_url(self):
        return f"/api/v1/integrations/runtime/{self.tenant.id}/journal/template/update/"

    def _enable(self):
        self.tenant.journal_shaping_enabled = True
        self.tenant.save(update_fields=["journal_shaping_enabled"])

    def test_endpoints_require_internal_auth(self):
        requests = (
            ("missing get", lambda: self.client.get(self._get_url())),
            ("wrong get", lambda: self.client.get(self._get_url(), **self._headers(key="wrong-key"))),
            (
                "missing post",
                lambda: self.client.post(
                    self._update_url(),
                    data={"sections": []},
                    content_type="application/json",
                ),
            ),
            (
                "wrong post",
                lambda: self.client.post(
                    self._update_url(),
                    data={"sections": []},
                    content_type="application/json",
                    **self._headers(key="wrong-key"),
                ),
            ),
        )
        for label, request in requests:
            with self.subTest(label=label):
                self.assertEqual(request().status_code, 401)

    def test_endpoints_reject_tenant_scope_mismatch(self):
        headers = self._headers(tenant_id=str(self.other.id))

        get_response = self.client.get(self._get_url(), **headers)
        update_response = self.client.post(
            self._update_url(),
            data={"sections": []},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(get_response.status_code, 401)
        self.assertEqual(update_response.status_code, 401)

    def test_flag_off_returns_403_on_both_endpoints(self):
        get_response = self.client.get(self._get_url(), **self._headers())
        update_response = self.client.post(
            self._update_url(),
            data={"sections": []},
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(update_response.status_code, 403)

    def test_get_seeds_and_returns_default_template(self):
        self._enable()
        NoteTemplate.objects.filter(tenant=self.tenant).delete()

        response = self.client.get(self._get_url(), **self._headers())

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["name"], "Default")
        self.assertEqual(len(body["sections"]), 5)
        template = NoteTemplate.objects.get(tenant=self.tenant, is_default=True)
        self.assertEqual(body["sections"], template.sections)

    @patch("apps.cron.publish.publish_task")
    def test_post_replaces_default_sections_and_pushes_config(self, publish_task):
        self._enable()
        template = seed_default_templates_for_tenant(tenant=self.tenant)["template"]
        sections = [
            {"slug": "mood", "title": "Mood", "content": "-", "source": "human"},
            {"slug": "gratitude", "title": "Gratitude", "content": "", "source": "agent"},
        ]

        response = self.client.post(
            self._update_url(),
            data={"sections": sections},
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        template.refresh_from_db()
        self.assertEqual(template.sections, sections)
        self.assertEqual(response.json()["sections"], sections)
        publish_task.assert_called_once_with("update_tenant_config", str(self.tenant.id))

    @patch("apps.cron.publish.publish_task")
    def test_placeholder_writes_off_preserves_section_bytes_and_runtime_omits_receipts(self, _publish_task):
        self._enable()
        template = seed_default_templates_for_tenant(tenant=self.tenant)["template"]
        sections = [
            {
                "slug": "alice-focus",
                "title": "Alice focus",
                "content": "Plan the launch with Alice",
                "source": "human",
            }
        ]

        updated = self.client.post(
            self._update_url(),
            data={"sections": sections},
            content_type="application/json",
            **self._headers(),
        )
        runtime_read = self.client.get(self._get_url(), **self._headers())

        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(runtime_read.status_code, 200, runtime_read.content)
        template.refresh_from_db()
        self.assertEqual(template.sections, sections)
        self.assertEqual(template.pii_receipts["sections"], {"state": "bypass", "writer": "runtime"})
        self.assertEqual(updated.json()["sections"], sections)
        self.assertEqual(runtime_read.json()["sections"], sections)
        self.assertNotIn("pii_receipts", updated.json())
        self.assertNotIn("pii_receipts", runtime_read.json())

    @patch("apps.cron.publish.publish_task")
    def test_placeholder_writes_on_pins_runtime_writer_and_runtime_stays_placeholder_space(self, _publish_task):
        self._enable()
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        template = seed_default_templates_for_tenant(tenant=self.tenant)["template"]
        sections = [
            {
                "slug": "alice-focus",
                "title": "Alice focus",
                "content": "Plan the launch with Alice",
                "source": "human",
            }
        ]
        placeholder_sections = [
            {
                "slug": "alice-focus",
                "title": "[PERSON_1] focus",
                "content": "Plan the launch with [PERSON_1]",
                "source": "human",
            }
        ]

        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring._detect_pii", return_value=[]),
        ):
            updated = self.client.post(
                self._update_url(),
                data={"sections": sections},
                content_type="application/json",
                **self._headers(),
            )
        runtime_read = self.client.get(self._get_url(), **self._headers())

        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(runtime_read.status_code, 200, runtime_read.content)
        template.refresh_from_db()
        self.assertEqual(template.sections, placeholder_sections)
        self.assertEqual(template.pii_receipts["sections"]["state"], "unconfirmed")
        self.assertEqual(template.pii_receipts["sections"]["reason"], "detector-deferred")
        self.assertEqual(template.pii_receipts["sections"]["writer"], "runtime")
        self.assertEqual(template.pii_receipts["sections"]["redactions"], [{"placeholder": "[PERSON_1]"}])
        self.assertEqual(updated.json()["sections"], placeholder_sections)
        self.assertEqual(runtime_read.json()["sections"], placeholder_sections)
        self.assertNotIn("pii_receipts", updated.json())
        self.assertNotIn("pii_receipts", runtime_read.json())

    @patch("apps.cron.publish.publish_task")
    def test_post_rejections_leave_template_unchanged_and_do_not_publish(self, publish_task):
        self._enable()
        template = seed_default_templates_for_tenant(tenant=self.tenant)["template"]
        original_sections = template.sections
        valid = {"slug": "mood", "title": "Mood", "content": "", "source": "human"}
        cases = (
            ("empty", [], "cannot be empty"),
            ("duplicate slugs", [valid, valid], "duplicate section slug"),
            (
                "too many sections",
                [{**valid, "slug": f"mood-{index}"} for index in range(13)],
                "at most 12",
            ),
            ("oversize slug", [{**valid, "slug": "s" * 65}], "slug must be at most 64"),
            ("oversize title", [{**valid, "title": "T" * 121}], "title must be at most 120"),
            ("oversize content", [{**valid, "content": "C" * 4001}], "content must be at most 4000"),
            (
                "oversize payload",
                [{**valid, "slug": f"section-{index}", "content": "C" * 3900} for index in range(6)],
                "payload must be at most 20KB",
            ),
            ("non-list", {"slug": "mood"}, "must be an array"),
        )

        for label, sections, message in cases:
            with self.subTest(label=label):
                response = self.client.post(
                    self._update_url(),
                    data={"sections": sections},
                    content_type="application/json",
                    **self._headers(),
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(message, response.json()["detail"])
                template.refresh_from_db()
                self.assertEqual(template.sections, original_sections)

        publish_task.assert_not_called()
