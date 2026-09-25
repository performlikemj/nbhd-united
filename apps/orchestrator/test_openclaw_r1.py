"""Review round 1 regressions; local transports only."""

import copy
import json
import os
import shlex
import shutil
import subprocess
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.cron.models import CronJob
from apps.cron.suspension import resume_tenant_crons
from apps.orchestrator import hibernation
from apps.orchestrator import openclaw_migration as migration
from apps.orchestrator import runtime_operator as operator
from apps.orchestrator.test_runtime_operator import OperatorCronAdapterTests
from apps.orchestrator.test_tenant_openclaw_migration import DIGEST, TAG, job, record, tenant_fixture


class PrivateChannelTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "Node required")
    def test_r1_real_redactor_private_response_survives(self):
        redactor = str(Path("runtime/openclaw/redact-stdout.js").resolve())
        env = {**os.environ, "NODE_OPTIONS": "--require=" + redactor}
        baseline = subprocess.run(
            ["node", "-e", "console.log('NBHD_RESULT:{}')"], env=env, capture_output=True, text=True
        )
        self.assertNotIn("NBHD_RESULT:", baseline.stdout)

        def connect(url, **kwargs):
            command = parse_qs(urlsplit(url).query)["command"][0]
            result = subprocess.run(shlex.split(command), env=env, capture_output=True, timeout=15)
            socket = Mock()
            socket.recv.side_effect = [b"\x00\x01" + result.stdout, b""]
            return socket

        with (
            patch.object(
                operator, "_connection", return_value=(SimpleNamespace(exec_endpoint="wss://offline/exec"), "secret")
            ),
            patch.object(operator.websocket, "create_connection", side_effect=connect),
        ):
            self.assertEqual(operator.run_node(Mock(), "return {ok:true};"), {"ok": True})
            body = """const {sameCron,buildAddArgs}=await import('/opt/nbhd/nbhd-cron-sync.mjs');
const d={name:'test',declarationKey:'nbhd:test',schedule:{kind:'cron',expr:'0 9 * * *'},payload:{kind:'agentTurn',message:'private'},delivery:{mode:'announce',to:'a'}};
return {same:sameCron({...d,status:'idle',schedule:{...d.schedule,staggerMs:0}},d),different:sameCron({...d,delivery:{mode:'announce',to:'b'}},d)};
"""
            self.assertEqual(operator.run_node(Mock(), body), {"same": True, "different": False})


class DeliveryTests(OperatorCronAdapterTests):
    def test_r6_destination_mismatch_never_matches_or_deletes(self):
        wanted = self.job(delivery={"mode": "announce", "channel": "telegram", "to": "recipient-a"})
        wrong = self.job(delivery={"mode": "announce", "channel": "telegram", "to": "recipient-b"})
        result = self.run_adapter([wrong, wanted | {"id": "legacy", "declarationKey": ""}], [wanted])
        self.assertFalse(result["result"]["matches"][0]["match"])
        self.assertEqual(result["mutations"], [])


class RecoveryTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture()
        self.tenant.openclaw_version = migration.VERSION
        self.tenant.save()

    def test_r2_aborted_hibernate_resumes_scheduling(self):
        with (
            patch.object(hibernation, "_capture_tenant_cron_schedules", return_value=[job()]),
            patch("apps.cron.suspension.suspend_tenant_crons", side_effect=RuntimeError("probe")),
            patch("apps.cron.suspension.resume_tenant_crons", return_value={"errors": 0}) as resume,
        ):
            self.assertFalse(hibernation.hibernate_idle_tenant(self.tenant))
        resume.assert_called_once()

    def test_r2_retry_keeps_original_snapshot(self):
        snapshot = {"jobs": [job()]}
        self.tenant.cron_suspend_state = {"active": True, "enabled_ids": ["reminder-id"]}
        self.tenant.cron_jobs_snapshot = snapshot
        self.tenant.save()
        with patch.object(hibernation, "_live_enabled_cron_jobs", return_value=[]):
            self.assertEqual(hibernation._capture_tenant_cron_schedules(self.tenant), snapshot["jobs"])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.cron_jobs_snapshot, snapshot)

    def test_r3_missing_noncanonical_job_cannot_clear_recovery(self):
        self.tenant.cron_suspend_state = {
            "active": True,
            "enabled_ids": ["operator-id"],
            "declarations": [job("operator")],
        }
        self.tenant.save()
        with (
            patch.object(operator, "list_crons", return_value=[]),
            patch.object(operator, "restore_crons", return_value={"verified": False}, create=True),
            patch("apps.cron.share_cron_sync.write_tenant_crons_file"),
        ):
            try:
                resume_tenant_crons(self.tenant)
            except RuntimeError:
                pass
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.cron_suspend_state)

    def test_r8_suspend_probe_failure_warns_once_without_payload(self):
        with (
            patch.object(hibernation, "_capture_tenant_cron_schedules", return_value=[]),
            patch("apps.cron.suspension.suspend_tenant_crons", side_effect=RuntimeError("private-payload")),
            patch("apps.cron.suspension.resume_tenant_crons", return_value={"errors": 0}),
            self.assertLogs(hibernation.logger, level="WARNING") as logs,
        ):
            self.assertFalse(hibernation.hibernate_idle_tenant(self.tenant))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("reason=suspend_failed", logs.output[0])
        self.assertNotIn("private-payload", logs.output[0])

    def test_r4_expired_captured_one_shot_fails_verification(self):
        rec = record()
        rec["cron_export"] = [job(schedule={"kind": "at", "at": (timezone.now() - timedelta(minutes=1)).isoformat()})]
        rec["evidence"]["preflight"] = {"target_image": "offline"}
        with (
            patch.object(migration, "_desired_jobs", return_value=[]),
            patch.object(
                operator,
                "inspect_signed_crons",
                return_value={"matches": [], "expected": 0, "extras": [], "legacy": []},
            ),
            patch.object(operator, "list_crons", return_value=[]),
            patch.object(migration, "wait_healthy"),
            patch.object(operator, "console_error_counts", return_value={"errors": {}}),
            patch("time.sleep"),
            self.assertRaisesRegex(migration.MigrationError, "one_shot_expired"),
        ):
            migration.verify(self.tenant, rec)

    def test_r4_imminent_job_defers_before_image(self):
        rec = record()
        rec["evidence"]["preflight"] = {"target_image": "new", "revision_suffix": "new"}
        imminent = job(schedule={"kind": "at", "at": (timezone.now() + timedelta(minutes=5)).isoformat()})
        rec["cron_export"] = [imminent]
        app = SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image="old")], revision_suffix="old")
        )
        with (
            patch.object(migration, "get_app", return_value=app),
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [imminent]}),
            patch.object(migration.azure_client, "update_container_image") as update,
            patch.object(migration, "wait_healthy"),
            self.assertRaisesRegex(migration.MigrationError, "cron_imminent"),
        ):
            migration.image_step(self.tenant, rec)
        update.assert_not_called()

    def test_r6_unsupported_declaration_rejected_at_capture(self):
        with (
            patch(
                "apps.cron.gateway_client.invoke_gateway_tool",
                return_value={"jobs": [job(delivery={"mode": "webhook", "url": "https://private.invalid"})]},
            ),
            self.assertRaisesRegex(migration.MigrationError, "unsupported_cron"),
        ):
            migration.capture(self.tenant, record())

    def test_r5_retry_recaptures_live_edit_and_keeps_history(self):
        rec = record()
        rec["cron_export"] = [job()]
        fresh = job(payload={"kind": "agentTurn", "message": "edited"})
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [fresh]}) as live:
            migration.capture(self.tenant, rec)
        live.assert_called_once()
        self.assertEqual(rec["cron_export"], [fresh])
        self.assertEqual(rec["cron_export_history"][0]["jobs"], [job()])
        self.assertEqual(CronJob.objects.get(tenant=self.tenant).data["payload"], fresh["payload"])

    def test_verify_only_is_read_only_and_reason_codes_only(self):
        before = copy.deepcopy(self.tenant.openclaw_migration)
        out = StringIO()
        with (
            patch.object(migration, "verify_existing", return_value={"verification": {"result": "PASS"}}) as verify,
            patch("apps.orchestrator.management.commands.migrate_tenant_openclaw.migrate_tenant") as mutate,
        ):
            call_command("migrate_tenant_openclaw", tenant=str(self.tenant.pk), verify_only=True, stdout=out)
        self.assertEqual(out.getvalue().strip(), "PASS verified")
        verify.assert_called_once()
        mutate.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_migration, before)


class RegistryTests(SimpleTestCase):
    def test_r7_system_identity_acr_data_plane(self):
        credential = Mock()
        credential.get_token.return_value.token = "private-aad"
        exchange = Mock()
        exchange.json.return_value = {"refresh_token": "private-refresh"}
        token = Mock()
        token.json.return_value = {"access_token": "private-access"}
        manifest = Mock(headers={"Docker-Content-Digest": DIGEST})
        with (
            patch("azure.identity.ManagedIdentityCredential", return_value=credential) as identity,
            patch.object(migration.requests, "post", side_effect=[exchange, token]) as post,
            patch.object(migration.requests, "head", return_value=manifest) as head,
        ):
            self.assertEqual(migration.registry_digest("test.azurecr.io/nbhd-openclaw:" + TAG), DIGEST)
        identity.assert_called_once_with()
        self.assertEqual(post.call_args_list[1].kwargs["data"]["scope"], "repository:nbhd-openclaw:pull")
        self.assertEqual(head.call_args.kwargs["headers"]["Authorization"], "Bearer private-access")


class AdditionalRecoveryTests(RecoveryTests):
    def test_r2_azure_failure_restores_before_return(self):
        with (
            patch.object(hibernation, "_capture_tenant_cron_schedules", return_value=[job()]),
            patch("apps.cron.suspension.suspend_tenant_crons", return_value={"errors": 0}),
            patch("apps.orchestrator.azure_client.hibernate_container_app", side_effect=RuntimeError("azure")),
            patch("apps.orchestrator.azure_client.container_app_has_active_revision", return_value=True),
            patch("apps.cron.publish.publish_task"),
            patch("apps.cron.suspension.resume_tenant_crons", return_value={"errors": 0}) as resume,
        ):
            self.assertFalse(hibernation.hibernate_idle_tenant(self.tenant))
        resume.assert_called_once()

    def test_r3_full_declarations_survive_and_verified_resume_clears(self):
        from apps.cron.suspension import suspend_tenant_crons

        jobs = [job("operator"), job("_sync:agent", enabled=False)]
        with (
            patch.object(operator, "capture_cron_declarations", return_value=jobs),
            patch.object(operator, "list_crons", return_value=[]),
            patch("apps.cron.share_cron_sync.write_tenant_crons_file"),
            patch("time.sleep"),
        ):
            suspend_tenant_crons(self.tenant)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.cron_suspend_state["declarations"], jobs)
        with (
            patch.object(operator, "restore_crons", return_value={"verified": True}) as restore,
            patch.object(operator, "inspect_signed_crons", return_value={"matches": [], "expected": 0, "extras": []}),
            patch("apps.cron.share_cron_sync.write_tenant_crons_file"),
        ):
            resume_tenant_crons(self.tenant)
        restore.assert_called_once_with(self.tenant, jobs)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.cron_suspend_state, {})

    def test_r5_cancel_after_completed_capture_never_reimports_old_job(self):
        rec = record()
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [job()]}):
            migration.capture(self.tenant, rec)
        rec["evidence"]["preflight"] = {"target_image": "new", "revision_suffix": "new"}
        app = SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image="old")], revision_suffix="old")
        )
        with (
            patch.object(migration, "get_app", return_value=app),
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
            patch.object(migration.azure_client, "update_container_image"),
            patch.object(migration, "wait_healthy"),
        ):
            migration.image_step(self.tenant, rec)
        self.assertEqual(rec["cron_export"], [])
        self.assertEqual(rec["cron_export_history"][0]["jobs"], [job()])
        self.assertFalse(CronJob.objects.get(tenant=self.tenant).enabled)

    def test_r4_all_one_shot_dispositions_accounted_for(self):
        future = (timezone.now() + timedelta(hours=1)).isoformat()
        past = (timezone.now() - timedelta(hours=1)).isoformat()
        jobs = [
            job("pending", schedule={"kind": "at", "at": future}),
            job("delivered", schedule={"kind": "at", "at": past}),
        ]
        rec = record() | {"cron_export": jobs}
        observed = [
            jobs[0],
            jobs[1] | {"state": {"lastDelivered": True, "lastRunAtMs": int(timezone.now().timestamp() * 1000)}},
        ]
        with patch.object(operator, "list_crons", return_value=observed):
            migration.one_shot_dispositions(self.tenant, rec)
        self.assertEqual([d["disposition"] for d in rec["one_shot_dispositions"]], ["pending_runtime", "delivered"])


@skipUnless(shutil.which("node"), "Node required")
class DeclarationContractTests(SimpleTestCase):
    def test_r6_all_execution_fields_roundtrip_and_mismatch(self):
        declaration = job(
            declarationKey="nbhd:test",
            sessionTarget="current",
            sessionKey="agent:main:test",
            deleteAfterRun=False,
            wakeMode="next-heartbeat",
            agentId="steward",
            pacing={"min": "1m", "max": "5m"},
            delivery={
                "mode": "announce",
                "channel": "telegram",
                "to": "recipient",
                "accountId": "account",
                "threadId": 23,
                "bestEffort": True,
            },
            payload={
                "kind": "agentTurn",
                "message": "private-payload",
                "thinking": "high",
                "model": "model",
                "fallbacks": ["fallback"],
                "timeoutSeconds": 30,
                "toolsAllow": ["nbhd_send_to_user"],
                "lightContext": True,
            },
            schedule={"kind": "cron", "expr": "0 9 * * *", "tz": "UTC", "staggerMs": 0},
        )
        module = Path("runtime/openclaw/nbhd-cron-sync.mjs").resolve().as_uri()
        script = (
            "const {buildAddArgs,sameCron}=await import("
            + json.dumps(module)
            + ");const j="
            + json.dumps(declaration)
            + """;
const flags=buildAddArgs(j);const checks=[];
for(const [part,field] of [['delivery','to'],['delivery','channel'],['delivery','accountId'],['delivery','threadId'],['delivery','bestEffort'],['payload','thinking'],['schedule','staggerMs']]){
 const changed=structuredClone(j);delete changed[part][field];checks.push(sameCron(j,changed));
}
for(const field of ['sessionTarget','sessionKey','deleteAfterRun','agentId','pacing']){const changed=structuredClone(j);delete changed[field];checks.push(sameCron(j,changed));}
console.log(JSON.stringify({flags,checks}));"""
        )
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True
        )
        output = json.loads(result.stdout)
        for flag in [
            "--to",
            "--channel",
            "--account",
            "--thread-id",
            "--best-effort-deliver",
            "--session",
            "--session-key",
            "--keep-after-run",
            "--thinking",
            "--exact",
            "--pacing-min",
        ]:
            self.assertIn(flag, output["flags"])
        # Omitted staggering and recurring retention retain runtime defaults.
        self.assertEqual(output["checks"].count(True), 2)

    def test_r3_restore_uses_private_file_and_recreates_missing_disabled_jobs(self):
        import tempfile

        jobs = [job("operator"), job("_sync:agent", enabled=False)]
        with tempfile.TemporaryDirectory() as directory:

            def write(tenant_id, name, *, data, **kwargs):
                Path(directory, name).write_bytes(data)

            def execute(tenant, body, **kwargs):
                body = body.replace(
                    "/opt/nbhd/nbhd-cron-sync.mjs", Path("runtime/openclaw/nbhd-cron-sync.mjs").resolve().as_uri()
                )
                prefix = "const fs=require('node:fs'),crypto=require('node:crypto');const jobs=[];const oc=(args)=>{if(args[1]==='list')return JSON.stringify({jobs});if(args[1]==='add'){const envelope=JSON.parse(fs.readFileSync(require('node:path').join(process.env.TEST_DIR,fs.readdirSync(process.env.TEST_DIR).find(n=>n.startsWith('nbhd-cron-restore-'))),'utf8'));const d=JSON.parse(envelope.signed).find(j=>j.name===args[2]);jobs.push({...d,id:'restored-'+d.id,declarationKey:args[args.indexOf('--declaration-key')+1],enabled:!args.includes('--disabled')});return '{}';}throw Error('unexpected');};"
                result = subprocess.run(
                    ["node", "-e", prefix + "(async()=>{" + body + "})().then(r=>console.log(JSON.stringify(r)));"],
                    env={
                        **os.environ,
                        "NODE_OPTIONS": "",
                        "TEST_DIR": directory,
                        "OPENCLAW_CONFIG_PATH": directory + "/openclaw.json",
                        "NBHD_INTERNAL_API_KEY": "test-key",
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertNotIn("Private reminder payload", result.stdout)
                return json.loads(result.stdout)

            with (
                patch("apps.orchestrator.azure_client._put_share_file", side_effect=write),
                patch(
                    "apps.orchestrator.azure_client.delete_workspace_file",
                    side_effect=lambda tid, name: Path(directory, name).unlink(),
                ),
                patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="test-key"),
                patch.object(operator, "run_node", side_effect=execute),
            ):
                self.assertEqual(
                    operator.restore_crons(SimpleNamespace(pk="tenant"), jobs), {"verified": True, "count": 2}
                )
            self.assertEqual(list(Path(directory).iterdir()), [])
