"""Fleet census 2026-09-25: real system-cron shapes, canonical precedence, spent sync notices."""

import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.migration_preservation import declaration_digest, preservation_reasons
from apps.orchestrator.test_tenant_openclaw_migration import tenant_fixture

MODEL = "openrouter/deepseek/deepseek-v4-flash-0731"


def system_row(name="Morning Briefing", **extra):
    """The real config_generator row form: model pinned at the TOP level."""
    return {
        "name": name,
        "model": MODEL,
        "enabled": True,
        "payload": {"kind": "agentTurn", "message": "Synthetic briefing"},
        "delivery": {"mode": "none"},
        "schedule": {"tz": "UTC", "expr": "0 7 * * *", "kind": "cron"},
        "sessionTarget": "isolated",
        **extra,
    }


def live_copy(name="Morning Briefing"):
    """5.28 runtime form of the same job: model folded into the payload."""
    return {
        "id": name + "-id",
        "agentId": "main",
        "name": name,
        "enabled": True,
        "createdAtMs": 1788300591874,
        "schedule": {"tz": "UTC", "expr": "0 7 * * *", "kind": "cron"},
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "payload": {"kind": "agentTurn", "message": "Synthetic briefing", "model": MODEL},
        "delivery": {"mode": "none"},
        "state": {"nextRunAtMs": 1790406000000},
        "updatedAtMs": 1790319963512,
    }


def sync_notice(name, *, age_ms):
    now = int(timezone.now().timestamp() * 1000)
    return {
        "id": f"{name}-{age_ms}",
        "agentId": "main",
        "sessionKey": "agent:main:main",
        "name": name,
        "enabled": True,
        "createdAtMs": now - age_ms,
        "schedule": {"kind": "cron", "expr": "7 21 24 9 *", "tz": "UTC"},
        "sessionTarget": "main",
        "wakeMode": "now",
        "payload": {
            "kind": "systemEvent",
            "text": f"[Sync — {name[6:]}] Synthetic. After noting this, run: cron remove {name}",
        },
    }


HOUR = 60 * 60 * 1000


class SystemCronShapeTests(SimpleTestCase):
    def test_top_level_model_system_row_is_supported_and_proven(self):
        self.assertEqual(preservation_reasons(system_row()), set())

    def test_top_level_model_digest_equals_payload_model_digest(self):
        folded = system_row()
        folded["payload"] = {**folded["payload"], "model": folded.pop("model")}
        self.assertEqual(declaration_digest(system_row()), declaration_digest(folded))

    def test_conflicting_model_pins_are_refused(self):
        row = system_row()
        row["payload"]["model"] = "openrouter/other/model"
        self.assertIn("unsupported_declaration", preservation_reasons(row))

    def test_unproven_model_value_is_still_refused(self):
        self.assertIn("unproven_shape", preservation_reasons(system_row(model="future/model")))

    @skipUnless(shutil.which("node"), "Node is required for the JS mirror")
    def test_js_mirror_folds_the_same_way(self):
        digest = (Path(__file__).parent / "migration_cron_digest.mjs").resolve().as_uri()
        conflicting = system_row()
        conflicting["payload"]["model"] = "openrouter/other/model"
        rows = [system_row(), live_copy(), conflicting]
        script = f"""
import {{normalizedDeclaration,stableJSON,provenShape}} from {json.dumps(digest)};
import {{createHash}} from 'node:crypto';
const rows={json.dumps(rows)};
console.log(JSON.stringify(rows.map(r=>({{
 digest:createHash('sha256').update(stableJSON(normalizedDeclaration(r))).digest('hex'),
 proven:provenShape(r)}}))));
"""
        out = json.loads(
            subprocess.run(
                ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True
            ).stdout
        )
        self.assertEqual(out[0]["digest"], declaration_digest(system_row()))
        self.assertTrue(out[0]["proven"])
        self.assertEqual(out[1]["digest"], declaration_digest(live_copy()))
        self.assertEqual(out[2]["digest"], declaration_digest(conflicting))
        self.assertNotEqual(out[2]["digest"], out[0]["digest"])


class CanonicalPrecedenceTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(951001)
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])

    def row(self, declaration, **kwargs):
        with suppress_cronjob_reconcile():
            return CronJob.objects.create(
                tenant=self.tenant, name=declaration["name"], data=declaration, managed=True, **kwargs
            )

    def test_live_copy_of_canonical_name_is_not_an_import(self):
        self.row(system_row())
        m.preservation_precheck(self.tenant, [live_copy()])

    def test_live_only_job_still_needs_proof(self):
        self.row(system_row())
        orphan = {**live_copy("Agent Made"), "delivery": {"mode": "announce", "to": "private"}}
        with self.assertRaises(m.PreservationError) as raised:
            m.preservation_precheck(self.tenant, [live_copy(), orphan])
        self.assertIn("delivery_to", raised.exception.reasons)

    def test_disabled_canonical_rows_stay_in_postgres_and_do_not_block(self):
        self.row(system_row())
        self.row(system_row("Old Paused", delivery={"mode": "announce", "to": "private"}), enabled=False)
        m.preservation_precheck(self.tenant, [live_copy()])

    def test_memory_core_dreaming_job_is_not_an_import(self):
        # kihomizuno canary 2026-09-26: memory-core created it on 5.28 at wake.
        self.row(system_row())
        dreaming = {
            **live_copy("Memory Dreaming Promotion"),
            "payload": {"kind": "agentTurn", "message": "Synthetic", "lightContext": True},
        }
        m.preservation_precheck(self.tenant, [live_copy(), dreaming])

    def test_recaptured_import_owned_row_still_needs_proof(self):
        row = self.row(system_row("Imported Earlier"))
        versions = {"Imported Earlier": row.updated_at.isoformat()}
        changed = {**live_copy("Imported Earlier"), "delivery": {"mode": "announce", "to": "private"}}
        with self.assertRaises(m.PreservationError) as raised:
            m.preservation_precheck(self.tenant, [changed], record={"imported_versions": versions})
        self.assertIn("delivery_to", raised.exception.reasons)

    def test_verify_ignores_disabled_canonical_rows(self):
        row = self.row(system_row())
        self.row(system_row("Old Paused", delivery={"mode": "announce", "to": "private"}), enabled=False)
        inspection = {
            "expected": 1,
            "matches": [{"key": f"nbhd:{row.pk}", "id": "runtime-id", "match": True}],
            "extras": [],
            "legacy": [],
        }
        with (
            patch.object(m.runtime_operator, "inspect_signed_crons", return_value=inspection),
            patch.object(m, "wait_healthy", return_value={}),
            patch.object(m.runtime_operator, "console_error_counts", return_value={"errors": {}}),
            patch.object(m.time, "sleep"),
        ):
            rec = {"evidence": {"preflight": {"target_image": "image"}}}
            self.assertEqual(m.verify(self.tenant, rec)["result"], "PASS")

    def test_enabled_unsupported_canonical_row_still_blocks(self):
        self.row(system_row("Pinned Destination", delivery={"mode": "announce", "to": "private"}))
        with self.assertRaises(m.PreservationError):
            m.preservation_precheck(self.tenant, [])


class SpentSyncNoticeTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(951002)

    def listing(self, *jobs):
        return patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": list(jobs)})

    def test_spent_duplicate_notices_are_not_source_jobs(self):
        normal = live_copy()
        with self.listing(
            normal,
            sync_notice("_sync:Morning Briefing", age_ms=3 * HOUR),
            sync_notice("_sync:Morning Briefing", age_ms=27 * HOUR),
        ):
            self.assertEqual(m.live_source_jobs(self.tenant), [normal])

    def test_fresh_notice_defers(self):
        with (
            self.listing(sync_notice("_sync:Evening Check-in", age_ms=5 * 60 * 1000)),
            self.assertRaisesRegex(m.MigrationError, "cron_imminent"),
        ):
            m.live_source_jobs(self.tenant)

    def test_notice_without_creation_time_defers(self):
        notice = sync_notice("_sync:Evening Check-in", age_ms=3 * HOUR)
        del notice["createdAtMs"]
        with self.listing(notice), self.assertRaisesRegex(m.MigrationError, "cron_imminent"):
            m.live_source_jobs(self.tenant)

    def test_other_sync_prefixed_jobs_are_not_notices(self):
        recurring = sync_notice("_sync:Morning Briefing", age_ms=30 * HOUR)
        recurring["schedule"] = {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}
        agent_text = sync_notice("_sync:Heartbeat Check-in", age_ms=30 * HOUR)
        agent_text["payload"] = {"kind": "systemEvent", "text": "agent authored"}
        isolated = {**sync_notice("_sync:Other", age_ms=30 * HOUR), "sessionTarget": "isolated"}
        for job in (recurring, agent_text, isolated):
            with self.subTest(name=job["name"]), self.listing(job):
                self.assertEqual(m.live_source_jobs(self.tenant), [job])
                self.assertEqual(m.report_tenant(self.tenant)["status"], "BLOCKED_UNSUPPORTED")

    def test_report_is_ready_despite_spent_duplicates_and_never_deletes(self):
        with self.listing(
            sync_notice("_sync:Morning Briefing", age_ms=3 * HOUR),
            sync_notice("_sync:Morning Briefing", age_ms=27 * HOUR),
        ) as gateway:
            self.assertEqual(m.report_tenant(self.tenant)["status"], "READY")
        self.assertEqual({c.args[1] for c in gateway.call_args_list}, {"cron.list"})

    def gateway(self, jobs, calls):
        def invoke(tenant, tool, args, **kwargs):
            calls.append((tool, args))
            removed = {a["jobId"] for t, a in calls if t == "cron.remove"}
            return {"jobs": [j for j in jobs if j["id"] not in removed]}

        return patch("apps.cron.gateway_client.invoke_gateway_tool", side_effect=invoke)

    def test_capture_deletes_only_spent_notices_after_precheck(self):
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        with suppress_cronjob_reconcile():
            CronJob.objects.create(tenant=self.tenant, name="Morning Briefing", data=system_row(), managed=True)
        spent = [sync_notice("_sync:A", age_ms=3 * HOUR), sync_notice("_sync:B", age_ms=30 * HOUR)]
        calls = []
        with self.gateway([live_copy(), *spent], calls):
            m.capture(self.tenant, {"completed": [], "evidence": {}})
        self.assertEqual(sorted(a["jobId"] for t, a in calls if t == "cron.remove"), sorted(j["id"] for j in spent))

    def test_blocked_capture_deletes_nothing(self):
        blocked = {**live_copy("Agent Made"), "delivery": {"mode": "announce", "to": "private"}}
        calls = []
        with (
            self.gateway([blocked, sync_notice("_sync:A", age_ms=3 * HOUR)], calls),
            self.assertRaises(m.PreservationError),
        ):
            m.capture(self.tenant, {"completed": [], "evidence": {}})
        self.assertNotIn("cron.remove", {t for t, _ in calls})

    def test_removal_that_does_not_stick_fails_closed(self):
        spent = sync_notice("_sync:A", age_ms=3 * HOUR)
        with self.listing(spent), self.assertRaisesRegex(m.MigrationError, "sync_notice_cleanup_failed"):
            m.remove_spent_sync_notices(self.tenant, [spent])


class OwnedJobsAreNeverCapturedTests(TestCase):
    """kihomizuno canary 2026-09-26: capture imported memory-core's job as a row."""

    def setUp(self):
        self.tenant = tenant_fixture(951003)
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        with suppress_cronjob_reconcile():
            CronJob.objects.create(tenant=self.tenant, name="Morning Briefing", data=system_row(), managed=True)

    def test_capture_imports_neither_memory_core_nor_fuel_jobs(self):
        dreaming = {
            **live_copy("Memory Dreaming Promotion"),
            "payload": {"kind": "agentTurn", "message": "Synthetic", "lightContext": True},
        }
        fuel = {**live_copy("_fuel:welcome"), "schedule": {"kind": "cron", "expr": "55 1 7 7 *", "tz": "UTC"}}
        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [live_copy(), dreaming, fuel]}
        ):
            self.assertEqual([j["name"] for j in m.live_source_jobs(self.tenant)], ["Morning Briefing"])
            m.capture(self.tenant, {"completed": [], "evidence": {}})
        self.assertEqual(
            set(CronJob.objects.filter(tenant=self.tenant).values_list("name", flat=True)), {"Morning Briefing"}
        )
        m.preservation_precheck(self.tenant, [live_copy()])

    def test_an_already_imported_memory_core_row_does_not_block(self):
        with suppress_cronjob_reconcile():
            CronJob.objects.create(
                tenant=self.tenant,
                name="Memory Dreaming Promotion",
                data={
                    **system_row("Memory Dreaming Promotion"),
                    "payload": {"kind": "agentTurn", "message": "x", "lightContext": True},
                },
                managed=True,
            )
        m.preservation_precheck(self.tenant, [live_copy()])
