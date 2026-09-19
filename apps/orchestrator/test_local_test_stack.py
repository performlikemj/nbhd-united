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
