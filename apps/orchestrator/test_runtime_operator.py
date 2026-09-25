"""Offline execution of operator adapters and console framing/privacy contracts."""

import hmac
import json
import os
import shutil
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from apps.orchestrator import runtime_operator as operator


class OperatorConsoleTests(SimpleTestCase):
    def test_console_handles_split_frames_and_returns_only_explicit_result(self):
        socket = Mock()
        socket.recv.side_effect = [b"\x01connected", b"\x00\x01NBHD_RES", b'\x00\x01ULT:{"ok":true}\r\n']
        container = SimpleNamespace(exec_endpoint="wss://console.invalid/exec")
        with (
            patch.object(operator, "_connection", return_value=(container, "private-token")),
            patch.object(operator.websocket, "create_connection", return_value=socket) as connect,
        ):
            self.assertEqual(operator.run_node(Mock(), "return {ok:true};"), {"ok": True})
        self.assertNotIn("private-token", connect.call_args.args[0])
        socket.close.assert_called_once()

    def test_console_failure_does_not_expose_output(self):
        socket = Mock()
        socket.recv.return_value = b'\x00\x01private-payload\nNBHD_RESULT:{"operatorError":true}\n'
        with (
            patch.object(
                operator,
                "_connection",
                return_value=(SimpleNamespace(exec_endpoint="wss://console.invalid/exec"), "secret"),
            ),
            patch.object(operator.websocket, "create_connection", return_value=socket),
            self.assertRaises(operator.OperatorError) as error,
        ):
            operator.run_node(Mock(), "return false;")
        self.assertNotIn("private-payload", str(error.exception))
        socket.close.assert_called_once()

    def test_console_log_scan_returns_counts_only(self):
        from django.utils import timezone

        now = timezone.now()
        response = Mock(text=json.dumps({"TimeStamp": now.isoformat(), "Log": "SQLite invalid JSON private-payload"}))
        with (
            patch.object(
                operator,
                "_connection",
                return_value=(SimpleNamespace(log_stream_endpoint="https://logs.invalid"), "secret"),
            ),
            patch.object(operator.requests, "get", return_value=response),
        ):
            result = operator.console_error_counts(Mock(), since=now)
        self.assertEqual(result["errors"]["sqlite"], 1)
        self.assertNotIn("private-payload", json.dumps(result))

    def test_saturated_log_tail_cannot_claim_clean_window(self):
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        response = Mock(text="\n".join(json.dumps({"TimeStamp": now.isoformat(), "Log": "ok"}) for _ in range(300)))
        with (
            patch.object(
                operator,
                "_connection",
                return_value=(SimpleNamespace(log_stream_endpoint="https://logs.invalid"), "secret"),
            ),
            patch.object(operator.requests, "get", return_value=response),
            self.assertRaisesRegex(operator.OperatorError, "does not cover"),
        ):
            operator.console_error_counts(Mock(), since=now - timedelta(minutes=5))


@skipUnless(shutil.which("node"), "Node is required for offline operator adapter tests")
class OperatorCronAdapterTests(SimpleTestCase):
    def run_adapter(self, current, desired, *, cleanup=True, canonical=None, now_ms=None):
        with patch.object(operator, "run_node", side_effect=lambda tenant, body: body):
            body = operator.inspect_signed_crons(Mock(), cleanup=cleanup, canonical_digests=canonical)
        module = Path("runtime/openclaw/nbhd-cron-sync.mjs").resolve().as_uri()
        comparator = operator._comparison_adapter()
        body = body.replace(
            "const {readSignedJobs,sameCron,buildAddArgs,atFireMs}=await import('/opt/nbhd/nbhd-cron-sync.mjs');",
            "const {readSignedJobs}=await import(" + json.dumps(module) + ");" + comparator,
        )
        signed = json.dumps(desired)
        key = "offline-test-key"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crons.json"
            path.write_text(
                json.dumps({"signed": signed, "sig": hmac.new(key.encode(), signed.encode(), sha256).hexdigest()})
            )
            script = (
                ("Date.now=()=>" + json.dumps(now_ms) + ";" if now_ms is not None else "")
                + "const crypto=await import('node:crypto'); const mutations=[]; const oc=(args)=>{if(args[1]==='list')return "
                + json.dumps(json.dumps({"jobs": current}))
                + ";mutations.push(args);return '{}';};"
                "const result=await(async()=>{" + body + "})();console.log(JSON.stringify({result,mutations}));"
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "NBHD_CRONS_FILE": str(path), "NBHD_INTERNAL_API_KEY": key},
                timeout=15,
            )
            return json.loads(result.stdout)

    def job(self, **changes):
        return {
            "id": "signed-id",
            "name": "Reminder",
            "enabled": True,
            "declarationKey": "nbhd:1",
            "schedule": {"kind": "cron", "expr": "0 9 * * *"},
            "payload": {"kind": "agentTurn", "message": "private-reminder"},
            **changes,
        }

    def test_cleanup_only_after_matching_signed_job_exists(self):
        desired = self.job()
        legacy = self.job(id="legacy-id", declarationKey="")
        result = self.run_adapter([desired, legacy], [desired])
        self.assertEqual(result["mutations"], [["cron", "rm", "legacy-id"]])
        self.assertEqual(result["result"]["legacy"], [])
        self.assertNotIn("private-reminder", json.dumps(result))
        missing = self.run_adapter([legacy], [desired])
        self.assertEqual(missing["mutations"], [])
        self.assertFalse(missing["result"]["matches"][0]["match"])

    def test_same_name_different_payload_and_disabled_jobs_are_never_deleted(self):
        desired = self.job()
        different = self.job(id="other", declarationKey="", payload={"kind": "agentTurn", "message": "different"})
        disabled = self.job(id="disabled", declarationKey="", enabled=False)
        result = self.run_adapter([desired, different, disabled], [desired])
        self.assertEqual(result["mutations"], [])
        self.assertEqual(result["result"]["legacy"], ["other"])

    def test_default_tools_do_not_cause_false_mismatch(self):
        desired = self.job()
        current = self.job(payload={**desired["payload"], "toolsAllow": ["*"]})
        result = self.run_adapter([current], [desired])
        self.assertTrue(result["result"]["matches"][0]["match"])
        self.assertEqual(result["mutations"], [])

    def test_duplicate_signed_keys_fail_verification(self):
        desired = self.job()
        result = self.run_adapter([desired, self.job(id="duplicate")], [desired])
        self.assertFalse(result["result"]["matches"][0]["match"])

    def test_current_postgres_content_overrides_stale_signed_file(self):
        from apps.orchestrator.migration_preservation import canonical_digests

        original = self.job()
        changes = [
            {"payload": {"kind": "agentTurn", "message": "changed"}},
            {"schedule": {"kind": "cron", "expr": "0 10 * * *"}},
            {"schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Tokyo"}},
            {
                "delivery": {
                    "mode": "announce",
                    "channel": "telegram",
                    "to": "changed",
                    "accountId": "a",
                    "threadId": "t",
                }
            },
            {"enabled": False},
        ]
        good = self.run_adapter([original], [original], cleanup=False, canonical=canonical_digests([original]))
        self.assertTrue(good["result"]["matches"][0]["match"])
        for fields in changes:
            with self.subTest(fields=list(fields)):
                result = self.run_adapter(
                    [original], [original], cleanup=False, canonical=canonical_digests([original | fields])
                )
                self.assertFalse(result["result"]["matches"][0]["match"])
                self.assertEqual(result["mutations"], [])
                self.assertNotIn("private-reminder", json.dumps(result))

    def test_config_observation_compares_opaque_applied_revision(self):
        with patch.object(operator, "run_node", side_effect=lambda tenant, body: body):
            body = operator.config_observed(Mock())
        prefix = "const fs={readFileSync:()=>'{\"x\":1}'};const crypto=await import('node:crypto');"
        for applied, succeeds in (("hmac-sha256:v1:current", True), ("hmac-sha256:v1:old", False)):
            remote = {
                "valid": True,
                "hash": "opaque-raw-hash",
                "configRevisionHash": "hmac-sha256:v1:current",
                "appliedConfigHash": applied,
            }
            script = (
                prefix
                + "const oc=(args)=>args[0]==='health'?'{}':"
                + json.dumps(json.dumps(remote))
                + ";try{const result=await(async()=>{"
                + body
                + "})();console.log(JSON.stringify(result));}catch{process.exit(3);}"
            )
            result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0 if succeeds else 3)


@override_settings(AZURE_RESOURCE_GROUP="test-rg")
class ImageStorageRevisionTests(SimpleTestCase):
    def test_single_revision_contains_complete_retrofit_and_preserves_secrets(self):
        from azure.mgmt.appcontainers.models import Container, ContainerApp, EnvironmentVar, Template, Volume

        from apps.orchestrator.azure_client import _OC_STATE_ENV, _WORKSPACE_MOUNT_OPTIONS, update_container_image

        app = ContainerApp(
            location="westus2",
            template=Template(
                containers=[
                    Container(name="openclaw", image="old", env=[EnvironmentVar(name="KEY", secret_ref="kv-secret")])
                ],
                volumes=[Volume(name="workspace", storage_type="AzureFile", storage_name="ws-test")],
            ),
        )
        client = Mock()
        client.container_apps.get.return_value = app
        with (
            patch("apps.orchestrator.azure_client._is_mock", return_value=False),
            patch("apps.orchestrator.azure_client.get_container_client", return_value=client),
        ):
            update_container_image(
                "oc-test",
                "test.azurecr.io/nbhd-openclaw:2026.9.4-abcdef0",
                revision_suffix="m94-test",
                retrofit_storage=True,
            )
        client.container_apps.begin_create_or_update.assert_called_once()
        self.assertEqual(app.template.revision_suffix, "m94-test")
        self.assertEqual(
            {v.name for v in app.template.volumes}, {"workspace", "oc-state", "plugin-runtime-deps", "index-cache"}
        )
        self.assertEqual(app.template.volumes[0].mount_options, _WORKSPACE_MOUNT_OPTIONS)
        env = {e.name: e.value for e in app.template.containers[0].env}
        self.assertEqual({name: env[name] for name in _OC_STATE_ENV}, _OC_STATE_ENV)
        self.assertEqual(app.template.containers[0].env[0].secret_ref, "kv-secret")
