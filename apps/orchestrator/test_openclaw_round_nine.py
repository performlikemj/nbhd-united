"""Lightweight request fences, main-path parity, and cutover drain contracts."""

import ast
import hashlib
import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.actions.models import ActionStatus, ActionType, PendingAction
from apps.actions.views import GateRespondView
from apps.cron import pending_at_views, tenant_views
from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.integrations import runtime_views
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.migration_cron_fence import cron_edits_fenced
from apps.orchestrator.test_openclaw_round_seven import PrestagingTests
from apps.orchestrator.test_tenant_openclaw_migration import TAG, job, tenant_fixture
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key


class WithoutFence(ast.NodeTransformer):
    """Remove only the request checks/imports to pin remaining code to main."""

    def visit_ImportFrom(self, node):
        if node.module == "apps.orchestrator.migration_cron_fence":
            return None
        return node

    def visit_If(self, node):
        if any(isinstance(n, ast.Name) and n.id == "cron_edits_fenced" for n in ast.walk(node.test)):
            return None
        return self.generic_visit(node)


class MainRequestPathParityTests(SimpleTestCase):
    def test_request_implementations_equal_main_except_explicit_fence(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((Path(__file__).parent / "fixtures/cron_request_paths_main.json").read_text())
        for path, digest in manifest.items():
            with self.subTest(path=path):
                source_path, _, function = path.partition("#")
                tree = ast.parse((root / source_path).read_text())
                if function:
                    tree = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
                tree = WithoutFence().visit(tree)
                self.assertEqual(hashlib.sha256(ast.dump(tree).encode()).hexdigest(), digest)


@override_settings(NBHD_INTERNAL_API_KEY="local-key", DEPLOY_SECRET="offline")
class EntryPointTests(TestCase):
    def setUp(self):
        self.tenant = seed_internal_key(tenant_fixture(949409), "local-key")
        self.client = APIClient()
        self.client.force_authenticate(self.tenant.user)

    def test_loaded_flag_has_no_queries_and_is_the_only_decision(self):
        for record in ({}, {"status": "RUNNING"}, {"status": "PASS"}):
            self.tenant.openclaw_migration = record
            for enabled in (False, True):
                self.tenant.openclaw_migration_cron_fenced = enabled
                with self.assertNumQueries(0):
                    self.assertIs(cron_edits_fenced(self.tenant), enabled)

    def test_dashboard_entries_add_zero_queries(self):
        cases = (
            (tenant_views.CronJobListCreateView, "post", {}, {}, 400),
            (tenant_views.CronJobDetailView, "patch", {"schedule": {}}, {"job_name": "reminder"}, 400),
            (tenant_views.CronJobDetailView, "delete", {}, {"job_name": "reminder"}, 204),
            (tenant_views.CronJobToggleView, "post", {}, {"job_name": "reminder"}, 400),
            (tenant_views.CronJobBulkDeleteView, "post", {}, {}, 400),
            (tenant_views.CronJobBulkUpdateForegroundView, "post", {}, {}, 400),
            (pending_at_views.PendingAtCronCancelView, "delete", {}, {"name": "reminder"}, 404),
        )
        for canonical in (False, True):
            self.tenant.postgres_cron_canonical = canonical
            for cls, method, data, kwargs, expected in cases:
                for fenced in (False, True):
                    self.tenant.openclaw_migration_cron_fenced = fenced
                    with (
                        self.subTest(view=cls.__name__, method=method, canonical=canonical, fenced=fenced),
                        patch.object(tenant_views, "_get_tenant_for_user", return_value=self.tenant),
                        patch.object(pending_at_views, "_get_tenant_for_user", return_value=self.tenant),
                        patch.object(tenant_views, "cron_remove") as remove,
                        patch("apps.cron.postgres_canonical.delete_job", return_value=(None, 204)) as delete,
                        patch.object(pending_at_views, "invoke_gateway_tool", return_value={}) as gateway,
                        self.assertNumQueries(0),
                    ):
                        response = getattr(cls(), method)(SimpleNamespace(user=None, data=data), **kwargs)
                        self.assertEqual(response.status_code, 409 if fenced else expected)
                        if fenced:
                            self.assertEqual(response.data, {"error": "assistant_updating", "retry_after": 60})
                            remove.assert_not_called()
                            delete.assert_not_called()
                            gateway.assert_not_called()

    def test_all_assistant_entries_add_zero_queries_and_return_friendly_retry(self):
        cases = (
            (runtime_views.RuntimeCronCreatePureReminderView, "post", {}),
            (runtime_views.RuntimeCronCreateQuoteUserIntentView, "post", {}),
            (runtime_views.RuntimeCronCreateDomainSummaryView, "post", {}),
            (runtime_views.RuntimeCronPhase2SummaryView, "post", {}),
            (runtime_views.RuntimeProfileUpdateView, "patch", {"timezone": "invalid-zone"}),
        )
        # Warm the relationship used by the existing profile path.
        self.assertIsNotNone(self.tenant.user)
        for cls, method, data in cases:
            for fenced in (False, True):
                self.tenant.openclaw_migration_cron_fenced = fenced
                with (
                    self.subTest(view=cls.__name__, fenced=fenced),
                    patch.object(runtime_views, "_internal_auth_or_401", return_value=None),
                    patch.object(runtime_views, "_load_tenant_or_404", return_value=(self.tenant, None)),
                    patch.object(runtime_views, "record_runtime_write_activity"),
                    patch.object(runtime_views, "assert_write_allowed_for_document_turn", return_value=None),
                    self.assertNumQueries(0 if fenced or cls is runtime_views.RuntimeCronPhase2SummaryView else 1),
                ):
                    response = getattr(cls(), method)(SimpleNamespace(data=data), self.tenant.pk)
                    self.assertEqual(response.status_code, 409 if fenced else 400, response.data)
                    if fenced:
                        self.assertEqual(response.data["error"], "assistant_updating")
                        self.assertEqual(response.data["retry_after"], 60)
                        self.assertIn("one minute", response.data["detail"])

    def test_http_routes_block_before_validation_or_side_effects(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration_cron_fenced=True)
        # Clear the user reverse-relation cache just as a fresh request does.
        self.tenant.user._state.fields_cache.pop("tenant", None)
        cases = (
            ("post", "/api/v1/cron-jobs/"),
            ("patch", "/api/v1/cron-jobs/reminder/"),
            ("delete", "/api/v1/cron-jobs/reminder/"),
            ("post", "/api/v1/cron-jobs/reminder/toggle/"),
            ("post", "/api/v1/cron-jobs/bulk-delete/"),
            ("post", "/api/v1/cron-jobs/bulk-update-foreground/"),
            ("delete", "/api/v1/cron-jobs/pending-at/reminder/"),
        )
        for method, path in cases:
            with self.subTest(path=path, method=method):
                response = getattr(self.client, method)(path, {}, format="json")
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json(), {"error": "assistant_updating", "retry_after": 60})
        for suffix in (
            "crons/pure_reminder/",
            "crons/quote_user_intent/",
            "crons/domain_summary/",
            "cron-phase2-summary/",
        ):
            response = self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.pk}/{suffix}",
                {},
                format="json",
                HTTP_X_NBHD_INTERNAL_KEY="local-key",
                HTTP_X_NBHD_TENANT_ID=str(self.tenant.pk),
            )
            self.assertEqual(response.status_code, 409, response.content)
            self.assertIn("one minute", response.json()["detail"])

    def test_settings_and_indirect_reminder_entries_add_zero_queries(self):
        from rest_framework.response import Response

        from apps.fuel import runtime_views as fuel_views
        from apps.tenants import views as settings_views

        # These paths modify generated reminders or remove reminders saved from documents.
        cases = (
            (settings_views.HeartbeatConfigView, "patch", {}, {}, 200),
            (settings_views.ProfileView, "patch", {"timezone": "UTC"}, {}, 200),
            (
                runtime_views.RuntimeDocumentForgetView,
                "post",
                {},
                {"tenant_id": self.tenant.pk, "ingestion_id": "example"},
                200,
            ),
            (
                fuel_views.RuntimeFuelProfileView,
                "patch",
                {"preferred_time": "morning"},
                {"tenant_id": self.tenant.pk},
                202,
            ),
            (fuel_views.RuntimeWorkoutPlanListCreateView, "post", {}, {"tenant_id": self.tenant.pk}, 202),
            (
                fuel_views.RuntimeWorkoutPlanDetailView,
                "patch",
                {"name": "new"},
                {"tenant_id": self.tenant.pk, "plan_id": "example"},
                404,
            ),
            (
                fuel_views.RuntimeWorkoutPlanDetailView,
                "delete",
                {},
                {"tenant_id": self.tenant.pk, "plan_id": "example"},
                404,
            ),
        )
        self.tenant.user.tenant = self.tenant
        for cls, method, data, kwargs, expected in cases:
            for fenced in (False, True):
                self.tenant.openclaw_migration_cron_fenced = fenced
                with (
                    self.subTest(view=cls.__name__, method=method, fenced=fenced),
                    patch.object(settings_views, "UserSerializer") as serializer,
                    patch.object(runtime_views, "_internal_auth_or_401", return_value=None),
                    patch.object(runtime_views, "_load_tenant_or_404", return_value=(self.tenant, None)),
                    patch.object(runtime_views, "record_runtime_write_activity"),
                    patch("apps.journal.document_ingestion.forget_ingestion", return_value={}) as forget,
                    patch.object(fuel_views, "_internal_auth_or_401", return_value=None),
                    patch.object(fuel_views, "_get_tenant_or_404", return_value=self.tenant),
                    patch.object(fuel_views, "record_runtime_write_activity"),
                    patch.object(fuel_views.RuntimeWorkoutPlanDetailView, "_get_plan", return_value=None),
                    patch.object(
                        fuel_views, "assert_write_allowed_for_document_turn", return_value=Response({}, status=202)
                    ),
                ):
                    serializer.return_value.validated_data = {}
                    serializer.return_value.data = {}
                    with self.assertNumQueries(0):
                        response = getattr(cls(), method)(SimpleNamespace(data=data, user=self.tenant.user), **kwargs)
                    self.assertEqual(response.status_code, 409 if fenced else expected, response.data)
                    if fenced:
                        forget.assert_not_called()
                        serializer.assert_not_called()
                        self.assertEqual(response.data["error"], "assistant_updating")
                        self.assertEqual(response.data["retry_after"], 60)

    def test_manual_registry_delete_has_no_added_queries(self):
        from apps.cron.views import delete_registry_cron

        request = SimpleNamespace(
            method="POST",
            headers={"X-Deploy-Secret": "offline"},
            body=json.dumps({"tenant_id": str(self.tenant.pk), "name": "missing"}).encode(),
        )
        for fenced in (False, True):
            self.tenant.openclaw_migration_cron_fenced = fenced
            with (
                patch("apps.cron.views.Tenant.objects.get", return_value=self.tenant),
                patch("apps.tenants.middleware.set_rls_context"),
                self.assertNumQueries(0 if fenced else 1),
            ):
                response = delete_registry_cron(request)
                self.assertEqual(response.status_code, 409 if fenced else 404)

    def test_all_channel_approval_seam_refuses_without_resolving_action(self):
        for fenced in (False, True):
            Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration_cron_fenced=fenced)
            action = PendingAction.objects.create(
                tenant=self.tenant, action_type=ActionType.CRON_CREATE, action_payload={}
            )
            with patch("apps.cron.gate.approve_cron_action", return_value={"main": True}) as approve:
                # Existing savepoint, action lookup, tenant lookup, release; fence adds no queries.
                with self.assertNumQueries(4):
                    _, data, code = GateRespondView.resolve_action(action_id=action.pk, response_action="approve")
                self.assertEqual(code, 409 if fenced else 200)
                if fenced:
                    self.assertEqual(data, {"error": "assistant_updating", "retry_after": 60})
                    approve.assert_not_called()
                    action.refresh_from_db()
                    self.assertEqual(action.status, ActionStatus.PENDING)
                else:
                    approve.assert_called_once()


class DrainAndRecoveryTests(TestCase):
    setUp = PrestagingTests.setUp

    def test_drain_precedes_final_capture_and_restages_changed_canonical_digest(self):
        record = {
            "status": "RUNNING",
            "tag": TAG,
            "owner_token": "owner",
            "cron_export": [job()],
            "evidence": {"preflight": {"target_image": "target", "revision_suffix": "new"}},
        }
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration=record)
        with suppress_cronjob_reconcile():
            row = CronJob.objects.create(tenant=self.tenant, name="reminder", data=job(), managed=True)
        m.stage_signed_crons(self.tenant, record, checkpoint="signed_prestaged")
        old_digest = record["signed_prestaged"]["digest"]
        events = []
        changed = job(payload={"kind": "agentTurn", "message": "An admitted edit completed during drain"})

        def drain(seconds):
            self.assertEqual(seconds, 30)
            self.tenant.refresh_from_db()
            self.assertTrue(self.tenant.openclaw_migration_cron_fenced)
            # Simulate an already admitted writer finishing during the drain.
            with suppress_cronjob_reconcile():
                row.data = changed
                row.save()
            events.append("drain")

        def source(tenant, **kwargs):
            self.assertEqual(events, ["drain"])
            events.append("capture")
            return [changed]

        def submit(*args, **kwargs):
            self.assertEqual(events, ["drain", "capture"])
            self.assertNotEqual(record["signed_prestaged"]["digest"], old_digest)
            self.assertEqual(
                record["signed_prestaged"]["digest"], hashlib.sha256(self.files["nbhd-crons.json"]).hexdigest()
            )
            self.assertEqual(record["signed_prestaged"]["canonical_revision"], m.canonical_revision(self.tenant))
            raise SystemExit("dead after submission")

        app = SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image="old")], revision_suffix="old")
        )
        with (
            patch.object(m.time, "sleep", side_effect=drain),
            patch.object(m, "live_source_jobs", side_effect=source),
            patch.object(m, "get_app", return_value=app),
            patch.object(m.azure_client, "update_container_image", side_effect=submit),
            self.assertRaises(SystemExit),
        ):
            m.image_step(self.tenant, record)
        out = StringIO()
        with patch.object(m, "live_source_jobs", side_effect=AssertionError("report must not contact tenant")):
            call_command("migrate_tenant_openclaw", tenant=str(self.tenant.pk), report=True, stdout=out)
        self.assertIn("cron_edits_fenced=True", out.getvalue())
        self.assertIn("owner_token=owner", out.getvalue())
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.openclaw_migration_cron_fenced)


class DrainCommitTests(TransactionTestCase):
    def test_drain_commits_fence_without_open_transaction(self):
        tenant = tenant_fixture(9494092)
        record = {"owner_token": "owner"}
        Tenant.objects.filter(pk=tenant.pk).update(openclaw_migration=record)

        def drain(seconds):
            self.assertEqual(seconds, 30)
            self.assertFalse(connection.in_atomic_block)
            observer = connection.Database.connect(**connection.get_connection_params())
            try:
                with observer.cursor() as cursor:
                    cursor.execute("SELECT openclaw_migration_cron_fenced FROM tenants WHERE id = %s", [tenant.pk])
                    self.assertTrue(cursor.fetchone()[0])
            finally:
                observer.close()

        with patch.object(m.time, "sleep", side_effect=drain) as sleep:
            m.fence_and_drain(tenant, record)
        sleep.assert_called_once_with(30)
