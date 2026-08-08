"""P3 W3b Sautai prompt ingress and deliberate partner-egress coverage."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import Integration, SautaiMealPlanJob
from .sautai_client import call_sautai_generate_plan


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


@override_settings(
    NBHD_INTERNAL_API_KEY="test-internal-key",
    SAUTAI_M2M_BASE_URL="https://app.sautai.test",
    SAUTAI_PLATFORM_SECRET="test-secret",
)
class SautaiPromptPlaceholderTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="W3b Sautai", telegram_chat_id=880316)
        self.tenant.sautai_enabled = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["sautai_enabled", "pii_entity_map"])
        seed_internal_key(self.tenant)
        Integration.objects.create(
            tenant=self.tenant,
            provider=Integration.Provider.SAUTAI,
            status=Integration.Status.ACTIVE,
            sautai_user_id=501,
        )
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _url(self):
        return f"/api/v1/integrations/runtime/{self.tenant.id}/sautai/generate-plan/"

    def _post(self, data):
        return self.client.post(self._url(), data, format="json", **self.headers)

    def _dispatch(self, data):
        preview = self._post(data)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["status"], "confirmation_required")
        confirmed = dict(preview.data["preview"]["tool_parameters"])
        confirmed["confirm_token"] = preview.data["confirm_token"]
        return preview, self._post(confirmed)

    def _enable_placeholder_writes(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

    def test_flag_off_confirmed_ingress_preserves_prompt_bytes(self):
        prompt = "Plan dinners Alice will enjoy"
        with patch("apps.cron.publish.publish_task"):
            _preview, response = self._dispatch({"week_start": "2026-08-10", "user_prompt": prompt})

        self.assertEqual(response.status_code, 201, response.data)
        job = SautaiMealPlanJob.objects.get(id=response.data["job_id"])
        self.assertEqual(job.user_prompt, prompt)
        self.assertEqual(job.pii_receipts["user_prompt"], {"state": "bypass", "writer": "runtime"})
        self.assertNotIn("pii_receipts", response.data)

    def test_flag_on_ingress_stays_placeholder_space_and_egress_rehydrates(self):
        self._enable_placeholder_writes()
        prompt = "Plan dinners [PERSON_1] will enjoy"
        with (
            _checked_detection(),
            patch("apps.cron.publish.publish_task"),
        ):
            preview, response = self._dispatch({"week_start": "2026-08-10", "user_prompt": prompt})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn("[PERSON_1]", preview.data["preview"]["request"]["user_prompt"])
        self.assertNotIn("Alice", preview.data["preview"]["request"]["user_prompt"])
        job = SautaiMealPlanJob.objects.get(id=response.data["job_id"])
        self.assertEqual(job.user_prompt, prompt)
        self.assertEqual(job.pii_receipts["user_prompt"]["writer"], "runtime")
        self.assertEqual(job.pii_receipts["user_prompt"]["redactions"], [{"placeholder": "[PERSON_1]"}])
        self.assertNotIn("pii_receipts", response.data)

        failed_response = SimpleNamespace(
            status_code=400,
            json=lambda: {"detail": "partner unavailable"},
            text="partner unavailable",
        )
        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=failed_response,
        ) as http_post:
            call_sautai_generate_plan(job)

        outbound_prompt = http_post.call_args.kwargs["json"]["user_prompt"]
        self.assertEqual(outbound_prompt, "Plan dinners Alice will enjoy")
        job.refresh_from_db()
        self.assertEqual(job.user_prompt, prompt)
