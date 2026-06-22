"""Tests for tenant-facing cron job API."""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.cron.tenant_views import (
    _is_hidden_cron,
    _message_has_phase2_marker,
    _normalize_job_for_universal_isolation,
    _strip_phase2_block,
    _wrap_message_with_phase2,
)
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key


def _create_user_and_tenant(*, active=True):
    user = User.objects.create_user(username="cronuser", password="testpass123")
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE if active else Tenant.Status.PENDING,
        container_id="oc-test" if active else "",
        container_fqdn="oc-test.internal.azurecontainerapps.io" if active else "",
        # Default helper exercises the legacy gateway-write path. Tests that
        # need the Postgres-canonical branch flip the flag explicitly in
        # their own setUp (see PostgresCanonicalSignalTest et al.).
        postgres_cron_canonical=False,
    )
    return user, tenant


class CronJobListCreateTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_list_cron_jobs(self, mock_invoke):
        # Use two crons that remain user-visible (Evening Check-in is now
        # hidden as a core data-unearthing task).
        mock_invoke.return_value = {
            "jobs": [
                {"name": "Morning Briefing", "enabled": True},
                {"name": "Weekly Reflection", "enabled": True},
            ],
        }
        resp = self.client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["jobs"]), 2)
        mock_invoke.assert_called_once_with(self.tenant, "cron.list", {})

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_cron_job(self, mock_invoke):
        # First call: cron.list (cap check), second call: cron.add
        mock_invoke.side_effect = [
            {"jobs": []},  # cron.list returns empty
            {"name": "New Job", "enabled": True},  # cron.add result
        ]
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {
                "name": "New Job",
                "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"},
                "payload": {"kind": "agentTurn", "message": "do thing"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(mock_invoke.call_count, 2)
        # First call: cap check
        self.assertEqual(mock_invoke.call_args_list[0][0][1], "cron.list")
        # Second call: actual create — must be normalized to isolated/agentTurn
        # and message must be wrapped with the Phase 2 sync block (foreground default)
        self.assertEqual(mock_invoke.call_args_list[1][0][1], "cron.add")
        created_job = mock_invoke.call_args_list[1][0][2]["job"]
        self.assertEqual(created_job["name"], "New Job")
        self.assertEqual(created_job["sessionTarget"], "isolated")
        self.assertNotIn("wakeMode", created_job)
        self.assertEqual(created_job["payload"]["kind"], "agentTurn")
        # Phase 2 sync block should be appended (foreground default = True)
        self.assertIn("nbhd_cron_phase2_summary", created_job["payload"]["message"])
        self.assertIn('"New Job"', created_job["payload"]["message"])
        self.assertIn("do thing", created_job["payload"]["message"])

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_cron_job_background_skips_phase2_wrap(self, mock_invoke):
        """foreground=false → message is NOT wrapped with the Phase 2 block."""
        mock_invoke.side_effect = [
            {"jobs": []},
            {"name": "Silent Job", "enabled": True},
        ]
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {
                "name": "Silent Job",
                "schedule": {"kind": "cron", "expr": "0 3 * * *", "tz": "UTC"},
                "payload": {"kind": "agentTurn", "message": "quiet maintenance"},
                "foreground": False,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        created_job = mock_invoke.call_args_list[1][0][2]["job"]
        self.assertEqual(created_job["sessionTarget"], "isolated")
        self.assertEqual(created_job["payload"]["message"], "quiet maintenance")
        self.assertNotIn("_sync:", created_job["payload"]["message"])
        # foreground is a Django-only field — never sent to OpenClaw
        self.assertNotIn("foreground", created_job)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_cron_job_rejected_at_cap(self, mock_invoke):
        """Creating a job when at the cap returns 409."""
        mock_invoke.return_value = {
            "jobs": [{"name": f"Job {i}"} for i in range(10)],
        }
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {"name": "One More", "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        # Only cron.list should have been called (no cron.add)
        mock_invoke.assert_called_once()

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_cron_job_duplicate_name_rejected(self, mock_invoke):
        """Creating a job with a duplicate name returns 409."""
        mock_invoke.return_value = {
            "jobs": [{"name": "Morning Briefing"}],
        }
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {"name": "Morning Briefing", "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        mock_invoke.assert_called_once()

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_requires_name(self, mock_invoke):
        resp = self.client.post("/api/v1/cron-jobs/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        mock_invoke.assert_not_called()

    def test_unauthenticated_request_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 401)


class CronJobDetailTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_update_cron_job(self, mock_invoke):
        mock_invoke.return_value = {"name": "Morning Briefing", "enabled": True}
        resp = self.client.patch(
            "/api/v1/cron-jobs/Morning Briefing/",
            {"schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        call_args = mock_invoke.call_args
        self.assertEqual(call_args[0][1], "cron.update")
        self.assertEqual(
            call_args[0][2],
            {
                "jobId": "Morning Briefing",
                "patch": {
                    "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"},
                },
            },
        )

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_delete_cron_job(self, mock_invoke):
        mock_invoke.return_value = {}
        resp = self.client.delete("/api/v1/cron-jobs/Morning Briefing/")
        self.assertEqual(resp.status_code, 204)
        mock_invoke.assert_called_once_with(
            self.tenant,
            "cron.remove",
            {"jobId": "Morning Briefing"},
        )


class CronJobToggleTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_toggle_cron_job(self, mock_invoke):
        mock_invoke.return_value = {"name": "Morning Briefing", "enabled": False}
        resp = self.client.post(
            "/api/v1/cron-jobs/Morning Briefing/toggle/",
            {"enabled": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        call_args = mock_invoke.call_args
        self.assertEqual(call_args[0][1], "cron.update")
        self.assertEqual(
            call_args[0][2],
            {
                "jobId": "Morning Briefing",
                "patch": {"enabled": False},
            },
        )

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_toggle_requires_enabled_field(self, mock_invoke):
        resp = self.client.post(
            "/api/v1/cron-jobs/Morning Briefing/toggle/",
            {},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        mock_invoke.assert_not_called()


class HiddenSystemCronsTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_list_hides_system_crons(self, mock_invoke):
        mock_invoke.return_value = {
            "jobs": [
                {"name": "Morning Briefing", "enabled": True},
                {"name": "Evening Check-in", "enabled": True},
                {"name": "Personal Question", "enabled": True},
                {"name": "Background Tasks", "enabled": True},
                {"name": "Heartbeat Check-in", "enabled": True},
                {"name": "Week Ahead Review", "enabled": True},
            ],
        }
        resp = self.client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 200)
        names = [j["name"] for j in resp.json()["jobs"]]
        # Infrastructure crons are hidden
        self.assertNotIn("Background Tasks", names)
        self.assertNotIn("Heartbeat Check-in", names)
        # Personal Question is a core unearthing task — timing is governed by
        # tenant.heartbeat_start_hour, so the user already controls *when*
        # without owning the cron row itself.
        self.assertNotIn("Personal Question", names)
        # User-controllable system crons still appear — including Evening
        # Check-in, whose 21:00 schedule is hard-coded and which the user
        # has a legitimate need to retime or pause.
        self.assertIn("Morning Briefing", names)
        self.assertIn("Evening Check-in", names)
        self.assertIn("Week Ahead Review", names)
        self.assertEqual(len(names), 3)

    def test_delete_blocked_for_system_crons(self):
        resp = self.client.delete("/api/v1/cron-jobs/Background Tasks/")
        self.assertEqual(resp.status_code, 403)

    def test_delete_blocked_for_heartbeat_checkin(self):
        resp = self.client.delete("/api/v1/cron-jobs/Heartbeat Check-in/")
        self.assertEqual(resp.status_code, 403)

    def test_patch_blocked_for_system_crons(self):
        resp = self.client.patch(
            "/api/v1/cron-jobs/Background Tasks/",
            {"enabled": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_toggle_blocked_for_system_crons(self):
        resp = self.client.post(
            "/api/v1/cron-jobs/Heartbeat Check-in/toggle/",
            {"enabled": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_bulk_delete_blocked_for_system_crons(self):
        resp = self.client.post(
            "/api/v1/cron-jobs/bulk-delete/",
            {"ids": ["Background Tasks", "Morning Briefing"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Background Tasks", resp.json()["detail"])


class CronJobUpdateSnapshotFallbackTest(TestCase):
    """Tests for the snapshot fallback when a job vanishes from the container."""

    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_update_vanished_job_found_in_snapshot(self, mock_invoke):
        """Job missing from container but present in snapshot is recreated."""
        self.tenant.cron_jobs_snapshot = {
            "jobs": [
                {
                    "jobId": "abc123",
                    "name": "My Task",
                    "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"},
                    "sessionTarget": "isolated",
                    "payload": {"kind": "agentTurn", "message": "do stuff"},
                    "delivery": {"mode": "none"},
                    "enabled": True,
                },
            ],
            "snapshot_at": "2026-04-04T00:00:00Z",
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        mock_invoke.side_effect = [
            {"jobs": []},  # cron.list returns empty (job vanished)
            {"name": "My Task", "enabled": True},  # cron.add succeeds
        ]
        resp = self.client.patch(
            "/api/v1/cron-jobs/abc123/",
            {
                "payload": {"kind": "agentTurn", "message": "do stuff"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_invoke.call_count, 2)
        # Should call cron.list then cron.add (no cron.remove since job vanished)
        self.assertEqual(mock_invoke.call_args_list[0][0][1], "cron.list")
        self.assertEqual(mock_invoke.call_args_list[1][0][1], "cron.add")
        # Universal isolation: every job is sessionTarget=isolated, agentTurn payload
        created_job = mock_invoke.call_args_list[1][0][2]["job"]
        self.assertEqual(created_job["sessionTarget"], "isolated")
        self.assertEqual(created_job["payload"]["kind"], "agentTurn")

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_update_vanished_job_not_in_snapshot_creates_fresh(self, mock_invoke):
        """Job missing from both container and snapshot is created from scratch."""
        self.tenant.cron_jobs_snapshot = {"jobs": [], "snapshot_at": "2026-04-04T00:00:00Z"}
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        mock_invoke.side_effect = [
            {"jobs": []},  # cron.list returns empty
            {"name": "Ghost Task", "enabled": True},  # cron.add succeeds
        ]
        resp = self.client.patch(
            "/api/v1/cron-jobs/Ghost Task/",
            {
                "payload": {"kind": "agentTurn", "message": "hello"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_invoke.call_count, 2)
        self.assertEqual(mock_invoke.call_args_list[1][0][1], "cron.add")
        created_job = mock_invoke.call_args_list[1][0][2]["job"]
        self.assertEqual(created_job["name"], "Ghost Task")
        self.assertEqual(created_job["sessionTarget"], "isolated")

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_legacy_main_payload_migrated_to_isolated_on_recreate(self, mock_invoke):
        """A legacy main-session job in the gateway gets normalized to isolated on edit."""
        mock_invoke.side_effect = [
            # cron.list returns a legacy main-session job with systemEvent payload
            {
                "jobs": [
                    {
                        "jobId": "abc123",
                        "name": "Legacy Task",
                        "sessionTarget": "main",
                        "wakeMode": "now",
                        "payload": {"kind": "systemEvent", "text": "morning briefing"},
                        "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                        "enabled": True,
                    },
                ]
            },
            {},  # cron.remove
            {"name": "Legacy Task", "enabled": True},  # cron.add
        ]
        resp = self.client.patch(
            "/api/v1/cron-jobs/abc123/",
            # Edit just changes the payload — but the recreate path must
            # also force isolated and drop wakeMode
            {"payload": {"kind": "agentTurn", "message": "morning briefing"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_invoke.call_count, 3)
        created_job = mock_invoke.call_args_list[2][0][2]["job"]
        self.assertEqual(created_job["sessionTarget"], "isolated")
        self.assertEqual(created_job["payload"]["kind"], "agentTurn")
        self.assertEqual(
            created_job["payload"]["message"].split("\n\n---\n")[0],
            "morning briefing",
        )
        self.assertNotIn("text", created_job["payload"])
        self.assertNotIn("wakeMode", created_job)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_legacy_delivery_channel_preserved_on_recreate(self, mock_invoke):
        """delivery.channel/to are preserved under universal isolation.

        OpenClaw allows channel-based delivery on isolated jobs, and the
        previous main-only restriction is gone now that every job is
        isolated. User-created channel-based crons should keep working.
        """
        mock_invoke.side_effect = [
            {
                "jobs": [
                    {
                        "jobId": "abc123",
                        "name": "Daily Workout Plan",
                        "sessionTarget": "main",
                        "payload": {"kind": "systemEvent", "text": "workout"},
                        "schedule": {"kind": "cron", "expr": "0 5 * * *", "tz": "Asia/Tokyo"},
                        "delivery": {"channel": "telegram", "to": "12345", "mode": "auto"},
                        "enabled": True,
                    },
                ]
            },
            {},  # cron.remove
            {"name": "Daily Workout Plan", "enabled": True},  # cron.add
        ]
        resp = self.client.patch(
            "/api/v1/cron-jobs/abc123/",
            {"payload": {"kind": "agentTurn", "message": "workout"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        created_job = mock_invoke.call_args_list[2][0][2]["job"]
        self.assertEqual(created_job["sessionTarget"], "isolated")
        self.assertEqual(
            created_job["delivery"],
            {"channel": "telegram", "to": "12345", "mode": "auto"},
        )

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_foreground_toggle_off_strips_phase2_block(self, mock_invoke):
        """PATCH {foreground: false} must strip the Phase 2 sync block."""
        mock_invoke.side_effect = [
            {
                "jobs": [
                    {
                        "jobId": "abc123",
                        "name": "My Task",
                        "sessionTarget": "isolated",
                        "payload": {
                            "kind": "agentTurn",
                            # Existing message has the marker (foreground)
                            "message": (
                                "do stuff\n\n---\n**FINAL STEP — conditional sync to "
                                "the main session:**\n... wrapper content ..."
                            ),
                        },
                        "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"},
                        "enabled": True,
                    },
                ]
            },
            {},  # cron.remove
            {"name": "My Task", "enabled": True},  # cron.add
        ]
        resp = self.client.patch(
            "/api/v1/cron-jobs/abc123/",
            {"foreground": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        # foreground change is not a patchable field → goes through delete+recreate
        self.assertEqual(mock_invoke.call_count, 3)
        created_job = mock_invoke.call_args_list[2][0][2]["job"]
        self.assertEqual(created_job["sessionTarget"], "isolated")
        # Marker must be stripped from the message
        self.assertNotIn("FINAL STEP", created_job["payload"]["message"])
        self.assertEqual(created_job["payload"]["message"], "do stuff")
        # foreground is a Django-only field — never sent to OpenClaw
        self.assertNotIn("foreground", created_job)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_foreground_toggle_on_appends_phase2_block(self, mock_invoke):
        """PATCH {foreground: true} on a background job must append the Phase 2 block."""
        mock_invoke.side_effect = [
            {
                "jobs": [
                    {
                        "jobId": "abc123",
                        "name": "My Task",
                        "sessionTarget": "isolated",
                        "payload": {"kind": "agentTurn", "message": "quiet maintenance"},
                        "schedule": {"kind": "cron", "expr": "0 3 * * *", "tz": "UTC"},
                        "enabled": True,
                    },
                ]
            },
            {},
            {"name": "My Task", "enabled": True},
        ]
        resp = self.client.patch(
            "/api/v1/cron-jobs/abc123/",
            {"foreground": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        created_job = mock_invoke.call_args_list[2][0][2]["job"]
        msg = created_job["payload"]["message"]
        self.assertIn("nbhd_cron_phase2_summary", msg)
        self.assertIn('"My Task"', msg)
        self.assertTrue(msg.startswith("quiet maintenance"))

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_update_existing_job_still_uses_delete_recreate(self, mock_invoke):
        """Job present in container still goes through normal delete+recreate."""
        mock_invoke.side_effect = [
            # cron.list returns the job
            {
                "jobs": [
                    {
                        "jobId": "abc123",
                        "name": "My Task",
                        "sessionTarget": "isolated",
                        "payload": {"kind": "agentTurn", "message": "old"},
                        "enabled": True,
                    },
                ]
            },
            {},  # cron.remove succeeds
            {"name": "My Task", "enabled": True},  # cron.add succeeds
        ]
        resp = self.client.patch(
            "/api/v1/cron-jobs/abc123/",
            {
                "payload": {"kind": "agentTurn", "message": "new"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_invoke.call_count, 3)
        self.assertEqual(mock_invoke.call_args_list[0][0][1], "cron.list")
        self.assertEqual(mock_invoke.call_args_list[1][0][1], "cron.remove")
        self.assertEqual(mock_invoke.call_args_list[2][0][1], "cron.add")


class InactiveTenantTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant(active=False)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_inactive_tenant_returns_502(self, mock_invoke):
        resp = self.client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 502)
        mock_invoke.assert_not_called()


class HiddenCronHelperTest(SimpleTestCase):
    """Unit tests for the _is_hidden_cron helper."""

    def test_named_system_jobs_are_hidden(self):
        self.assertTrue(_is_hidden_cron("Background Tasks"))
        self.assertTrue(_is_hidden_cron("Heartbeat Check-in"))
        self.assertTrue(_is_hidden_cron("Project Check-in"))

    def test_core_unearthing_crons_are_hidden(self):
        # Personal Question pursues one long-term-memory gap per day. Its
        # timing is governed by tenant.heartbeat_start_hour, so the user
        # already controls *when* without owning the cron row.
        self.assertTrue(_is_hidden_cron("Personal Question"))

    def test_evening_checkin_is_visible(self):
        # Evening Check-in's 21:00 schedule is hard-coded; the user has a
        # legitimate need to retime or pause it. Until the schedule is
        # wired to a tenant field, it stays in the user-editable cron list.
        self.assertFalse(_is_hidden_cron("Evening Check-in"))

    def test_sync_prefix_jobs_are_hidden(self):
        self.assertTrue(_is_hidden_cron("_sync:Morning Briefing"))
        self.assertTrue(_is_hidden_cron("_sync:Anything Goes"))
        self.assertTrue(_is_hidden_cron("_sync:"))

    def test_normal_jobs_are_visible(self):
        self.assertFalse(_is_hidden_cron("Morning Briefing"))
        self.assertFalse(_is_hidden_cron("My Custom Task"))
        self.assertFalse(_is_hidden_cron("sync:not-prefixed"))

    def test_empty_name_is_visible(self):
        self.assertFalse(_is_hidden_cron(""))
        self.assertFalse(_is_hidden_cron(None))  # type: ignore[arg-type]


class NormalizationHelperTest(SimpleTestCase):
    """Unit tests for _normalize_job_for_universal_isolation."""

    def test_full_job_dict_forced_to_isolated(self):
        out = _normalize_job_for_universal_isolation(
            {
                "name": "Test",
                "sessionTarget": "main",
                "wakeMode": "now",
                "payload": {"kind": "systemEvent", "text": "hello"},
                "delivery": {"channel": "telegram", "to": "12345", "mode": "auto"},
            }
        )
        self.assertEqual(out["sessionTarget"], "isolated")
        self.assertNotIn("wakeMode", out)
        self.assertEqual(out["payload"], {"kind": "agentTurn", "message": "hello"})
        # Delivery is left untouched — channel-based delivery still works
        # under universal isolation (the main-only restriction is gone).
        self.assertEqual(
            out["delivery"],
            {"channel": "telegram", "to": "12345", "mode": "auto"},
        )

    def test_partial_patch_left_alone(self):
        """If none of the structural fields are in the input, normalization is a no-op."""
        out = _normalize_job_for_universal_isolation(
            {
                "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"},
                "enabled": True,
            }
        )
        self.assertEqual(
            out,
            {
                "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"},
                "enabled": True,
            },
        )

    def test_payload_only_triggers_normalization(self):
        out = _normalize_job_for_universal_isolation(
            {
                "payload": {"kind": "systemEvent", "text": "hi"},
            }
        )
        self.assertEqual(out["sessionTarget"], "isolated")
        self.assertEqual(out["payload"], {"kind": "agentTurn", "message": "hi"})

    def test_already_isolated_payload_preserved(self):
        out = _normalize_job_for_universal_isolation(
            {
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "hi"},
            }
        )
        self.assertEqual(out["sessionTarget"], "isolated")
        self.assertEqual(out["payload"], {"kind": "agentTurn", "message": "hi"})


class Phase2WrapHelperTest(SimpleTestCase):
    """Unit tests for _wrap_message_with_phase2 / _strip_phase2_block."""

    def test_foreground_appends_block(self):
        wrapped = _wrap_message_with_phase2("base prompt", "Test Job", foreground=True)
        self.assertTrue(wrapped.startswith("base prompt"))
        self.assertIn("FINAL STEP", wrapped)
        self.assertIn("nbhd_cron_phase2_summary", wrapped)
        self.assertIn('"Test Job"', wrapped)

    def test_foreground_idempotent(self):
        once = _wrap_message_with_phase2("base", "Test Job", foreground=True)
        twice = _wrap_message_with_phase2(once, "Test Job", foreground=True)
        self.assertEqual(once, twice)
        # Marker should appear exactly once
        self.assertEqual(twice.count("FINAL STEP — conditional sync"), 1)

    def test_background_strips_existing_block(self):
        wrapped = _wrap_message_with_phase2("base", "Test Job", foreground=True)
        self.assertTrue(_message_has_phase2_marker(wrapped))
        unwrapped = _wrap_message_with_phase2(wrapped, "Test Job", foreground=False)
        self.assertEqual(unwrapped, "base")
        self.assertFalse(_message_has_phase2_marker(unwrapped))

    def test_background_no_marker_passthrough(self):
        out = _wrap_message_with_phase2("plain", "Test Job", foreground=False)
        self.assertEqual(out, "plain")

    def test_strip_handles_messages_without_marker(self):
        self.assertEqual(_strip_phase2_block("just a message"), "just a message")
        self.assertEqual(_strip_phase2_block(""), "")

    def test_marker_detection(self):
        wrapped = _wrap_message_with_phase2("base", "Test Job", foreground=True)
        self.assertTrue(_message_has_phase2_marker(wrapped))
        self.assertFalse(_message_has_phase2_marker("base"))
        self.assertFalse(_message_has_phase2_marker(""))


# ═════════════════════════════════════════════════════════════════════
# Bulk-write hibernation-wake — same pattern as the read-side cache
# fallback. Writes against a hibernated container would otherwise return
# the raw Azure splash to the user.
# ═════════════════════════════════════════════════════════════════════


class CronJobBulkWriteHibernationTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant")
    def test_bulk_delete_hibernated_returns_503_and_wakes(self, mock_wake):
        from django.utils import timezone

        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])

        resp = self.client.post(
            "/api/v1/cron-jobs/bulk-delete/",
            {"ids": ["some-job-id"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.json().get("container_waking"))
        mock_wake.assert_called_once()

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant")
    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_bulk_delete_503_on_mid_loop_azure_splash(self, mock_invoke, mock_wake):
        """If the gateway returns the Azure splash mid-loop, abort and 503."""
        from apps.cron.gateway_client import GatewayError

        # First delete: Azure splash. We should not attempt subsequent ones.
        mock_invoke.side_effect = GatewayError(
            "Gateway returned 404: <title>Container App - Unavailable</title>",
            status_code=404,
        )
        resp = self.client.post(
            "/api/v1/cron-jobs/bulk-delete/",
            {"ids": ["job-a", "job-b", "job-c"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.json().get("container_waking"))
        mock_wake.assert_called_once()
        # We aborted after the first failure rather than calling cron.remove
        # for every id.
        self.assertEqual(mock_invoke.call_count, 1)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant")
    def test_bulk_update_foreground_hibernated_returns_503_and_wakes(self, mock_wake):
        from django.utils import timezone

        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])

        resp = self.client.post(
            "/api/v1/cron-jobs/bulk-update-foreground/",
            {"ids": ["some-job-id"], "foreground": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.json().get("container_waking"))
        mock_wake.assert_called_once()


# ═════════════════════════════════════════════════════════════════════
# Postgres-canonical cron flow — flag-gated, one cohesive surface
# ═════════════════════════════════════════════════════════════════════


class PostgresCanonicalReadTest(TestCase):
    """Read paths must serve Postgres without touching the gateway when
    `postgres_cron_canonical=True`."""

    def setUp(self):
        from apps.cron.models import CronJob, CronJobSource

        self.user, self.tenant = _create_user_and_tenant()
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        CronJob.objects.create(
            tenant=self.tenant,
            name="My Task",
            data={
                "name": "My Task",
                "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
                "payload": {"kind": "agentTurn", "message": "hi"},
            },
            source=CronJobSource.USER,
            managed=True,
            enabled=True,
        )
        # Hidden system cron — must not appear in dashboard list.
        CronJob.objects.create(
            tenant=self.tenant,
            name="Background Tasks",
            data={"name": "Background Tasks"},
            source=CronJobSource.SYSTEM,
            managed=True,
            enabled=True,
        )

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_list_serves_postgres_no_gateway_call(self, mock_invoke):
        resp = self.client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 200)
        names = [j["name"] for j in resp.json()["jobs"]]
        self.assertEqual(names, ["My Task"])  # hidden cron excluded
        mock_invoke.assert_not_called()


class PostgresCanonicalWriteTest(TestCase):
    """Write paths mutate Postgres + fire signal; gateway is untouched."""

    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_creates_row_no_gateway(self, mock_invoke):
        from apps.cron.models import CronJob

        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {
                "name": "Daily Reminder",
                "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
                "payload": {"kind": "agentTurn", "message": "Hi"},
                "delivery": {"mode": "none"},
                "foreground": False,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name="Daily Reminder").exists())
        mock_invoke.assert_not_called()

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_delete_removes_row_no_gateway(self, mock_invoke):
        from apps.cron.models import CronJob, CronJobSource

        CronJob.objects.create(
            tenant=self.tenant,
            name="Throwaway",
            data={"name": "Throwaway"},
            source=CronJobSource.USER,
        )
        resp = self.client.delete("/api/v1/cron-jobs/Throwaway/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name="Throwaway").exists())
        mock_invoke.assert_not_called()

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_toggle_flips_enabled_column(self, mock_invoke):
        from apps.cron.models import CronJob, CronJobSource

        row = CronJob.objects.create(
            tenant=self.tenant,
            name="Daily",
            data={"name": "Daily", "enabled": True},
            source=CronJobSource.USER,
            enabled=True,
        )
        resp = self.client.post(
            f"/api/v1/cron-jobs/{row.name}/toggle/",
            {"enabled": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        row.refresh_from_db()
        self.assertFalse(row.enabled)
        mock_invoke.assert_not_called()

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_bulk_delete_removes_rows_no_gateway(self, mock_invoke):
        from apps.cron.models import CronJob, CronJobSource

        for n in ("a", "b", "c"):
            CronJob.objects.create(
                tenant=self.tenant,
                name=n,
                data={"name": n},
                source=CronJobSource.USER,
            )
        resp = self.client.post(
            "/api/v1/cron-jobs/bulk-delete/",
            {"ids": ["a", "b"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deleted"], 2)
        self.assertCountEqual(
            list(CronJob.objects.filter(tenant=self.tenant).values_list("name", flat=True)),
            ["c"],
        )
        mock_invoke.assert_not_called()

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_hidden_crons_do_not_count_toward_user_quota(self, mock_invoke):
        """Personal Question and infrastructure crons must not consume the
        user's MAX_CRON_JOBS slots — otherwise a tenant with all hidden
        system crons (Background Tasks, Heartbeat, Project, Personal) would
        have 4 slots silently consumed.
        """
        from apps.cron.models import CronJob, CronJobSource

        # 9 user crons + every hidden system cron. With hidden counting,
        # this would be at-cap (or over) and the 10th user create would 409.
        for i in range(9):
            CronJob.objects.create(
                tenant=self.tenant,
                name=f"User Cron {i}",
                data={"name": f"User Cron {i}"},
                source=CronJobSource.USER,
            )
        for hidden in (
            "Background Tasks",
            "Heartbeat Check-in",
            "Personal Question",
            "Project Check-in",
        ):
            CronJob.objects.create(
                tenant=self.tenant,
                name=hidden,
                data={"name": hidden},
                source=CronJobSource.SYSTEM,
            )
        # Sanity: 13 total rows but only 9 should be visible.
        self.assertEqual(CronJob.objects.filter(tenant=self.tenant).count(), 13)

        # The 10th user cron must succeed — hidden crons don't consume slots.
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {
                "name": "User Cron 9",
                "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
                "payload": {"kind": "agentTurn", "message": "hi"},
                "delivery": {"mode": "none"},
                "foreground": False,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        # And the 11th must 409 — proving the cap still works against visible.
        resp_over = self.client.post(
            "/api/v1/cron-jobs/",
            {
                "name": "User Cron 10",
                "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
                "payload": {"kind": "agentTurn", "message": "hi"},
                "delivery": {"mode": "none"},
                "foreground": False,
            },
            format="json",
        )
        self.assertEqual(resp_over.status_code, 409, resp_over.content)
        mock_invoke.assert_not_called()


class PostgresCanonicalSignalTest(TestCase):
    """post_save / post_delete on CronJob enqueue the debounced regen,
    only when the tenant is on the new flow."""

    def setUp(self):
        # The custom test runner disconnects the reconciler signal at
        # test-environment setup (see ``config/test_runner.py``). This class
        # exists to verify the signal contract, so reconnect for the
        # duration of these tests and tear down on cleanup.
        from apps.cron.signals import (
            connect_cronjob_reconcile_signals,
            disconnect_cronjob_reconcile_signals,
        )

        connect_cronjob_reconcile_signals()
        self.addCleanup(disconnect_cronjob_reconcile_signals)

        self.user, self.tenant = _create_user_and_tenant()

    @patch("apps.cron.publish.publish_task")
    def test_no_publish_when_flag_off(self, mock_publish):
        from apps.cron.models import CronJob, CronJobSource

        CronJob.objects.create(
            tenant=self.tenant,
            name="Foo",
            data={},
            source=CronJobSource.USER,
        )
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    def test_publishes_debounced_regen_when_flag_on(self, mock_publish):
        from apps.cron.models import CronJob, CronJobSource

        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        CronJob.objects.create(
            tenant=self.tenant,
            name="Bar",
            data={},
            source=CronJobSource.USER,
        )
        mock_publish.assert_called()
        call = mock_publish.call_args
        self.assertEqual(call[0][0], "regenerate_tenant_crons")
        self.assertEqual(call[1]["delay_seconds"], 30)
        self.assertEqual(call[1]["idempotency_key"], f"regen-cron-{self.tenant.id}")

    @patch("apps.cron.publish.publish_task")
    def test_publishes_on_delete(self, mock_publish):
        from apps.cron.models import CronJob, CronJobSource

        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        row = CronJob.objects.create(
            tenant=self.tenant,
            name="Bar",
            data={},
            source=CronJobSource.USER,
        )
        mock_publish.reset_mock()
        row.delete()
        mock_publish.assert_called_once()


class RegenerateTenantCronsTest(TestCase):
    """Reconciler diff-and-apply against gateway."""

    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.tenant.postgres_cron_canonical = True
        self.tenant.container_fqdn = "oc-test.example.com"
        self.tenant.save(update_fields=["postgres_cron_canonical", "container_fqdn"])

    def test_skips_when_flag_off(self):
        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        self.tenant.postgres_cron_canonical = False
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(
            result,
            {
                "added": 0,
                "removed": 0,
                "recreated": 0,
                "unchanged": 0,
                "errors": 0,
                "stuck_reaped": 0,
                "cap_reaped": 0,
                "at_pending": 0,
                "duplicates_reaped": 0,
            },
        )

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_adds_missing_jobs(self, mock_invoke):
        from apps.cron.models import CronJob, CronJobSource
        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        CronJob.objects.create(
            tenant=self.tenant,
            name="Morning Briefing",
            data={
                "name": "Morning Briefing",
                "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "x"},
                "delivery": {"mode": "none"},
            },
            source=CronJobSource.SYSTEM,
            managed=True,
        )
        mock_invoke.side_effect = lambda tenant, tool, args: {"details": {"jobs": []}} if tool == "cron.list" else None
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["removed"], 0)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_removes_stale_managed_jobs(self, mock_invoke):
        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        # No CronJob rows = empty desired. Container has one job that we
        # consider managed (no underscore prefix).
        mock_invoke.side_effect = lambda tenant, tool, args: (
            {"details": {"jobs": [{"name": "Old Task", "id": "j1"}]}} if tool == "cron.list" else None
        )
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["removed"], 1)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_leaves_sync_prefix_alone(self, mock_invoke):
        """`_sync:*` agent-created crons must survive reconciliation."""
        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        mock_invoke.side_effect = lambda tenant, tool, args: (
            {"details": {"jobs": [{"name": "_sync:Morning Briefing", "id": "sync-1"}]}} if tool == "cron.list" else None
        )
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["removed"], 0)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_unmanaged_rows_dont_drive_adds(self, mock_invoke):
        from apps.cron.models import CronJob, CronJobSource
        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        CronJob.objects.create(
            tenant=self.tenant,
            name="_sync:foo",
            data={"name": "_sync:foo"},
            source=CronJobSource.AGENT,
            managed=False,
        )
        mock_invoke.side_effect = lambda tenant, tool, args: {"details": {"jobs": []}} if tool == "cron.list" else None
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["added"], 0)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_leaves_at_kind_alone(self, mock_invoke):
        """``kind:"at"`` one-shots are unmanaged — the gateway auto-deletes
        them on success, so the reconciler must not race that."""
        import time

        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        future_ms = int(time.time() * 1000) + 60 * 60 * 1000  # 1h from now
        mock_invoke.side_effect = lambda tenant, tool, args: (
            {
                "details": {
                    "jobs": [
                        {
                            "name": "Take out laundry",
                            "id": "at-1",
                            "schedule": {"kind": "at", "at": "2030-01-01T00:00:00Z"},
                            "state": {"nextRunAtMs": future_ms},
                            "enabled": True,
                        }
                    ]
                }
            }
            if tool == "cron.list"
            else None
        )
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["stuck_reaped"], 0)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_reaps_past_due_at_cron(self, mock_invoke):
        """Past-due ``at`` jobs older than the grace window get reaped by
        the janitor — covers crashed-mid-fire and slept-through-fire cases."""
        import time

        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        stale_ms = int(time.time() * 1000) - 2 * 60 * 60 * 1000  # 2h ago
        calls: list[tuple[str, dict]] = []

        def _stub(tenant, tool, args):
            calls.append((tool, args))
            if tool == "cron.list":
                return {
                    "details": {
                        "jobs": [
                            {
                                "name": "Forgotten reminder",
                                "id": "at-stale",
                                "schedule": {"kind": "at", "at": "2024-01-01T00:00:00Z"},
                                "state": {"nextRunAtMs": stale_ms},
                                "enabled": True,
                            }
                        ]
                    }
                }
            return None

        mock_invoke.side_effect = _stub
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["stuck_reaped"], 1)
        self.assertEqual(result["removed"], 0)
        # Ensure the janitor actually issued cron.remove with the job id.
        remove_calls = [c for c in calls if c[0] == "cron.remove"]
        self.assertEqual(len(remove_calls), 1)
        self.assertEqual(remove_calls[0][1], {"jobId": "at-stale"})

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_fresh_at_cron_not_reaped(self, mock_invoke):
        """An ``at`` job that hasn't fired yet must not be reaped by the
        janitor even though it has no corresponding Postgres row."""
        import time

        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        future_ms = int(time.time() * 1000) + 20 * 60 * 1000  # 20m from now
        mock_invoke.side_effect = lambda tenant, tool, args: (
            {
                "details": {
                    "jobs": [
                        {
                            "name": "Drink water",
                            "id": "at-fresh",
                            "schedule": {"kind": "at", "at": "2030-01-01T00:00:00Z"},
                            "state": {"nextRunAtMs": future_ms},
                            "enabled": True,
                        }
                    ]
                }
            }
            if tool == "cron.list"
            else None
        )
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["stuck_reaped"], 0)

    def test_is_past_due_at_cron_parses_iso_fallback(self):
        """If ``state.nextRunAtMs`` is missing, the janitor falls back to
        parsing ``schedule.at`` directly."""
        import time

        from apps.orchestrator.cron_reconcile import _is_past_due_at_cron

        now_ms = int(time.time() * 1000)
        stale_job = {
            "name": "iso-stale",
            "schedule": {"kind": "at", "at": "2020-01-01T00:00:00Z"},
        }
        fresh_job = {
            "name": "iso-fresh",
            "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
        }
        not_an_at_job = {
            "name": "recurring",
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
        }
        self.assertTrue(_is_past_due_at_cron(stale_job, now_ms))
        self.assertFalse(_is_past_due_at_cron(fresh_job, now_ms))
        self.assertFalse(_is_past_due_at_cron(not_an_at_job, now_ms))

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_hard_cap_logs_but_does_not_reap(self, mock_invoke):
        """At 51+ pending at-crons: log a PlatformIssueLog, no reaping —
        respect the user's queued intent."""
        import time

        from apps.orchestrator.cron_reconcile import _AT_CRON_HARD_CAP, regenerate_tenant_crons
        from apps.platform_logs.models import PlatformIssueLog

        future_ms = int(time.time() * 1000) + 60 * 60 * 1000
        jobs = [
            {
                "name": f"reminder-{i}",
                "id": f"at-{i}",
                "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
                "state": {"nextRunAtMs": future_ms + i},
                "createdAtMs": 1_700_000_000_000 + i,
                "enabled": True,
            }
            for i in range(_AT_CRON_HARD_CAP + 5)  # 55 pending
        ]
        mock_invoke.side_effect = lambda tenant, tool, args: (
            {"details": {"jobs": jobs}} if tool == "cron.list" else None
        )
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["cap_reaped"], 0)
        self.assertEqual(result["at_pending"], _AT_CRON_HARD_CAP + 5)
        log = PlatformIssueLog.objects.get(tenant=self.tenant, category=PlatformIssueLog.Category.RATE_LIMIT)
        self.assertEqual(log.severity, PlatformIssueLog.Severity.MEDIUM)
        self.assertFalse(log.resolved)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_catastrophic_cap_reaps_newest_first(self, mock_invoke):
        """At 200+ pending at-crons: reap newest-first back to 200 and
        log severity HIGH. The user's older queue is preserved."""
        import time

        from apps.orchestrator.cron_reconcile import (
            _AT_CRON_CATASTROPHIC_CAP,
            regenerate_tenant_crons,
        )
        from apps.platform_logs.models import PlatformIssueLog

        future_ms = int(time.time() * 1000) + 60 * 60 * 1000
        excess = 7
        jobs = [
            {
                "name": f"reminder-{i}",
                "id": f"at-{i}",
                "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
                "state": {"nextRunAtMs": future_ms + i},
                # ``createdAtMs`` ascends with i — so the highest-i jobs are newest.
                "createdAtMs": 1_700_000_000_000 + i,
                "enabled": True,
            }
            for i in range(_AT_CRON_CATASTROPHIC_CAP + excess)
        ]
        remove_calls: list[str] = []

        def _stub(tenant, tool, args):
            if tool == "cron.list":
                return {"details": {"jobs": jobs}}
            if tool == "cron.remove":
                remove_calls.append(args["jobId"])
            return None

        mock_invoke.side_effect = _stub
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["cap_reaped"], excess)
        # Reaped ids are the newest (highest indices).
        expected_reaped = {f"at-{i}" for i in range(_AT_CRON_CATASTROPHIC_CAP, _AT_CRON_CATASTROPHIC_CAP + excess)}
        self.assertEqual(set(remove_calls), expected_reaped)
        log = PlatformIssueLog.objects.get(tenant=self.tenant, category=PlatformIssueLog.Category.RATE_LIMIT)
        self.assertEqual(log.severity, PlatformIssueLog.Severity.HIGH)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_cap_breach_log_is_deduped(self, mock_invoke):
        """A pre-existing unresolved RATE_LIMIT log suppresses re-logging
        — the existing entry IS the still-ongoing marker."""
        import time

        from apps.orchestrator.cron_reconcile import _AT_CRON_HARD_CAP, regenerate_tenant_crons
        from apps.platform_logs.models import PlatformIssueLog

        PlatformIssueLog.objects.create(
            tenant=self.tenant,
            category=PlatformIssueLog.Category.RATE_LIMIT,
            severity=PlatformIssueLog.Severity.MEDIUM,
            tool_name="cron.add",
            summary="prior breach",
        )
        future_ms = int(time.time() * 1000) + 60 * 60 * 1000
        jobs = [
            {
                "name": f"reminder-{i}",
                "id": f"at-{i}",
                "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
                "state": {"nextRunAtMs": future_ms + i},
                "createdAtMs": 1_700_000_000_000 + i,
                "enabled": True,
            }
            for i in range(_AT_CRON_HARD_CAP + 5)
        ]
        mock_invoke.side_effect = lambda tenant, tool, args: (
            {"details": {"jobs": jobs}} if tool == "cron.list" else None
        )
        regenerate_tenant_crons(self.tenant)
        self.assertEqual(
            PlatformIssueLog.objects.filter(tenant=self.tenant, category=PlatformIssueLog.Category.RATE_LIMIT).count(),
            1,
        )

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_resolved_log_does_not_block_new_breach(self, mock_invoke):
        """If the prior cap-breach log was resolved, a new breach should
        create a fresh entry — the resolved log is no longer the active
        marker."""
        import time

        from apps.orchestrator.cron_reconcile import _AT_CRON_HARD_CAP, regenerate_tenant_crons
        from apps.platform_logs.models import PlatformIssueLog

        PlatformIssueLog.objects.create(
            tenant=self.tenant,
            category=PlatformIssueLog.Category.RATE_LIMIT,
            severity=PlatformIssueLog.Severity.MEDIUM,
            tool_name="cron.add",
            summary="prior breach",
            resolved=True,
        )
        future_ms = int(time.time() * 1000) + 60 * 60 * 1000
        jobs = [
            {
                "name": f"reminder-{i}",
                "id": f"at-{i}",
                "schedule": {"kind": "at", "at": "2099-01-01T00:00:00Z"},
                "state": {"nextRunAtMs": future_ms + i},
                "createdAtMs": 1_700_000_000_000 + i,
                "enabled": True,
            }
            for i in range(_AT_CRON_HARD_CAP + 5)
        ]
        mock_invoke.side_effect = lambda tenant, tool, args: (
            {"details": {"jobs": jobs}} if tool == "cron.list" else None
        )
        regenerate_tenant_crons(self.tenant)
        self.assertEqual(
            PlatformIssueLog.objects.filter(
                tenant=self.tenant,
                category=PlatformIssueLog.Category.RATE_LIMIT,
            ).count(),
            2,
        )


class RuntimeContainerStartedTest(TestCase):
    """The container-start hook fires the reconciler immediately."""

    def setUp(self):
        from django.test import override_settings as _ovr

        self.user, self.tenant = _create_user_and_tenant()
        self.tenant.postgres_cron_canonical = True
        self.tenant.container_fqdn = "oc-test.example.com"
        self.tenant.save(update_fields=["postgres_cron_canonical", "container_fqdn"])
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        self._override = _ovr(NBHD_INTERNAL_API_KEY="test-internal-key")
        self._override.enable()
        # Phase 1d: the global internal-key fallback is gone, so the target
        # tenant must carry its own internal_api_key. Seed it after the
        # override is live so it matches the header the client sends.
        seed_internal_key(self.tenant)

    def tearDown(self):
        self._override.disable()

    @patch("apps.orchestrator.cron_reconcile.regenerate_tenant_crons")
    def test_hook_invokes_reconciler(self, mock_regen):
        mock_regen.return_value = {"added": 0, "removed": 0, "unchanged": 0, "errors": 0}
        resp = self.client.post(
            f"/api/cron/runtime/{self.tenant.id}/container-started/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        mock_regen.assert_called_once()

    @patch("apps.orchestrator.cron_reconcile.regenerate_tenant_crons")
    def test_hook_skipped_when_flag_off(self, mock_regen):
        self.tenant.postgres_cron_canonical = False
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        resp = self.client.post(
            f"/api/cron/runtime/{self.tenant.id}/container-started/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("skipped"))
        mock_regen.assert_not_called()

    def test_hook_unauthorized(self):
        resp = self.client.post(f"/api/cron/runtime/{self.tenant.id}/container-started/")
        self.assertEqual(resp.status_code, 401)
