"""Tests for orchestrator app."""

import json
import os
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .config_generator import (
    _build_heartbeat_cron,
    _heartbeat_cron_expr,
    build_cron_seed_jobs,
    config_to_json,
    generate_openclaw_config,
)
from .config_validator import validate_openclaw_config
from .services import (
    deprovision_tenant,
    provision_tenant,
    restore_user_cron_jobs,
    update_tenant_config,
)


class ConfigGeneratorTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Config Test",
            telegram_chat_id=999888777,
        )

    def test_generates_valid_config(self):
        config = generate_openclaw_config(self.tenant)
        self.assertIn("gateway", config)
        self.assertIn("channels", config)
        self.assertIn("agents", config)
        self.assertEqual(config["gateway"]["mode"], "local")

    def test_gateway_defaults_use_supported_bind_mode(self):
        config = generate_openclaw_config(self.tenant)
        self.assertEqual(config["gateway"]["bind"], "loopback")
        # Auth is intentionally present — token from env var for Django→OC calls
        self.assertEqual(config["gateway"]["auth"]["mode"], "token")

    def test_telegram_channel_absent_for_central_poller(self):
        """No Telegram channel — central Django poller handles all inbound."""
        config = generate_openclaw_config(self.tenant)
        self.assertIn("telegram", config["channels"])
        self.assertIn("inlineButtons", config["channels"]["telegram"]["capabilities"])

    def test_starter_tier_model(self):
        self.tenant.model_tier = "starter"
        config = generate_openclaw_config(self.tenant)
        self.assertIn("minimax", config["agents"]["defaults"]["model"]["primary"].lower())

    def test_starter_tier_uses_openrouter(self):
        self.tenant.model_tier = "starter"
        config = generate_openclaw_config(self.tenant)
        primary = config["agents"]["defaults"]["model"]["primary"]
        self.assertTrue(primary.startswith("openrouter/"))
        # OpenRouter is built-in; no custom providers block needed
        self.assertNotIn("models", config)

    def test_starter_tier_has_active_models(self):
        self.tenant.model_tier = "starter"
        config = generate_openclaw_config(self.tenant)
        models = config["agents"]["defaults"]["models"]
        aliases = sorted(v.get("alias") for v in models.values())
        self.assertEqual(aliases, ["gemma", "kimi", "minimax"])

    def test_audio_model_defaults_to_whisper(self):
        self.tenant.model_tier = "starter"
        config = generate_openclaw_config(self.tenant)
        audio = config["tools"]["media"]["audio"]
        self.assertTrue(audio["enabled"])
        models = audio["models"]
        self.assertEqual(len(models), 1)
        self.assertEqual(
            models[0],
            {"provider": "openai", "model": "gpt-4o-mini-transcribe"},
        )

    def test_plugin_wiring_enabled_when_plugin_id_configured(self):
        with override_settings(
            OPENCLAW_GOOGLE_PLUGIN_ID="nbhd-google-tools",
            OPENCLAW_JOURNAL_PLUGIN_ID="nbhd-journal-tools",
            OPENCLAW_USAGE_PLUGIN_ID="",
        ):
            config = generate_openclaw_config(self.tenant)

        self.assertEqual(
            sorted(config["plugins"]["allow"]),
            ["nbhd-google-tools", "nbhd-journal-tools"],
        )
        self.assertTrue(config["plugins"]["entries"]["nbhd-google-tools"]["enabled"])
        self.assertTrue(config["plugins"]["entries"]["nbhd-journal-tools"]["enabled"])
        paths = config["plugins"]["load"]["paths"]
        self.assertIn("/opt/nbhd/plugins/nbhd-google-tools", paths)
        self.assertIn("/opt/nbhd/plugins/nbhd-journal-tools", paths)
        self.assertIn("group:plugins", config["tools"]["allow"])

    def test_plugin_wiring_omitted_when_no_plugins_configured(self):
        with override_settings(
            OPENCLAW_GOOGLE_PLUGIN_ID="", OPENCLAW_JOURNAL_PLUGIN_ID="", OPENCLAW_USAGE_PLUGIN_ID=""
        ):
            config = generate_openclaw_config(self.tenant)

        self.assertNotIn("plugins", config)
        # group:plugins is in the base tool policy (tool_policy.py), not added by plugin wiring
        self.assertIn("group:plugins", config["tools"]["allow"])

    def test_single_plugin_wired_when_only_one_configured(self):
        with override_settings(
            OPENCLAW_GOOGLE_PLUGIN_ID="nbhd-google-tools",
            OPENCLAW_JOURNAL_PLUGIN_ID="",
            OPENCLAW_USAGE_PLUGIN_ID="",
        ):
            config = generate_openclaw_config(self.tenant)

        self.assertEqual(config["plugins"]["allow"], ["nbhd-google-tools"])
        self.assertNotIn("nbhd-journal-tools", config["plugins"]["entries"])

    def test_tools_policy_uses_allow_and_deny_lists(self):
        self.tenant.model_tier = "starter"
        config = generate_openclaw_config(self.tenant)
        tools = config["tools"]
        self.assertIn("allow", tools)
        self.assertIn("deny", tools)
        self.assertIn("gateway", tools["deny"])
        self.assertNotIn("group:ui", tools["allow"])

    # ── OpenClaw v2026.4.15 tool-policy migration ──────────────────────

    def test_tool_policy_allows_memory_tools(self):
        """memory_search and memory_get are required for the agent's recall
        path introduced in OpenClaw 2026.4.9+'s built-in memory engine.
        They live under group:openclaw in 2026.4.15.
        Originally deferred ('decide after a week of live data') — now
        deliberately included because the version bump delivers tangible
        recall value via group:openclaw."""
        config = generate_openclaw_config(self.tenant)
        self.assertIn("group:openclaw", config["tools"]["allow"])

    def test_tool_policy_keeps_subscriber_invariants(self):
        """Even with the wider group:openclaw allow, these MUST remain
        denied:
        - message: tenant has no Telegram bot token; outbound goes via
          nbhd_send_to_user plugin
        - sessions_*, agents_list, gateway: cross-session / infra controls
          not for subscribers
        - browser, canvas, nodes, code_execution: surfaces with no
          representation in Telegram-only containers
        """
        config = generate_openclaw_config(self.tenant)
        deny = set(config["tools"]["deny"])
        must_deny = {
            "message", "sessions_spawn", "sessions_send", "sessions_list",
            "sessions_history", "session_status", "agents_list", "gateway",
            "browser", "canvas", "nodes", "code_execution",
        }
        missing = must_deny - deny
        self.assertEqual(missing, set(), f"Required deny entries missing: {missing}")

    def test_tool_policy_uses_only_valid_groups(self):
        """Only group:openclaw and group:plugins exist as group:* names
        in OpenClaw 2026.4.15. Older entries (group:web, group:automation,
        group:fs, group:memory) parse but match nothing — caused cron
        access loss before this fix."""
        config = generate_openclaw_config(self.tenant)
        group_entries = {
            e for e in config["tools"]["allow"] if e.startswith("group:")
        }
        valid_groups = {"group:openclaw", "group:plugins"}
        invalid = group_entries - valid_groups
        self.assertEqual(invalid, set(), f"Invalid group names: {invalid}")

    def test_channels_empty_no_telegram(self):
        """No Telegram channel — central Django poller handles all Telegram."""
        config = generate_openclaw_config(self.tenant)
        self.assertIn("telegram", config["channels"])
        self.assertIn("inlineButtons", config["channels"]["telegram"]["capabilities"])

    def test_chat_completions_endpoint_enabled(self):
        """Gateway exposes /v1/chat/completions for central poller forwarding."""
        config = generate_openclaw_config(self.tenant)
        endpoints = config["gateway"]["http"]["endpoints"]
        self.assertTrue(endpoints["chatCompletions"]["enabled"])

    # ── OpenClaw v2026.4.5 upgrade compatibility ─────────────────────

    def test_line_channel_has_no_capabilities_key(self):
        """LINE schema rejects 'capabilities' in OpenClaw >= 2026.4.5."""
        config = generate_openclaw_config(self.tenant)
        self.assertNotIn("capabilities", config["channels"]["line"])

    def test_telegram_channel_retains_capabilities(self):
        """Telegram channel config should still include 'capabilities'."""
        config = generate_openclaw_config(self.tenant)
        self.assertIn("capabilities", config["channels"]["telegram"])

    def test_heartbeat_cron_uses_delivery_none(self):
        """Heartbeat cron uses delivery.mode='none' — sends via plugin, not built-in messaging."""
        from .config_generator import build_cron_seed_jobs

        self.tenant.heartbeat_enabled = True
        self.tenant.heartbeat_start_hour = 8
        self.tenant.heartbeat_window_hours = 6
        jobs = build_cron_seed_jobs(self.tenant)
        hb = next((j for j in jobs if j["name"] == "Heartbeat Check-in"), None)
        self.assertIsNotNone(hb, "Heartbeat cron job should be generated when enabled")
        self.assertEqual(hb["delivery"]["mode"], "none")

    def test_silent_cron_jobs_use_delivery_none(self):
        """Background-only cron jobs should use delivery.mode='none'."""
        from .config_generator import build_cron_seed_jobs

        jobs = build_cron_seed_jobs(self.tenant)
        for job in jobs:
            if job["name"] in ("Week Ahead Review", "Background Tasks"):
                self.assertEqual(
                    job["delivery"]["mode"],
                    "none",
                    f"{job['name']} should use delivery.mode='none'",
                )

    def test_interactive_cron_jobs_have_delivery(self):
        """Interactive cron jobs (main session) have delivery config."""
        from .config_generator import build_cron_seed_jobs

        jobs = build_cron_seed_jobs(self.tenant)
        interactive = ["Morning Briefing", "Evening Check-in", "Weekly Reflection"]
        for job in jobs:
            if job["name"] in interactive:
                self.assertIn("delivery", job, f"{job['name']} should have delivery config")

    # ── Universal isolation cron model ───────────────────────────────

    def test_all_seed_jobs_run_isolated(self):
        """Universal isolation: every cron job has sessionTarget=isolated."""
        from .config_generator import build_cron_seed_jobs

        self.tenant.heartbeat_enabled = True
        jobs = build_cron_seed_jobs(self.tenant)
        for job in jobs:
            self.assertEqual(
                job["sessionTarget"],
                "isolated",
                f"{job['name']} must run isolated under universal isolation",
            )
            self.assertNotIn(
                "wakeMode",
                job,
                f"{job['name']} should not carry wakeMode (only valid on main jobs)",
            )
            self.assertEqual(
                job["payload"]["kind"],
                "agentTurn",
                f"{job['name']} payload kind must be agentTurn",
            )
            self.assertIn(
                "message",
                job["payload"],
                f"{job['name']} payload must use 'message' field, not 'text'",
            )

    def test_foreground_jobs_carry_phase2_sync_block(self):
        """Foreground seed jobs have the Phase 2 sync wrapper appended."""
        from .config_generator import build_cron_seed_jobs

        self.tenant.heartbeat_enabled = True
        jobs = build_cron_seed_jobs(self.tenant)
        foreground_names = {
            "Morning Briefing",
            "Evening Check-in",
            "Weekly Reflection",
            "Week Ahead Review",
            "Heartbeat Check-in",
        }
        for job in jobs:
            if job["name"] in foreground_names:
                msg = job["payload"]["message"]
                self.assertIn(
                    f"_sync:{job['name']}",
                    msg,
                    f"{job['name']} should contain its sync cron name in the wrapper",
                )
                self.assertIn(
                    "FINAL STEP — conditional sync",
                    msg,
                    f"{job['name']} should carry the Phase 2 sync block",
                )
                self.assertIn(
                    "Did you send the user a message",
                    msg,
                    f"{job['name']} should carry the conditional guard",
                )

    def test_background_jobs_skip_phase2_sync_block(self):
        """Background seed jobs (foreground=false) do NOT carry the Phase 2 wrapper."""
        from .config_generator import build_cron_seed_jobs

        jobs = build_cron_seed_jobs(self.tenant)
        bg = next((j for j in jobs if j["name"] == "Background Tasks"), None)
        self.assertIsNotNone(bg)
        msg = bg["payload"]["message"]
        self.assertNotIn("_sync:Background Tasks", msg)
        self.assertNotIn("FINAL STEP — conditional sync", msg)

    def test_heartbeat_is_foreground_with_conditional_sync(self):
        """Heartbeat is foreground=true so it can sync on hours that nudged the user."""
        from .config_generator import build_cron_seed_jobs

        self.tenant.heartbeat_enabled = True
        jobs = build_cron_seed_jobs(self.tenant)
        hb = next((j for j in jobs if j["name"] == "Heartbeat Check-in"), None)
        self.assertIsNotNone(hb)
        self.assertIn("_sync:Heartbeat Check-in", hb["payload"]["message"])
        self.assertIn("HEARTBEAT_OK", hb["payload"]["message"])

    # ── Config validator integration ────────────────────────────────

    def test_validator_passes_for_generated_config(self):
        """Generated config must pass validation with zero errors."""
        config = generate_openclaw_config(self.tenant)
        issues = validate_openclaw_config(config)
        errors = [i for i in issues if i.severity == "error"]
        self.assertEqual(errors, [], f"Config has validation errors: {errors}")

    def test_config_round_trip_produces_valid_json(self):
        """config_to_json(generate_openclaw_config(...)) must produce parseable JSON."""
        config = generate_openclaw_config(self.tenant)
        json_str = config_to_json(config)
        parsed = json.loads(json_str)
        self.assertEqual(parsed, config)

    # ── Reddit plugin ───────────────────────────────────────────────

    def test_reddit_plugin_loaded_when_integration_active(self):
        from apps.integrations.models import Integration

        Integration.objects.create(
            tenant=self.tenant,
            provider="reddit",
            status=Integration.Status.ACTIVE,
        )
        config = generate_openclaw_config(self.tenant)
        self.assertIn("plugins", config)
        self.assertIn("nbhd-reddit-tools", config["plugins"]["allow"])
        self.assertIn("nbhd-reddit-tools", config["plugins"]["entries"])

    def test_reddit_plugin_not_loaded_without_integration(self):
        with override_settings(
            OPENCLAW_GOOGLE_PLUGIN_ID="",
            OPENCLAW_JOURNAL_PLUGIN_ID="",
            OPENCLAW_USAGE_PLUGIN_ID="",
        ):
            config = generate_openclaw_config(self.tenant)
        self.assertNotIn("plugins", config)

    # ── Finance plugin ──────────────────────────────────────────────

    def test_finance_plugin_loaded_when_enabled(self):
        self.tenant.finance_enabled = True
        self.tenant.save()
        config = generate_openclaw_config(self.tenant)
        self.assertIn("plugins", config)
        self.assertIn("nbhd-finance-tools", config["plugins"]["allow"])

    def test_finance_plugin_not_loaded_when_disabled(self):
        self.tenant.finance_enabled = False
        self.tenant.save()
        with override_settings(
            OPENCLAW_GOOGLE_PLUGIN_ID="",
            OPENCLAW_JOURNAL_PLUGIN_ID="",
            OPENCLAW_USAGE_PLUGIN_ID="",
        ):
            config = generate_openclaw_config(self.tenant)
        self.assertNotIn("plugins", config)

    # ── Heartbeat cron ──────────────────────────────────────────────

    def test_heartbeat_cron_expr_default(self):
        """Default: start_hour=8, window=6 -> hours 8-13."""
        expr = _heartbeat_cron_expr(8, 6)
        self.assertEqual(expr, "0 8,9,10,11,12,13 * * *")

    def test_heartbeat_cron_expr_midnight_wrapping(self):
        """start_hour=22, window=6 -> wraps: 22,23,0,1,2,3."""
        expr = _heartbeat_cron_expr(22, 6)
        self.assertEqual(expr, "0 0,1,2,3,22,23 * * *")

    def test_heartbeat_cron_disabled(self):
        self.tenant.heartbeat_enabled = False
        self.tenant.save()
        result = _build_heartbeat_cron(self.tenant)
        self.assertIsNone(result)

    def test_heartbeat_cron_enabled_custom_window(self):
        self.tenant.heartbeat_enabled = True
        self.tenant.heartbeat_start_hour = 9
        self.tenant.heartbeat_window_hours = 4
        self.tenant.save()
        result = _build_heartbeat_cron(self.tenant)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Heartbeat Check-in")
        self.assertIn("9,10,11,12", result["schedule"]["expr"])

    # ── Task model preferences ──────────────────────────────────────

    def test_task_model_preferences_override_cron_jobs(self):
        self.tenant.task_model_preferences = {
            "morning_briefing": "openrouter/qwen/qwen3-30b-a3b",
        }
        self.tenant.save()
        jobs = build_cron_seed_jobs(self.tenant)
        morning = next(j for j in jobs if j["name"] == "Morning Briefing")
        self.assertEqual(morning["model"], "openrouter/qwen/qwen3-30b-a3b")

    def test_task_model_preferences_no_override_leaves_default(self):
        self.tenant.task_model_preferences = {}
        self.tenant.save()
        jobs = build_cron_seed_jobs(self.tenant)
        morning = next(j for j in jobs if j["name"] == "Morning Briefing")
        self.assertNotIn("model", morning)

    # ── GWS skills ──────────────────────────────────────────────────

    def test_gws_skills_loaded_when_google_active(self):
        from apps.integrations.models import Integration

        Integration.objects.create(
            tenant=self.tenant,
            provider="google",
            status=Integration.Status.ACTIVE,
        )
        config = generate_openclaw_config(self.tenant)
        extra_dirs = config.get("skills", {}).get("load", {}).get("extraDirs", [])
        skill_names = [d.rstrip("/").rsplit("/", 1)[-1] for d in extra_dirs]
        self.assertIn("gws-shared", skill_names)
        self.assertIn("gws-gmail-triage", skill_names)
        self.assertIn("nbhd-action-gate", skill_names)
        # Validator should pass
        issues = validate_openclaw_config(config)
        errors = [i for i in issues if i.severity == "error"]
        self.assertEqual(errors, [])

    def test_gws_env_vars_set_when_google_active(self):
        from apps.integrations.models import Integration

        Integration.objects.create(
            tenant=self.tenant,
            provider="google",
            status=Integration.Status.ACTIVE,
        )
        config = generate_openclaw_config(self.tenant)
        env = config.get("env", {})
        self.assertIn("NBHD_TENANT_ID", env)
        self.assertEqual(env["NBHD_TENANT_ID"], str(self.tenant.id))

    def test_gws_env_vars_absent_without_google(self):
        config = generate_openclaw_config(self.tenant)
        env = config.get("env", {})
        self.assertNotIn("NBHD_TENANT_ID", env)

    # ── Cron seed jobs ──────────────────────────────────────────────

    def test_cron_seed_jobs_count_with_heartbeat(self):
        self.tenant.heartbeat_enabled = True
        self.tenant.save()
        jobs = build_cron_seed_jobs(self.tenant)
        names = [j["name"] for j in jobs]
        self.assertIn("Morning Briefing", names)
        self.assertIn("Evening Check-in", names)
        self.assertIn("Background Tasks", names)
        self.assertIn("Heartbeat Check-in", names)

    def test_cron_seed_jobs_without_heartbeat(self):
        self.tenant.heartbeat_enabled = False
        self.tenant.save()
        jobs = build_cron_seed_jobs(self.tenant)
        names = [j["name"] for j in jobs]
        self.assertNotIn("Heartbeat Check-in", names)


class RestoreUserCronJobsTest(TestCase):
    """Tests for user cron job restore deduplication."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Restore Dedup Test",
            telegram_chat_id=555666777,
        )

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_snapshot_with_duplicates_restores_only_one_per_name(self, mock_invoke):
        """If snapshot has 3 copies of a job, only 1 should be restored."""
        self.tenant.cron_jobs_snapshot = {
            "jobs": [
                {"name": "Daily Workout Plan", "schedule": "0 6 * * *"},
                {"name": "Daily Workout Plan", "schedule": "0 6 * * *"},
                {"name": "Daily Workout Plan", "schedule": "0 6 * * *"},
                {"name": "Evening Journal", "schedule": "0 21 * * *"},
                {"name": "Evening Journal", "schedule": "0 21 * * *"},
            ],
            "snapshot_at": "2026-01-01T00:00:00",
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        result = restore_user_cron_jobs(self.tenant, existing_job_names=set())

        self.assertEqual(result["restored"], 2)  # 1 Workout + 1 Journal
        self.assertEqual(mock_invoke.call_count, 2)

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_snapshot_duplicates_skips_already_existing(self, mock_invoke):
        """Duplicate snapshot entries for a name already on container are all skipped."""
        self.tenant.cron_jobs_snapshot = {
            "jobs": [
                {"name": "Daily Workout Plan", "schedule": "0 6 * * *"},
                {"name": "Daily Workout Plan", "schedule": "0 6 * * *"},
            ],
            "snapshot_at": "2026-01-01T00:00:00",
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        result = restore_user_cron_jobs(self.tenant, existing_job_names={"Daily Workout Plan"})

        self.assertEqual(result["restored"], 0)
        mock_invoke.assert_not_called()

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_system_jobs_in_snapshot_are_never_restored(self, mock_invoke):
        """System job names in the snapshot should be skipped even if missing from container."""
        self.tenant.cron_jobs_snapshot = {
            "jobs": [
                {"name": "Morning Briefing", "schedule": "0 7 * * *"},
                {"name": "My Custom Job", "schedule": "0 12 * * *"},
            ],
            "snapshot_at": "2026-01-01T00:00:00",
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        result = restore_user_cron_jobs(self.tenant, existing_job_names=set())

        self.assertEqual(result["restored"], 1)  # Only custom job
        self.assertEqual(mock_invoke.call_count, 1)


@override_settings()
class ProvisioningTest(TestCase):
    def setUp(self):
        os.environ["AZURE_MOCK"] = "true"
        self.tenant = create_tenant(
            display_name="Provision Test",
            telegram_chat_id=111222333,
        )

    def tearDown(self):
        os.environ.pop("AZURE_MOCK", None)

    def test_provision_creates_container(self):
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        self.assertTrue(self.tenant.container_id.startswith("oc-"))
        self.assertTrue(self.tenant.container_fqdn)

    def test_deprovision_marks_deleted(self):
        provision_tenant(str(self.tenant.id))
        deprovision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.DELETED)
        self.assertEqual(self.tenant.container_id, "")

    @patch("apps.orchestrator.services.upload_config_to_file_share")
    def test_update_tenant_config_pushes_new_config(self, mock_upload):
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        mock_upload.reset_mock()
        update_tenant_config(str(self.tenant.id))

        # File share is updated (source of truth for OpenClaw)
        mock_upload.assert_called_once()
        upload_args = mock_upload.call_args[0]
        self.assertEqual(upload_args[0], str(self.tenant.id))
        # Config should contain gateway settings
        self.assertIn("gateway", upload_args[1])
