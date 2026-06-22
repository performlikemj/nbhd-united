"""Runtime endpoint tests for typed cron creation + pattern context + outbound validation.

Three endpoints per agent-creatable pattern (pure_reminder, quote_user_intent,
domain_summary) plus two enforcement-plugin-facing endpoints
(pattern_context, validate_outbound).

Auth pattern matches existing runtime endpoints: HTTP_X_NBHD_INTERNAL_KEY +
HTTP_X_NBHD_TENANT_ID headers; tenant scope must match the URL tenant_id.
"""

from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

from apps.cron.models import CronCreationPath, CronJob
from apps.cron.services import create_typed_cron
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeCronCreateViewsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="CronCreate", telegram_chat_id=818181)
        seed_internal_key(self.tenant)
        self.other_tenant = create_tenant(display_name="Other", telegram_chat_id=828282)

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    # ── auth ──────────────────────────────────────────────────────────────

    def test_create_requires_internal_auth(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/pure_reminder/",
            data={"name": "x", "schedule": {"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"}, "text": "x"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_create_rejects_tenant_scope_mismatch(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/pure_reminder/",
            data={"name": "x", "schedule": {"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"}, "text": "x"},
            content_type="application/json",
            **self._headers(tenant_id=str(self.other_tenant.id)),
        )
        self.assertEqual(resp.status_code, 401)

    # ── pure_reminder ─────────────────────────────────────────────────────

    def test_create_pure_reminder_succeeds(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/pure_reminder/",
            data={
                "name": "trash",
                "schedule": {"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"},
                "text": "Take out trash",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["cron"]["pattern"], "pure_reminder")
        self.assertEqual(body["cron"]["name"], "trash")

        row = CronJob.objects.get(tenant=self.tenant, name="trash")
        self.assertEqual(row.creation_path, CronCreationPath.TYPED)
        self.assertIn("Take out trash", row.data["payload"]["message"])
        self.assertEqual(row.data["payload"]["toolsAllow"], ["nbhd_send_to_user"])

    def test_create_pure_reminder_rejects_empty_text(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/pure_reminder/",
            data={
                "name": "x",
                "schedule": {"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"},
                "text": "",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_create_pure_reminder_name_conflict_returns_409(self):
        self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/pure_reminder/",
            data={
                "name": "dup",
                "schedule": {"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"},
                "text": "x",
            },
            content_type="application/json",
            **self._headers(),
        )
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/pure_reminder/",
            data={
                "name": "dup",
                "schedule": {"kind": "cron", "expr": "0 9 * * 2", "tz": "Asia/Tokyo"},
                "text": "y",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "name_conflict")

    # ── quote_user_intent ─────────────────────────────────────────────────

    def test_create_quote_user_intent_with_refresh(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/quote_user_intent/",
            data={
                "name": "appt",
                "schedule": {"kind": "cron", "expr": "0 9 * * 5", "tz": "Asia/Tokyo"},
                "text": "cardiologist appointment Tuesday 3pm",
                "refresh_facts_via": "nbhd_calendar_list_events",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        row = CronJob.objects.get(tenant=self.tenant, name="appt")
        self.assertIn("nbhd_calendar_list_events", row.data["payload"]["toolsAllow"])

    def test_create_quote_user_intent_rejects_mutation_refresh(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/quote_user_intent/",
            data={
                "name": "x",
                "schedule": {"kind": "cron", "expr": "0 9 * * 5", "tz": "Asia/Tokyo"},
                "text": "x",
                "refresh_facts_via": "nbhd_task_create",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    # ── domain_summary ────────────────────────────────────────────────────

    def test_create_domain_summary_succeeds(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/domain_summary/",
            data={
                "name": "weekly-tasks",
                "schedule": {"kind": "cron", "expr": "0 8 * * 0", "tz": "Asia/Tokyo"},
                "query_tool": "nbhd_task_list",
                "query_args": {"status": "open"},
                "render_block": "task_summary",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        row = CronJob.objects.get(tenant=self.tenant, name="weekly-tasks")
        self.assertIn("nbhd_task_list", row.data["payload"]["toolsAllow"])
        # Mutation tools must not have leaked in:
        self.assertNotIn("nbhd_task_create", row.data["payload"]["toolsAllow"])

    def test_create_domain_summary_rejects_block_mismatch(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/domain_summary/",
            data={
                "name": "x",
                "schedule": {"kind": "cron", "expr": "0 8 * * 0", "tz": "Asia/Tokyo"},
                "query_tool": "nbhd_task_list",
                "render_block": "goal_summary",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    # ── pattern_context + validate_outbound (enforcement plugin paths) ───

    def test_pattern_context_returns_typed_payload(self):
        create_typed_cron(
            tenant=self.tenant,
            pattern="pure_reminder",
            typed_payload={"text": "Drink water"},
            name="hydrate",
            schedule={"kind": "cron", "expr": "0 10 * * *", "tz": "Asia/Tokyo"},
        )
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/hydrate/pattern_context/",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["pattern"], "pure_reminder")
        self.assertEqual(body["typed_payload"]["text"], "Drink water")
        self.assertIn("pure_reminder", body["prompt_injection"])

    def test_pattern_context_404_for_unknown_cron(self):
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/no-such/pattern_context/",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 404)

    def test_validate_outbound_pass_and_fail(self):
        create_typed_cron(
            tenant=self.tenant,
            pattern="pure_reminder",
            typed_payload={"text": "Drink water"},
            name="hydrate",
            schedule={"kind": "cron", "expr": "0 10 * * *", "tz": "Asia/Tokyo"},
        )
        pass_resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/hydrate/validate_outbound/",
            data={"content": "Drink water"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(pass_resp.status_code, 200)
        self.assertTrue(pass_resp.json()["ok"])

        fail_resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/hydrate/validate_outbound/",
            data={"content": "You should stay hydrated"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(fail_resp.status_code, 200)
        body = fail_resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("fallback_content", body)
