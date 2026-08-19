"""Runtime endpoint tests for typed cron creation.

Three endpoints, one per agent-creatable pattern (pure_reminder,
quote_user_intent, domain_summary). Fire-time enforcement (pattern_context /
validate_outbound / grounding) is no longer an RPC surface — see
apps/cron/tests/test_typed_cron_services.py::TypedCronContractBakingTests
and apps/cron/patterns/tests for the baked-contract + parity coverage.

Auth pattern matches existing runtime endpoints: HTTP_X_NBHD_INTERNAL_KEY +
HTTP_X_NBHD_TENANT_ID headers; tenant scope must match the URL tenant_id.
"""

from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone

from apps.cron.models import CronCreationPath, CronJob
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
        self.assertNotIn("nbhd_datebook_read", row.data["payload"]["toolsAllow"])

    def test_create_quote_user_intent_arbitrates_ready_tenant_to_datebook(self):
        self.tenant.datebook_manifest_ok = True
        self.tenant.datebook_enabled = True
        self.tenant.datebook_events_consent_at = timezone.now()
        self.tenant.save(
            update_fields=[
                "datebook_manifest_ok",
                "datebook_enabled",
                "datebook_events_consent_at",
            ]
        )
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/quote_user_intent/",
            data={
                "name": "datebook-appt",
                "schedule": {"kind": "cron", "expr": "0 9 * * 5", "tz": "Asia/Tokyo"},
                "text": "cardiologist appointment Tuesday 3pm",
                "refresh_facts_via": "nbhd_calendar_list_events",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        row = CronJob.objects.get(tenant=self.tenant, name="datebook-appt")
        self.assertIn("nbhd_datebook_read", row.data["payload"]["toolsAllow"])
        self.assertNotIn("nbhd_calendar_list_events", row.data["payload"]["toolsAllow"])
        self.assertIn("nbhd_datebook_read", row.data["payload"]["message"])
        self.assertNotIn("nbhd_calendar_list_events", row.data["payload"]["message"])

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

    # ── schedule shapes the gateway would accept and mis-execute ──────────
    #
    # These assert the MODEL-FACING body, not just the status: the `detail`
    # string is the only guidance the agent gets at the moment it has to
    # correct itself, so its wording is part of the contract.

    def _post_reminder(self, schedule, name="x"):
        return self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/crons/pure_reminder/",
            data={"name": name, "schedule": schedule, "text": "Take out trash"},
            content_type="application/json",
            **self._headers(),
        )

    def test_every_ms_in_seconds_is_rejected_with_the_unit_spelled_out(self):
        resp = self._post_reminder({"kind": "every", "everyMs": 3600})

        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body["error"], "everyms_too_small")
        self.assertIn("MILLISECONDS", body["detail"])
        self.assertIn("3600000", body["detail"])

    def test_offsetless_at_is_rejected_with_a_worked_example(self):
        resp = self._post_reminder({"kind": "at", "at": "2099-06-18T09:00:00"})

        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body["error"], "naive_at_rejected")
        self.assertIn("+09:00", body["detail"])

    def test_etc_timezone_is_rejected_for_inverting_the_sign(self):
        resp = self._post_reminder({"kind": "cron", "expr": "0 8 * * 2", "tz": "Etc/GMT+9"})

        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body["error"], "tz_etc_rejected")
        self.assertIn("INVERTED", body["detail"])

    def test_omitted_cron_tz_is_backfilled_from_the_users_profile(self):
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])

        resp = self._post_reminder({"kind": "cron", "expr": "0 7 * * *"}, name="morning")

        self.assertEqual(resp.status_code, 201, resp.content)
        # The echoed schedule is what the agent will read back to the user, so
        # it must show the tz that was actually stored.
        self.assertEqual(resp.json()["cron"]["schedule"]["tz"], "Asia/Tokyo")
        row = CronJob.objects.get(tenant=self.tenant, name="morning")
        self.assertEqual(row.data["schedule"]["tz"], "Asia/Tokyo")
