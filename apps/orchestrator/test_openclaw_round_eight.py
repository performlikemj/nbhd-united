"""Round-eight DB/HTTP fence, takeover, publication and numeric contracts."""

import copy
import json
import shutil
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.cron.gateway_client import invoke_gateway_tool
from apps.cron.models import CronJob
from apps.cron.share_cron_sync import write_tenant_crons_file
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator import test_openclaw_round_seven as round_seven
from apps.orchestrator.migration_cron_fence import cron_edits_fenced
from apps.orchestrator.migration_preservation import preservation_reasons, scalar_types_valid
from apps.orchestrator.migration_signed_file import publish_signed_file
from apps.orchestrator.test_openclaw_round_seven import fixture, inventory_result
from apps.orchestrator.test_tenant_openclaw_migration import TAG, job, tenant_fixture
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key


def fence(tenant, status="RUNNING"):
    Tenant.objects.filter(pk=tenant.pk).update(
        openclaw_migration={"status": status, "owner_token": "owner", "tag": TAG},
        openclaw_migration_cron_fenced=True,
    )
    tenant.refresh_from_db()


class FenceTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(949408)
        seed_internal_key(self.tenant, "shared-key")
        with suppress_cronjob_reconcile():
            self.row = CronJob.objects.create(tenant=self.tenant, name="reminder", data=job())
        fence(self.tenant)

    def test_background_database_and_transport_writes_are_not_fenced(self):
        self.tenant.refresh_from_db()
        with self.assertNumQueries(0):
            self.assertTrue(cron_edits_fenced(self.tenant))
        with suppress_cronjob_reconcile():
            CronJob.objects.filter(pk=self.row.pk).update(enabled=False)
        with patch("apps.cron.gateway_client.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"ok": True, "result": {}}
            with self.assertNumQueries(0):
                invoke_gateway_tool(self.tenant, "cron.remove", {"jobId": "original"})
            post.assert_called_once()
        with patch.object(m.azure_client, "_put_share_file") as put:
            write_tenant_crons_file(self.tenant)
            put.assert_called_once()

    def test_no_active_record_takes_original_transport_and_database_path(self):
        for record in ({}, {"status": "PASS"}):
            # Even a stale true flag cannot fence an absent/PASS migration.
            Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration=record)
            with suppress_cronjob_reconcile():
                CronJob.objects.filter(pk=self.row.pk).update(enabled=True)
            with patch("apps.cron.gateway_client.requests.post") as post:
                post.return_value.status_code = 200
                post.return_value.json.return_value = {"ok": True, "result": {"main": True}}
                self.assertEqual(invoke_gateway_tool(self.tenant, "cron.remove", {"jobId": "original"}), {"main": True})
                self.assertEqual(
                    post.call_args.kwargs["json"], {"tool": "cron", "action": "remove", "args": {"jobId": "original"}}
                )
            with (
                patch.object(m.azure_client, "_put_share_file") as put,
                patch("apps.orchestrator.migration_signed_file.publish_signed_file") as atomic,
            ):
                self.assertEqual(write_tenant_crons_file(self.tenant), 1)
                put.assert_called_once()
                atomic.assert_not_called()

    def test_version_commit_releases_fence_and_selects_file_transport(self):
        from apps.cron.share_cron_sync import tenant_uses_file_cron_sync

        self.tenant.refresh_from_db()
        m.version_step(self.tenant, self.tenant.openclaw_migration)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)
        self.assertTrue(tenant_uses_file_cron_sync(self.tenant))
        with suppress_cronjob_reconcile():
            CronJob.objects.filter(pk=self.row.pk).update(enabled=False)

    def test_dashboard_mutations_return_retryable_json(self):
        client = APIClient()
        client.force_authenticate(self.tenant.user)
        for method, path, data in (
            (
                "post",
                "/api/v1/cron-jobs/",
                {
                    "name": "new",
                    "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                    "payload": {"kind": "agentTurn", "message": "test"},
                },
            ),
            ("patch", "/api/v1/cron-jobs/reminder/", {"enabled": False}),
            ("delete", "/api/v1/cron-jobs/reminder/", {}),
            ("post", "/api/v1/cron-jobs/reminder/toggle/", {"enabled": False}),
            ("post", "/api/v1/cron-jobs/bulk-delete/", {"ids": ["reminder"]}),
        ):
            with self.subTest(method=method, path=path), transaction.atomic():
                response = getattr(client, method)(path, data, format="json")
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json(), {"error": "assistant_updating", "retry_after": 60})

    @override_settings(CORS_ALLOW_ALL_ORIGINS=False, CORS_ALLOWED_ORIGINS=["https://dashboard.example"])
    def test_retry_response_retains_browser_cors_and_security_headers(self):
        client = APIClient()
        client.force_authenticate(self.tenant.user)
        with transaction.atomic():
            response = client.delete("/api/v1/cron-jobs/reminder/", HTTP_ORIGIN="https://dashboard.example")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "assistant_updating", "retry_after": 60})
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://dashboard.example")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_typed_assistant_endpoint_has_friendly_retry_message(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.pk}/crons/pure_reminder/",
            data=json.dumps(
                {"name": "new reminder", "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"}, "text": "test"}
            ),
            content_type="application/json",
            HTTP_X_NBHD_INTERNAL_KEY="shared-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.pk),
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"], "assistant_updating")
        self.assertIn("one minute", response.json()["detail"])

    def test_unfenced_proposal_survives_notification_failure_as_on_main(self):
        from apps.actions.models import PendingAction
        from apps.cron.gate import request_cron_action

        for index, record in enumerate(({}, {"status": "PASS"})):
            Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration=record)
            with (
                patch("apps.actions.messaging.send_gate_confirmation", side_effect=RuntimeError("notification failed")),
                self.assertRaisesMessage(RuntimeError, "notification failed"),
            ):
                request_cron_action(
                    self.tenant,
                    cron_request_id=f"parity-{index}",
                    pattern="pure_reminder",
                    name=f"new reminder {index}",
                    schedule={"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
                    typed_payload={"text": "test"},
                    reason="test",
                    origin_stamp=SimpleNamespace(kind="user", cron_name="", run_id=""),
                )
            self.assertTrue(
                PendingAction.objects.filter(tenant=self.tenant, cron_request_id=f"parity-{index}").exists()
            )


class TakeoverTests(TestCase):
    def test_expiry_missing_or_future_lease_never_authorizes_takeover(self):
        for lease in (
            None,
            (timezone.now() - timedelta(days=2)).isoformat(),
            (timezone.now() + timedelta(minutes=10)).isoformat(),
        ):
            record = {"status": "RUNNING", "owner_token": "exact", "lease_until": lease}
            for token, confirmed in ((None, False), ("exact", False), (None, True), ("wrong", True)):
                with self.subTest(lease=lease, token=token, confirmed=confirmed), self.assertRaises(m.MigrationError):
                    m._check_lease(record, token, confirmed)
            m._check_lease(record, "exact", True)

    def test_schema_backfill_only_fences_active_prestaged_legacy_records(self):
        from importlib import import_module

        tenants = []
        for index, (record, version, expected) in enumerate(
            (
                ({"status": "RUNNING", "signed_prestaged": {}}, "2026.5.28", True),
                ({"status": "FAILED", "signed_prestaged": {}}, "2026.5.28", True),
                ({"status": "PASS", "signed_prestaged": {}}, "2026.5.28", False),
                ({"status": "RUNNING", "signed_prestaged": {}}, m.VERSION, False),
                ({"status": "RUNNING"}, "2026.5.28", False),
                ({}, "2026.5.28", False),
            )
        ):
            tenant = tenant_fixture(94940830 + index)
            Tenant.objects.filter(pk=tenant.pk).update(openclaw_migration=record, openclaw_version=version)
            tenants.append((tenant, expected))
        migration = import_module("apps.tenants.migrations.0170_openclaw_migration_cron_fence")
        with connection.cursor() as cursor:
            cursor.execute(migration.SQL.split("CREATE FUNCTION", 1)[0])
        for tenant, expected in tenants:
            tenant.refresh_from_db()
            self.assertEqual(tenant.openclaw_migration_cron_fenced, expected)

    def test_cli_requires_both_flags_and_report_shows_stuck_owner_without_io(self):
        tenant = tenant_fixture(9494082)
        Tenant.objects.filter(pk=tenant.pk).update(
            openclaw_migration={
                "status": "RUNNING",
                "owner_token": "exact-owner",
                "updated_at": (timezone.now() - timedelta(hours=1)).isoformat(),
                "lease_until": (timezone.now() - timedelta(minutes=50)).isoformat(),
            }
        )
        for flags in ({"takeover": "exact-owner"}, {"confirm_owner_dead": True}):
            with self.assertRaises(CommandError):
                call_command("migrate_tenant_openclaw", tenant=str(tenant.pk), tag=TAG, **flags)
        out = StringIO()
        with patch.object(m, "live_source_jobs") as live:
            call_command("migrate_tenant_openclaw", tenant=str(tenant.pk), report=True, stdout=out)
            live.assert_not_called()
        self.assertIn("RUNNING", out.getvalue())
        self.assertIn("owner_token=exact-owner", out.getvalue())
        self.assertIn("lease_age_seconds=3600", out.getvalue())
        self.assertIn("lease_expired=True", out.getvalue())


class AtomicPublishTests(SimpleTestCase):
    @override_settings(AZURE_MOCK=False, AZURE_STORAGE_ACCOUNT_NAME="localfake")
    def test_upload_or_readback_death_preserves_previous_target_and_rename_is_last(self):
        tenant = Mock(pk="12345678-1234-1234-1234-123456789abc")
        for failure in ("upload", "readback", None):
            files = {"nbhd-crons.json": b"last-valid"}
            temporary = []

            def client_factory(files=files, failure=failure, temporary=temporary, **kwargs):
                path = kwargs["file_path"]
                temporary.append(path)
                client = Mock()
                client.__enter__ = Mock(return_value=client)
                client.__exit__ = Mock(return_value=False)

                def upload(data, **kw):
                    files[path] = b"\0" * len(data)
                    if failure == "upload":
                        raise SystemExit("dead after allocation")
                    files[path] = data

                def read():
                    if failure == "readback":
                        raise SystemExit("dead before rename")
                    return files[path]

                client.upload_file.side_effect = upload
                client.download_file.return_value.readall.side_effect = read
                client.rename_file.side_effect = lambda target, **kw: files.update({target: files.pop(path)})
                return client

            with (
                patch.object(m.azure_client, "is_mock", return_value=False),
                patch.object(
                    m.azure_client,
                    "download_workspace_file_binary",
                    side_effect=lambda tid, path, files=files: files.get(path),
                ),
                patch("azure.storage.fileshare.ShareFileClient", side_effect=client_factory),
                patch("apps.orchestrator.storage_credentials.acquire_account_key"),
                patch(
                    "apps.orchestrator.storage_credentials.run_with_lease",
                    side_effect=lambda tid, lease, fn: fn("local-key"),
                ),
            ):
                if failure:
                    with self.assertRaises(SystemExit):
                        publish_signed_file(tenant, b"new-valid")
                    self.assertEqual(files["nbhd-crons.json"], b"last-valid")
                else:
                    self.assertTrue(publish_signed_file(tenant, b"new-valid"))
                    self.assertEqual(files["nbhd-crons.json"], b"new-valid")
                    self.assertFalse(publish_signed_file(tenant, b"new-valid"))
                    self.assertEqual(len(temporary), 1)
                self.assertTrue(temporary[0].startswith("nbhd-crons.json.migration-"))


@skipUnless(shutil.which("node"), "Node required in local contract gate")
class SafeIntegerTests(SimpleTestCase):
    def test_interval_python_js_boundaries(self):
        base = fixture("every")["declaration"]
        for value in (2**53 - 2, 2**53 - 1, 2**53, 2**53 + 1, 9007199254741000, 10**24, -(2**53), -(2**53 - 1)):
            declaration = copy.deepcopy(base)
            declaration["schedule"]["everyMs"] = value
            with self.subTest(value=value):
                self.assertEqual(scalar_types_valid(declaration), abs(value) <= 2**53 - 1)
                self.assertEqual(bool(preservation_reasons(declaration)), bool(inventory_result([declaration])))


class ImageBoundaryTests(TestCase):
    setUp = round_seven.PrestagingTests.setUp

    def test_actual_share_is_reverified_and_repaired_immediately_before_submission(self):
        app = SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image="old")], revision_suffix="old")
        )
        record = {
            "status": "RUNNING",
            "tag": TAG,
            "evidence": {"preflight": {"target_image": "target", "revision_suffix": "new"}},
        }
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration=record)
        real_save = m._save
        damaged = []

        def save(tenant, rec):
            real_save(tenant, rec)
            if rec.get("image_submitted") and not damaged:
                self.files["nbhd-crons.json"] = b"damaged-between-checkpoint-and-submit"
                damaged.append(True)

        def submit(*args, **kwargs):
            from apps.cron.share_cron_sync import build_signed_crons_doc

            self.assertTrue(damaged)
            self.assertEqual(self.files["nbhd-crons.json"], build_signed_crons_doc(self.tenant)[0])
            self.tenant.refresh_from_db()
            self.assertTrue(self.tenant.openclaw_migration_cron_fenced)

        with (
            patch.object(m.time, "sleep"),
            patch.object(m, "get_app", return_value=app),
            patch.object(m, "live_source_jobs", return_value=[job()]),
            patch.object(m, "_save", side_effect=save),
            patch.object(m.azure_client, "update_container_image", side_effect=submit) as update,
            patch.object(m, "wait_healthy", return_value={}),
        ):
            m.image_step(self.tenant, record)
            update.assert_called_once()

    def test_migration_post_version_reconcile_uses_atomic_publication(self):
        fence(self.tenant)
        self.tenant.refresh_from_db()
        m.version_step(self.tenant, self.tenant.openclaw_migration)
        with patch("apps.orchestrator.migration_signed_file.publish_signed_file", wraps=publish_signed_file) as publish:
            m.stage_signed_crons(self.tenant, self.tenant.openclaw_migration, checkpoint="signed_crons")
            publish.assert_called_once()

    def test_already_submitted_revision_is_fenced_before_recovery_health_check(self):
        app = SimpleNamespace(
            template=SimpleNamespace(
                containers=[SimpleNamespace(name="openclaw", image="target")], revision_suffix="saved"
            )
        )
        record = {
            "status": "RUNNING",
            "tag": TAG,
            "evidence": {"preflight": {"target_image": "target", "revision_suffix": "saved"}},
            "signed_prestaged": {"digest": "older-command"},
        }
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration=record)

        def health(*args, **kwargs):
            self.tenant.refresh_from_db()
            self.assertTrue(self.tenant.openclaw_migration_cron_fenced)
            with self.assertNumQueries(0):
                self.assertTrue(cron_edits_fenced(self.tenant))
            return {}

        with (
            patch.object(m.time, "sleep"),
            patch.object(m, "get_app", return_value=app),
            patch.object(m, "wait_healthy", side_effect=health),
            patch.object(m.azure_client, "update_container_image") as update,
        ):
            m.image_step(self.tenant, record)
            update.assert_not_called()
