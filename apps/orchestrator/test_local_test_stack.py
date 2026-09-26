"""Real DB/config/storage contracts for the opt-in local adapter; no LLM calls."""

import json
import os
import runpy
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from django.conf import settings
from django.test import TestCase, override_settings

from apps.tenants.models import Tenant, User

from . import azure_client
from .config_generator import generate_openclaw_config
from .local_test import local_root, share_path


@override_settings(DEBUG=True)
class LocalStackTests(TestCase):
    def setUp(self):
        (settings.BASE_DIR / "deploy/local-test/.state/tmp").mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=settings.BASE_DIR / "deploy/local-test/.state/tmp")
        self.addCleanup(self.directory.cleanup)
        self.user = User.objects.create(email="local-stack-fixture@example.invalid")
        self.tenant = Tenant.objects.create(
            user=self.user, is_synthetic=True, is_eval_sink=False, openclaw_version="2026.9.1", sautai_enabled=True
        )
        self.overrides = override_settings(LOCAL_TEST_ROOT=self.directory.name)
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)
        self.env = patch.dict(
            os.environ,
            AZURE_MOCK="true",
            NBHD_TENANT_ID=str(self.tenant.id),
            LOCAL_TEST_MODEL="qwen3.8:27b-obliterated-q8",
            LOCAL_TEST_KEK_SEED="fixture-only",
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_storage_sanitizes_and_refuses_escape(self):
        azure_client.upload_workspace_file(str(self.tenant.id), "workspace/USER.md", "fixture\x00text")
        self.assertEqual(azure_client.download_workspace_file(str(self.tenant.id), "workspace/USER.md"), "fixturetext")
        with self.assertRaises(ValueError):
            share_path(self.tenant.id, "../outside")
        self.tenant.is_synthetic = False
        self.tenant.save(update_fields=["is_synthetic"])
        with self.assertRaises(ValueError):
            local_root(self.tenant.id)
        with override_settings(DEBUG=False), self.assertRaises(RuntimeError):
            local_root(self.tenant.id)

    def test_handoff_survives_disconnected_probe_and_rejects_invalid_host(self):
        handoff = runpy.run_path(str(settings.BASE_DIR / "deploy/local-test/handoff.py"))
        listener = handoff["start_listener"](Path(self.directory.name))
        self.addCleanup(listener.close)
        path = str(Path(self.directory.name) / "sautai-handoff.sock")
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(path)
        # A second request must receive a rejection even after the first client
        # disconnected without a payload. No token or DB link is manufactured.
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(path)
            client.sendall(b'{"base_url":"https://invalid.example"}\n')
            self.assertEqual(json.loads(client.recv(1024)), {"accepted": False})

    @override_settings(SAUTAI_PLATFORM_SECRET="")
    def test_handoff_secret_only_leaves_integration_untouched(self):
        from apps.integrations.models import Integration

        accept = runpy.run_path(str(settings.BASE_DIR / "deploy/local-test/handoff.py"))["accept_payload"]
        payload = {"base_url": "http://127.0.0.1:8000", "platform_secret": "fixture-secret"}
        accept(payload)
        self.assertEqual(settings.SAUTAI_PLATFORM_SECRET, "fixture-secret")
        self.assertFalse(Integration.objects.filter(tenant=self.tenant).exists())
        accept({**payload, "sautai_user_id": 42})
        row = Integration.objects.get(tenant=self.tenant)
        before = (row.sautai_user_id, row.linked_at, row.updated_at)
        accept({**payload, "sautai_user_id": None})
        row.refresh_from_db()
        self.assertEqual((row.sautai_user_id, row.linked_at, row.updated_at), before)

    @override_settings(SAUTAI_PLATFORM_SECRET="unchanged")
    def test_handoff_rejects_invalid_shapes_without_writes(self):
        from apps.integrations.models import Integration

        accept = runpy.run_path(str(settings.BASE_DIR / "deploy/local-test/handoff.py"))["accept_payload"]
        payload = {"base_url": "http://127.0.0.1:8000", "platform_secret": "fixture-secret"}
        invalid = [None, [], {}, {**payload, "base_url": "https://invalid.example"}]
        invalid += [{**payload, "sautai_user_id": value} for value in (True, False, 0, -1, 1.5, "1", [], {})]
        invalid += [{**payload, "platform_secret": value} for value in (None, "", 42)]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                accept(value)
        self.assertEqual(settings.SAUTAI_PLATFORM_SECRET, "unchanged")
        self.assertFalse(Integration.objects.filter(tenant=self.tenant).exists())

    def test_persona_import_accepts_harness_manifest_and_refuses_wrong_version(self):
        from django.core.management.base import CommandError

        from .management.commands.prepare_local_test_tenant import persona_facts

        manifest = {
            "persona": "yuki",
            "persona_version": "3",
            "facts": [{"id": "Y-001", "citation": "fixture:1", "text": "Synthetic fixture."}],
        }
        self.assertEqual(persona_facts(manifest), ["Y-001: Synthetic fixture. (source: fixture:1)"])
        manifest["persona_version"] = "2"
        with self.assertRaises(CommandError):
            persona_facts(manifest)

    def test_mock_key_survives_process_registry_reset(self):
        tid = str(self.tenant.id)
        azure_client.create_tenant_kek(tid)
        wrapped, _ = azure_client.wrap_dek(tid, b"fixture-data")
        azure_client._MOCK_KEK_REGISTRY.pop(tid)
        self.assertEqual(azure_client.unwrap_dek(tid, wrapped), b"fixture-data")
        self.assertEqual(azure_client.kek_liveness(tid), "live")

    def test_generated_config_uses_same_validated_share_apply(self):
        config = generate_openclaw_config(self.tenant)
        self.assertEqual(config["gateway"]["bind"], "loopback")
        self.assertEqual(config["gateway"]["port"], 19443)
        provider = config["models"]["providers"]["ollama"]
        self.assertEqual(provider["baseUrl"], "http://127.0.0.1:11434/v1")
        self.assertEqual(provider["api"], "openai-completions")
        self.assertIs(provider["injectNumCtxForOpenAICompat"], False)
        self.assertNotIn("num_ctx", json.dumps(config))
        self.assertIn("nbhd-sautai-tools", config["plugins"]["entries"])
        self.assertEqual(config["agents"]["defaults"]["model"]["fallbacks"], [])
        for path in config["plugins"]["load"]["paths"]:
            self.assertTrue(Path(path).is_dir(), path)
        azure_client.upload_config_to_file_share(str(self.tenant.id), json.dumps(config))
        path = share_path(self.tenant.id, "openclaw.json")
        self.assertEqual(json.loads(path.read_text()), config)
        # Real installed 2026.9.1 schema check, isolated from both gateways.
        env = {
            **os.environ,
            "HOME": self.directory.name,
            "OPENCLAW_HOME": self.directory.name,
            "OPENCLAW_STATE_DIR": self.directory.name + "/state",
            "OPENCLAW_CONFIG_PATH": str(path),
            "OPENCLAW_DEBUG": "1",
        }
        if not Path("/Users/mjjones/.local/bin/openclaw").exists():
            return  # The portable config-validator gate above still runs in Linux CI.
        result = subprocess.run(
            ["/Users/mjjones/.local/bin/openclaw", "config", "validate"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        if Path("/usr/bin/sandbox-exec").exists():
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 19443)) == 0:
                    self.skipTest("Designated test gateway port already occupied; never replace it")
            profile = settings.BASE_DIR / "deploy/local-test/gateway.sb"
            # Boot only: no chat, no inference, no cron jobs, no account signup.
            with tempfile.TemporaryFile() as logs:
                process = subprocess.Popen(
                    [
                        "/usr/bin/sandbox-exec",
                        "-f",
                        str(profile),
                        "/Users/mjjones/.local/bin/openclaw",
                        "gateway",
                        "run",
                    ],
                    env=env,
                    stdout=logs,
                    stderr=logs,
                )
                try:
                    ready = False
                    for _ in range(40):
                        if process.poll() is not None:
                            break
                        try:
                            with httpx.Client(trust_env=False, follow_redirects=False, timeout=1) as client:
                                response = client.get("http://127.0.0.1:19443/health")
                            ready = response.status_code == 200
                        except httpx.HTTPError:
                            pass
                        if ready:
                            break
                        time.sleep(0.5)
                    if not ready:
                        logs.seek(0)
                        diagnostics = logs.read().decode(errors="replace")
                        for value in env.values():
                            if len(value) > 20:
                                diagnostics = diagnostics.replace(value, "[env]")
                        self.fail("Isolated gateway failed boot: " + diagnostics[-4000:])
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    def test_local_qstash_fallback_waits_for_commit_and_honors_delay(self):
        from apps.cron.publish import publish_task
        from apps.cron.views import TASK_MAP

        called = threading.Event()
        with (
            override_settings(QSTASH_TOKEN=""),
            patch("apps.cron.views.execute_task_sync", side_effect=lambda *a, **k: called.set()) as execute,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                publish_task("generate_sautai_meal_plan", "fixture-job", delay_seconds=0.1)
                self.assertFalse(called.is_set())
            self.assertFalse(called.wait(0.02))
            self.assertTrue(called.wait(2))
            execute.assert_called_once_with(TASK_MAP["generate_sautai_meal_plan"], "fixture-job")

    def test_normal_mock_provision_path_creates_real_local_config_and_key(self):
        from apps.crypto.keys import unwrap_dek_for
        from apps.orchestrator.services import provision_tenant

        provision_tenant(str(self.tenant.id), send_first_session_welcome=False)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        self.assertEqual(self.tenant.internal_api_key, settings.NBHD_INTERNAL_API_KEY)
        self.assertEqual(len(unwrap_dek_for(self.tenant)), 32)
        config = json.loads(share_path(self.tenant.id, "openclaw.json").read_text())
        self.assertEqual(config["gateway"]["port"], 19443)
        self.assertIn("nbhd-sautai-tools", config["plugins"]["entries"])


@override_settings(DEBUG=True)
class YukiLocalTests(TestCase):
    def setUp(self):
        import io
        from unittest.mock import MagicMock

        from .management.commands import yuki_local

        self.helper = yuki_local
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.user = User.objects.create(email="yuki@example.com")
        self.tenant = Tenant.objects.create(user=self.user, is_synthetic=True, is_eval_sink=False)
        for context in (
            override_settings(LOCAL_TEST_ROOT=self.directory.name),
            patch.dict(os.environ, AZURE_MOCK="true", NBHD_TENANT_ID=str(self.tenant.id)),
            patch.object(yuki_local, "STATE", self.state),
            patch.object(yuki_local.time, "sleep"),
            patch("socket.socket", side_effect=AssertionError("No real sockets in helper tests")),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.client = MagicMock()
        factory = patch.object(yuki_local.httpx, "Client")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.factory.return_value.__enter__.return_value = self.client
        self.client.post.side_effect = self.post
        self.client.get.side_effect = self.get
        self.messages = []
        self.replies = ["Your plan is ready."]
        self.on_poll = lambda: None
        self.stdout = io.StringIO()
        self.stdin = io.StringIO()

    @staticmethod
    def response(body, code=200):
        import httpx

        return httpx.Response(code, json=body)

    def post(self, url, **kwargs):
        if url == "/api/v1/auth/login/":
            return self.response({"access": "fixture-access-token"})
        if url == "/api/v1/auth/signup/":
            return self.response({}, 201)
        if url == "/api/v1/integrations/sautai/link/":
            return self.response({"status": "connected"})
        if url == "/api/v1/chat/threads/":
            return self.response({"id": "fixture-thread", "is_main": False}, 201)
        if url == "/api/v1/chat/messages/":
            self.messages.append(kwargs["json"])
            return self.response({}, 202)
        raise AssertionError("Unexpected POST")

    def get(self, url, **kwargs):
        if url == "/api/v1/tenants/me/":
            return self.response({"id": str(self.tenant.id), "is_synthetic": True, "is_eval_sink": False})
        if url.startswith("/api/v1/chat/messages/"):
            self.on_poll()
            return self.response({"status": "ready", "source": "tenant", "reply_text": self.replies.pop(0)})
        if url == "/api/v1/integrations/sautai/link/":
            return self.response({"linked": True})
        if url == "/api/v1/fuel/meals/today/":
            return self.response({"linked": True, "meals": [{"name": "Soba"}], "week_start": "2026-09-21"})
        raise AssertionError("Unexpected GET")

    def credentials(self):
        self.helper.private_json(
            self.state / "yuki-account.json", {"email": self.user.email, "password": "fixture-password"}
        )

    def run_helper(self, action, text="", success=True, **options):
        import io

        from django.core.management import call_command

        self.stdout = io.StringIO()
        with patch.object(self.helper.sys, "stdin", io.StringIO(text)):
            if success:
                call_command("yuki_local", action, stdout=self.stdout, **options)
            else:
                with self.assertRaises(SystemExit) as error:
                    call_command("yuki_local", action, stdout=self.stdout, **options)
                self.assertEqual(error.exception.code, 1)
        lines = self.stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        for secret in ("fixture-password", "fixture-access-token", "fixture-connect-key"):
            self.assertNotIn(secret, lines[0])
        return json.loads(lines[0])

    def job(self, week="2026-09-21", **kwargs):
        from apps.integrations.models import SautaiMealPlanJob

        return SautaiMealPlanJob.objects.create(
            tenant=self.tenant,
            week_start=week,
            **{
                "status": "ready",
                "addressed_by": "linked_id",
                "result": {"week_start": week, "days": [{"meals": [{"name": "Soba"}]}]},
                **kwargs,
            },
        )

    def test_signup_creates_private_file_and_is_idempotent(self):
        self.assertEqual(self.run_helper("signup"), {"account": "created"})
        path = self.state / "yuki-account.json"
        original = path.read_bytes()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(json.loads(original)["password"], self.stdout.getvalue())
        self.assertEqual(self.run_helper("signup"), {"account": "exists"})
        self.assertEqual(path.read_bytes(), original)
        signup_calls = [c for c in self.client.post.call_args_list if c.args[0].endswith("signup/")]
        self.assertEqual(len(signup_calls), 1)
        self.factory.assert_called_with(base_url=self.helper.BASE, trust_env=False, follow_redirects=False, timeout=30)

    def test_signup_bootstraps_without_a_tenant(self):
        self.tenant.delete()
        self.assertFalse(Tenant.objects.exists())
        self.assertEqual(self.run_helper("signup"), {"account": "created"})
        self.assertEqual((self.state / "yuki-account.json").stat().st_mode & 0o777, 0o600)

    def test_signup_refuses_a_foreign_synthetic_tenant(self):
        other_user = User.objects.create(username="other-local-fixture", email="other-local-fixture@example.com")
        Tenant.objects.create(user=other_user, is_synthetic=True, is_eval_sink=False)
        self.assertEqual(
            self.run_helper("signup", success=False),
            {"account": "failed", "reason": "local_stack_required"},
        )
        self.factory.assert_not_called()
        self.assertFalse((self.state / "yuki-account.json").exists())

    def test_signup_refuses_a_malformed_tenant_id(self):
        with patch.dict(os.environ, NBHD_TENANT_ID="not-a-uuid"):
            self.assertEqual(
                self.run_helper("signup", success=False),
                {"account": "failed", "reason": "local_stack_required"},
            )
        self.factory.assert_not_called()

    def test_link_still_requires_a_tenant(self):
        self.tenant.delete()
        self.assertEqual(
            self.run_helper("link", "fixture-connect-key", success=False),
            {"linked": False, "reason": "local_stack_required"},
        )
        self.factory.assert_not_called()

    def test_signup_preserves_existing_file_on_failed_login_and_signup(self):
        self.credentials()
        path = self.state / "yuki-account.json"
        original = path.read_bytes()
        self.client.post.return_value = self.response({"detail": "fixture-password"}, 409)
        self.client.post.side_effect = None
        result = self.run_helper("signup", success=False)
        self.assertEqual(result, {"account": "failed", "reason": "signup_failed"})
        self.assertEqual(path.read_bytes(), original)

    def test_all_subcommands_refuse_missing_local_root(self):
        with override_settings(LOCAL_TEST_ROOT=""):
            for action in ("signup", "link", "chat-plan", "tonight"):
                self.assertEqual(self.run_helper(action, success=False)["reason"], "local_stack_required")
        self.factory.assert_not_called()

    def test_link_checks_real_persisted_row(self):
        from django.utils import timezone

        from apps.integrations.models import Integration

        self.credentials()
        row = Integration.objects.create(
            tenant=self.tenant, provider="sautai", sautai_user_id=42, linked_at=timezone.now()
        )
        result = self.run_helper("link", "fixture-connect-key\n")
        self.assertEqual(result, {"linked": True, "sautai_user_id": 42, "linked_at": row.linked_at.isoformat()})
        self.client.post.assert_any_call(
            "/api/v1/integrations/sautai/link/", json={"connect_key": "fixture-connect-key"}
        )

    def test_link_rejects_http_status_body_and_missing_row(self):
        self.credentials()
        for body, code, reason in (
            ({}, 400, "link_failed"),
            ({}, 200, "link_not_connected"),
            ([], 200, "invalid_response"),
            ({"status": "connected"}, 200, "link_not_persisted"),
        ):

            def post(url, body=body, code=code, **kwargs):
                return self.response(body, code) if url.endswith("sautai/link/") else self.post(url, **kwargs)

            self.client.post.side_effect = post
            self.assertEqual(
                self.run_helper("link", "fixture-connect-key", success=False), {"linked": False, "reason": reason}
            )

    def test_chat_confirms_twice_and_keeps_ordered_private_evidence(self):
        self.credentials()
        self.replies = ["Please confirm the week.", "Shall I proceed?", "Generation started."]
        self.on_poll = lambda: self.job() if len(self.messages) == 3 else None
        result = self.run_helper("chat-plan", "Plan the week of 2026-09-21.\n", week="2026-09-21")
        self.assertEqual((result["turns"], result["confirm_turns"], result["meal_count"]), (3, 2, 1))
        self.assertEqual(
            [m["text"] for m in self.messages],
            ["Plan the week of 2026-09-21.", self.helper.CONFIRM, self.helper.CONFIRM],
        )
        self.assertEqual({m["thread_id"] for m in self.messages}, {"fixture-thread"})
        path = Path(result["transcript"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        transcript = json.loads(path.read_text())["messages"]
        self.assertEqual([m["role"] for m in transcript], ["yuki", "assistant"] * 3)
        self.assertTrue(all(m["timestamp"] for m in transcript))
        self.client.delete.assert_not_called()

    def test_chat_no_job_prompts_confirmation_without_question(self):
        self.credentials()
        self.replies = ["I can help with that.", "Generation started."]
        self.on_poll = lambda: self.job() if len(self.messages) == 2 else None
        result = self.run_helper("chat-plan", "Plan this week", week="2026-09-21")
        self.assertEqual(result["confirm_turns"], 1)

    def test_chat_job_ready_needs_no_confirmation(self):
        self.credentials()
        self.on_poll = self.job
        self.assertEqual(self.run_helper("chat-plan", "Plan this week", week="2026-09-21")["confirm_turns"], 0)

    def test_chat_wrong_week_fails_with_both_dates_and_evidence(self):
        self.credentials()
        self.on_poll = lambda: self.job("2026-09-28")
        result = self.run_helper("chat-plan", "Plan this week", week="2026-09-21", success=False)
        self.assertEqual(
            (result["reason"], result["week"], result["job_week"]), ("week_mismatch", "2026-09-21", "2026-09-28")
        )
        self.assertTrue(Path(result["transcript"]).is_file())
        self.assertEqual(len(self.messages), 1)

    def test_chat_rejects_failed_and_invalid_ready_jobs(self):
        self.credentials()
        for changes, reason in (
            ({"status": "failed"}, "job_failed"),
            ({"addressed_by": "email"}, "job_result_invalid"),
            ({"result": {}}, "job_result_invalid"),
            ({"error": "private error"}, "job_result_invalid"),
        ):
            self.replies = ["Generation started."]
            self.on_poll = lambda changes=changes: self.job(**changes)
            self.assertEqual(self.run_helper("chat-plan", "Plan", week="2026-09-21", success=False)["reason"], reason)

    def test_chat_main_thread_and_tenant_gate_are_enforced(self):
        self.credentials()
        self.client.get.side_effect = lambda *a, **k: self.response(
            {"id": str(self.tenant.id), "is_synthetic": False, "is_eval_sink": False}
        )
        self.assertEqual(
            self.run_helper("chat-plan", "Plan", week="2026-09-21", success=False)["reason"], "tenant_gate_failed"
        )
        self.client.get.side_effect = self.get
        self.client.post.side_effect = lambda url, **kw: (
            self.response({"id": "main", "is_main": True}, 201) if url.endswith("threads/") else self.post(url, **kw)
        )
        self.assertEqual(
            self.run_helper("chat-plan", "Plan", week="2026-09-21", success=False)["reason"], "refuse_main_thread"
        )
        self.assertFalse(self.messages)

    def test_tonight_reports_names_and_view_week(self):
        self.credentials()
        self.assertEqual(
            self.run_helper("tonight"),
            {"linked": True, "meals_today": 1, "meal_names": ["Soba"], "week_start": "2026-09-21"},
        )

    def test_tonight_empty_day_is_not_a_link_failure(self):
        self.credentials()
        self.client.get.side_effect = lambda url, **kw: (
            self.response({"linked": True, "meals": [], "week_start": "2026-09-21", "empty_reason": "no_meal_today"})
            if url.endswith("meals/today/")
            else self.get(url, **kw)
        )
        result = self.run_helper("tonight")
        self.assertEqual((result["linked"], result["meals_today"], result["empty_reason"]), (True, 0, "no_meal_today"))
