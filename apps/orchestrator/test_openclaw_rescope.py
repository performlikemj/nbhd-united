"""Rescope boundaries: canonical wake, manual gates and cancellation audit."""

import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.test_tenant_openclaw_migration import TAG, job, record, tenant_fixture


@override_settings(DEPLOY_SECRET="offline", OPENCLAW_IMAGE_TAG=TAG)
class RescopeTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(949403)
        env = patch.dict(os.environ, {"AZURE_MOCK": "true"})
        env.start()
        self.addCleanup(env.stop)

    def row(self, declaration, **kwargs):
        with suppress_cronjob_reconcile():
            return CronJob.objects.create(tenant=self.tenant, name=declaration["name"], data=declaration, **kwargs)

    def test_recurring_jobs_never_defer_or_pause_even_running(self):
        source = job(schedule={"kind": "every", "everyMs": 60000}, state={"runningAtMs": 100})
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [source]}) as rpc:
            m.capture(self.tenant, record())
        self.assertEqual([c.args[1] for c in rpc.call_args_list], ["cron.list"])

    def test_one_shot_cancelled_only_when_absent_from_both_authorities(self):
        for minutes in (-10, 60):
            source = job(schedule={"kind": "at", "at": (timezone.now() + timedelta(minutes=minutes)).isoformat()})
            rec = record() | {"cron_export": [source]}
            with patch.object(m.runtime_operator, "list_crons", return_value=[]):
                m.one_shot_dispositions(self.tenant, rec)
            audit = rec["one_shot_dispositions"][0]
            self.assertEqual(audit["disposition"], "cancelled")
            self.assertTrue(audit["observed_at"])
            row = self.row(source)
            with patch.object(m.runtime_operator, "list_crons", return_value=[]), self.assertRaises(m.MigrationError):
                m.one_shot_dispositions(self.tenant, rec)
            with suppress_cronjob_reconcile():
                row.delete()
            # A runtime job recreated with a different ID but same name is not absent.
            with patch.object(m.runtime_operator, "list_crons", return_value=[source | {"id": "replacement"}]):
                if minutes < 0:
                    with self.assertRaises(m.MigrationError):
                        m.one_shot_dispositions(self.tenant, rec)
                else:
                    m.one_shot_dispositions(self.tenant, rec)
                    self.assertEqual(rec["one_shot_dispositions"][0]["disposition"], "pending_runtime")

    def test_atomic_endpoint_rejects_missing_malformed_and_unknown_scope_without_publication(self):
        bodies = [
            "",
            "{",
            "null",
            "[]",
            "{}",
            '{"tenant_id":""}',
            '{"tenant_id":42}',
            '{"tenant_id":"bad"}',
            json.dumps({"tenant_id": "00000000-0000-0000-0000-000000000000"}),
        ]
        with patch("apps.cron.publish.publish_batch") as publish:
            for body in bodies:
                response = self.client.post(
                    "/api/cron/rollout-atomic-bump/",
                    data=body,
                    content_type="application/json",
                    HTTP_X_DEPLOY_SECRET="offline",
                )
                self.assertEqual(response.status_code, 400, body)
        publish.assert_not_called()

    def test_all_manual_entrypoints_refuse_legacy_and_ambiguous_source_before_mutation(self):
        for source, version in (("2026.5.28-abcdef0", "2026.5.28"), ("abcdef0", m.VERSION)):
            self.tenant.container_image_tag, self.tenant.openclaw_version = source, version
            self.tenant.save()
            with (
                patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image") as image,
                patch(
                    "apps.orchestrator.management.commands.bump_openclaw_version.bump_openclaw_version_for_tenant"
                ) as bump,
                patch("apps.cron.publish.publish_batch") as publish,
            ):
                for command, args in (
                    ("bump_all_tenant_images", ["--tenant", str(self.tenant.pk), "--tag", TAG]),
                    (
                        "bump_openclaw_version",
                        ["--tenant", str(self.tenant.pk), "--all", "--oc-version", m.VERSION, "--image-tag", TAG],
                    ),
                ):
                    with self.assertRaisesRegex(CommandError, "migrate_tenant_openclaw"):
                        call_command(command, *args)
                response = self.client.post(
                    "/api/cron/rollout-atomic-bump/",
                    data=json.dumps({"tenant_id": str(self.tenant.pk)}),
                    content_type="application/json",
                    HTTP_X_DEPLOY_SECRET="offline",
                )
                self.assertEqual(response.status_code, 400)
                response = self.client.post(
                    "/api/cron/rollout-byo-image-bump/",
                    data="{}",
                    content_type="application/json",
                    HTTP_X_DEPLOY_SECRET="offline",
                )
                self.assertEqual(response.status_code, 400)
            image.assert_not_called()
            bump.assert_not_called()
            publish.assert_not_called()
