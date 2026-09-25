"""Round 4 regression contracts; no live services or tenant calls."""

import copy
import hashlib
import json
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.test_tenant_openclaw_migration import TAG, job, record, tenant_fixture


class AutomaticPathParityTests(SimpleTestCase):
    def test_pinned_main_blobs_including_entire_runtime_tree(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((Path(__file__).parent / "fixtures/automatic_paths_main.json").read_text())
        self.assertEqual(
            {str(p.relative_to(root)) for p in (root / "runtime/openclaw").rglob("*") if p.is_file()},
            {p for p in manifest["files"] if p.startswith("runtime/")},
        )
        for path, digest in manifest["files"].items():
            with self.subTest(path=path):
                content = (root / path).read_bytes()
                if path == "apps/cron/share_cron_sync.py":
                    # R8 permits the shared active-migration fence wrapper;
                    # the original selector/signer/writer body remains main.
                    content = content.replace(
                        b"from apps.orchestrator.migration_cron_fence import guard_transport\n\n", b""
                    ).replace(b"@guard_transport\n", b"")
                self.assertEqual(hashlib.sha256(content).hexdigest(), digest)


@override_settings(DEPLOY_SECRET="offline", OPENCLAW_IMAGE_TAG=TAG)
class RoundFourTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(949404)

    def row(self, declaration, **kwargs):
        with suppress_cronjob_reconcile():
            return CronJob.objects.create(
                tenant=self.tenant, name=declaration["name"], data=declaration, managed=True, **kwargs
            )

    def test_manual_commands_require_scope_and_single_tenant_family_guard(self):
        with (
            patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image") as image,
            patch(
                "apps.orchestrator.management.commands.bump_openclaw_version.bump_openclaw_version_for_tenant"
            ) as bump,
        ):
            for command, kwargs in [
                ("bump_all_tenant_images", {"tag": TAG}),
                ("bump_openclaw_version", {"all": True, "oc_version": m.VERSION, "image_tag": TAG}),
                ("bump_openclaw_version", {"tenant": str(self.tenant.pk), "oc_version": m.VERSION, "image_tag": TAG}),
            ]:
                with self.subTest(command=command, kwargs=kwargs), self.assertRaises(CommandError):
                    call_command(command, stdout=StringIO(), **kwargs)
            image.assert_not_called()
            bump.assert_not_called()

    def test_byo_bad_scope_never_invokes_command(self):
        with patch("django.core.management.call_command") as command:
            for body in [
                "",
                "{",
                "null",
                "[]",
                "{}",
                '{"tenant_ids":[]}',
                '{"tenant_ids":"bad"}',
                '{"tenant_ids":[null]}',
                '{"tenant_id":""}',
            ]:
                with self.subTest(body=body):
                    response = self.client.post(
                        "/api/cron/rollout-byo-image-bump/",
                        data=body,
                        content_type="application/json",
                        HTTP_X_DEPLOY_SECRET="offline",
                    )
                    self.assertEqual(response.status_code, 400)
            command.assert_not_called()

    def test_preservation_precheck_blocks_before_any_capture_write(self):
        cases = [
            job(delivery={"mode": "announce", "to": "private"}),
            job(schedule={"kind": "every", "everyMs": 60000, "anchorMs": 123}),
            job(schedule={"kind": "cron", "expr": "0 0 9 * * *"}),
            job(payload={"kind": "agentTurn", "message": "private", "thinking": "high"}),
            job(sessionTarget="current"),
            job("agent", delivery={"mode": "webhook", "to": "https://private.invalid"}),
        ]
        for declaration in cases:
            with (
                self.subTest(declaration=declaration["schedule"]),
                patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [declaration]}),
                patch.object(m, "_save") as save,
                self.assertRaisesRegex(m.MigrationError, "BLOCKED_UNSUPPORTED"),
            ):
                m.capture(self.tenant, record())
            save.assert_not_called()
            self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())

    def test_canonical_imminent_guard_precedes_filter_and_import(self):
        self.row(job(schedule={"kind": "at", "at": (timezone.now() + timedelta(minutes=10)).isoformat()}))
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
            patch.object(m, "_save") as save,
            self.assertRaisesRegex(m.MigrationError, "cron_imminent"),
        ):
            m.capture(self.tenant, record())
        save.assert_not_called()

    def test_canonical_only_expired_one_shot_is_audited(self):
        self.row(job(schedule={"kind": "at", "at": (timezone.now() - timedelta(minutes=1)).isoformat()}))
        with (
            patch.object(m.runtime_operator, "list_crons", return_value=[]),
            self.assertRaisesRegex(m.MigrationError, "one_shot_expired_undelivered"),
        ):
            m.one_shot_dispositions(self.tenant, record())

    def test_historical_cancellation_cannot_hide_current_canonical_row(self):
        declaration = job(schedule={"kind": "at", "at": (timezone.now() - timedelta(minutes=1)).isoformat()})
        self.row(declaration)
        rec = record() | {
            "cron_export": [declaration],
            "source_resolutions": {declaration["id"]: {"disposition": "cancelled_at_source"}},
        }
        with (
            patch.object(m.runtime_operator, "list_crons", return_value=[]),
            self.assertRaisesRegex(m.MigrationError, "one_shot_expired_undelivered"),
        ):
            m.one_shot_dispositions(self.tenant, rec)

    def test_report_is_read_only_and_checks_canonical_content(self):
        self.row(job(delivery={"mode": "announce", "accountId": "private"}))
        out = StringIO()
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
            patch.object(m, "_save") as save,
        ):
            call_command("migrate_tenant_openclaw", tenant=str(self.tenant.pk), report=True, stdout=out)
        self.assertIn("BLOCKED_UNSUPPORTED", out.getvalue())
        self.assertNotIn("private", out.getvalue())
        save.assert_not_called()

    def test_verify_compares_current_canonical_digests_and_retries_change_once(self):
        row = self.row(job())
        rec = record() | {"evidence": {"preflight": {"target_image": "image"}}}
        calls = []

        def inspect(tenant, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            if len(calls) == 2:
                CronJob.objects.filter(pk=row.pk).update(data=job(payload={"kind": "agentTurn", "message": "edited"}))
            return {
                "expected": 1,
                "matches": [{"key": f"nbhd:{row.pk}", "id": "runtime-id", "match": True}],
                "extras": [],
                "legacy": [],
            }

        with (
            patch.object(m.runtime_operator, "inspect_signed_crons", side_effect=inspect),
            patch.object(m, "wait_healthy", return_value={}),
            patch.object(m.runtime_operator, "console_error_counts", return_value={"errors": {}}),
            patch.object(m.time, "sleep"),
        ):
            self.assertEqual(m.verify(self.tenant, rec)["result"], "PASS")
        self.assertEqual(len(calls), 4)
        self.assertNotEqual(calls[0]["canonical_digests"], calls[2]["canonical_digests"])
        self.assertNotIn("edited", json.dumps(calls))

    def test_repeated_canonical_changes_fail_with_fixed_code(self):
        row = self.row(job())
        rec = record() | {"evidence": {"preflight": {"target_image": "image"}}}
        counter = 0

        def inspect(*args, **kwargs):
            nonlocal counter
            counter += 1
            CronJob.objects.filter(pk=row.pk).update(data=job(payload={"kind": "agentTurn", "message": str(counter)}))
            return {
                "expected": 1,
                "matches": [{"key": f"nbhd:{row.pk}", "id": "runtime", "match": True}],
                "extras": [],
                "legacy": [],
            }

        with (
            patch.object(m.runtime_operator, "inspect_signed_crons", side_effect=inspect),
            patch.object(m, "wait_healthy", return_value={}),
            patch.object(m.runtime_operator, "console_error_counts", return_value={"errors": {}}),
            patch.object(m.time, "sleep"),
            self.assertRaisesRegex(m.MigrationError, "^canonical_changed_during_verification$"),
        ):
            m.verify(self.tenant, rec)
        self.assertEqual(counter, 4)

    def test_actual_migration_blocked_and_deferred_without_any_writes(self):
        for declaration, status in [
            (job(delivery={"to": "private"}), "BLOCKED_UNSUPPORTED"),
            (job(schedule={"kind": "at", "at": (timezone.now() + timedelta(minutes=10)).isoformat()}), "DEFER"),
        ]:
            before = type(self.tenant).objects.filter(pk=self.tenant.pk).values().get()
            with (
                patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [declaration]}),
                patch.object(m, "_save") as save,
                patch.object(m.azure_client, "update_container_image") as image,
            ):
                self.assertEqual(m.migrate_tenant(self.tenant.pk, TAG)["status"], status)
            save.assert_not_called()
            image.assert_not_called()
            self.assertEqual(type(self.tenant).objects.filter(pk=self.tenant.pk).values().get(), before)
            self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())

    def test_report_all_outcomes_and_verify_only_94_preservation(self):
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [job()]}):
            self.assertEqual(m.report_tenant(self.tenant)["status"], "READY")
        self.tenant.openclaw_version = m.VERSION
        self.tenant.container_image_tag = TAG
        self.tenant.save()
        self.assertEqual(m.report_tenant(self.tenant)["status"], "ALREADY_94")
        out = StringIO()
        before = type(self.tenant).objects.filter(pk=self.tenant.pk).values().get()
        with (
            patch.object(
                m.runtime_operator,
                "preservation_inventory",
                return_value=[{"key": "runtime:0123456789abcdef", "reasons": ["unproven_shape"]}],
            ),
            self.assertRaisesRegex(CommandError, "BLOCKED_UNSUPPORTED"),
        ):
            call_command("migrate_tenant_openclaw", tenant=str(self.tenant.pk), verify_only=True, stdout=out)
        self.assertIn("unproven_shape", out.getvalue())
        self.assertIn("runtime:0123456789abcdef", out.getvalue())
        self.assertNotIn("private", out.getvalue())
        self.assertEqual(type(self.tenant).objects.filter(pk=self.tenant.pk).values().get(), before)

    def test_scoped_same_family_does_not_select_other_tenants(self):
        other = tenant_fixture(949405)
        with (
            patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image") as image,
            patch("apps.orchestrator.azure_client.get_container_client") as azure,
        ):
            from types import SimpleNamespace

            azure.return_value.container_apps.get.return_value = SimpleNamespace(
                template=SimpleNamespace(
                    containers=[SimpleNamespace(name="openclaw", image="registry/nbhd-openclaw:2026.5.28-old")]
                )
            )
            call_command("bump_all_tenant_images", tenant=str(self.tenant.pk), tag="2026.5.28-new", stdout=StringIO())
        image.assert_called_once()
        other.refresh_from_db()
        self.assertEqual(other.container_image_tag, "2026.5.28-abcdef0")

    def test_scope_list_validated_before_any_selected_tenant_is_mutated(self):
        other = tenant_fixture(949406)
        other.openclaw_version, other.container_image_tag = m.VERSION, TAG
        other.save()
        with (
            patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image") as image,
            patch(
                "apps.orchestrator.management.commands.bump_openclaw_version.bump_openclaw_version_for_tenant"
            ) as bump,
            patch("apps.cron.publish.publish_batch") as publish,
        ):
            ids = [str(other.pk), str(self.tenant.pk)]
            for command, options in [
                ("bump_all_tenant_images", {"tag": TAG}),
                ("bump_openclaw_version", {"all": True, "oc_version": m.VERSION, "image_tag": TAG}),
            ]:
                with self.assertRaises(CommandError):
                    call_command(command, tenants=",".join(ids), stdout=StringIO(), **options)
            for endpoint in ["rollout-byo-image-bump", "rollout-atomic-bump"]:
                response = self.client.post(
                    "/api/cron/" + endpoint + "/",
                    data=json.dumps({"tenant_ids": ids}),
                    content_type="application/json",
                    HTTP_X_DEPLOY_SECRET="offline",
                )
                self.assertEqual(response.status_code, 400)
            image.assert_not_called()
            bump.assert_not_called()
            publish.assert_not_called()

    def test_audit_failure_during_canonical_change_also_retries(self):
        row = self.row(job())
        rec = record() | {"evidence": {"preflight": {"target_image": "image"}}}
        calls = 0

        def audit(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                CronJob.objects.filter(pk=row.pk).update(data=job(payload={"kind": "agentTurn", "message": "edit"}))
                raise m.MigrationError("one_shot_pending_missing")

        inspection = {
            "expected": 1,
            "matches": [{"key": f"nbhd:{row.pk}", "id": "runtime", "match": True}],
            "extras": [],
            "legacy": [],
        }
        with (
            patch.object(m, "one_shot_dispositions", side_effect=audit),
            patch.object(m.runtime_operator, "inspect_signed_crons", return_value=inspection),
            patch.object(m, "wait_healthy", return_value={}),
            patch.object(m.runtime_operator, "console_error_counts", return_value={"errors": {}}),
            patch.object(m.time, "sleep"),
        ):
            self.assertEqual(m.verify(self.tenant, rec)["result"], "PASS")
        self.assertEqual(calls, 4)
