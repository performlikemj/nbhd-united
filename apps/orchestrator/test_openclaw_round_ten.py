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
        "payload": {"kind": "systemEvent", "text": "Synthetic sync"},
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

    def test_report_is_ready_despite_spent_duplicates(self):
        with self.listing(
            sync_notice("_sync:Morning Briefing", age_ms=3 * HOUR),
            sync_notice("_sync:Morning Briefing", age_ms=27 * HOUR),
        ):
            self.assertEqual(m.report_tenant(self.tenant)["status"], "READY")

    def test_capture_listing_deletes_only_spent_notices_and_verifies(self):
        normal = live_copy()
        spent = [sync_notice("_sync:A", age_ms=3 * HOUR), sync_notice("_sync:B", age_ms=30 * HOUR)]
        calls = []

        def gateway(tenant, tool, args, **kwargs):
            calls.append((tool, args))
            removed = {a["jobId"] for t, a in calls if t == "cron.remove"}
            return {"jobs": [j for j in [normal, *spent] if j["id"] not in removed]}

        with patch("apps.cron.gateway_client.invoke_gateway_tool", side_effect=gateway):
            self.assertEqual(m.live_source_jobs(self.tenant, remove_spent=True), [normal])
        self.assertEqual(sorted(a["jobId"] for t, a in calls if t == "cron.remove"), sorted(j["id"] for j in spent))

    def test_report_listing_never_deletes(self):
        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={"jobs": [sync_notice("_sync:A", age_ms=3 * HOUR)]},
        ) as gateway:
            self.assertEqual(m.report_tenant(self.tenant)["status"], "READY")
        self.assertEqual({c.args[1] for c in gateway.call_args_list}, {"cron.list"})

    def test_removal_that_does_not_stick_fails_closed(self):
        spent = sync_notice("_sync:A", age_ms=3 * HOUR)
        with self.listing(spent), self.assertRaisesRegex(m.MigrationError, "sync_notice_cleanup_failed"):
            m.live_source_jobs(self.tenant, remove_spent=True)
