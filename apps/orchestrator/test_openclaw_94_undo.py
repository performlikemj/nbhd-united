"""Operator undo for a failed 9.4 migration: share snapshot + pre-swap revision."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.orchestrator import azure_client
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.test_tenant_openclaw_migration import TAG, tenant_fixture
from apps.tenants.models import Tenant


def failed_record(**extra):
    return {
        "tag": TAG,
        "status": "FAILED",
        "completed": ["preflight", "capture"],
        "image_submitted": True,
        "evidence": {"preflight": {"source": {"version": "2026.5.28", "tag": "2026.5.28-abcdef0"}}},
        "undo": {
            "share_snapshot": "snap-1",
            "source_revision": "oc-test--old",
            "source_image": "registry/nbhd-openclaw:2026.5.28-abcdef0",
            "taken_at": "2026-09-25T00:00:00+00:00",
        },
        **extra,
    }


class RollbackTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(951201)
        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_version=m.VERSION,
            container_image_tag=TAG,
            openclaw_migration_cron_fenced=True,
            openclaw_migration=failed_record(),
        )

    def test_rollback_restores_share_then_old_revision_then_db(self):
        calls = []
        with (
            patch.object(
                azure_client,
                "restore_tenant_share",
                side_effect=lambda t, snap: calls.append(("restore", snap)) or {"restored": 3, "deleted": 1},
            ),
            patch.object(
                azure_client,
                "copy_revision",
                side_effect=lambda c, rev, suffix: calls.append(("revision", rev, suffix)),
            ),
            patch.object(
                m,
                "wait_healthy",
                side_effect=lambda t, **k: calls.append(("health", k["image"], k["suffix"])) or {"revision": "rb"},
            ),
        ):
            result = m.rollback_tenant(self.tenant.pk)
        self.assertEqual(calls[0], ("restore", "snap-1"))
        self.assertEqual(calls[1][:2], ("revision", "oc-test--old"))
        suffix = calls[1][2]
        self.assertTrue(suffix.startswith("rb-"))
        self.assertEqual(calls[2], ("health", "registry/nbhd-openclaw:2026.5.28-abcdef0", suffix))
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.tenant.refresh_from_db()
        self.assertEqual(
            (self.tenant.openclaw_version, self.tenant.container_image_tag), ("2026.5.28", "2026.5.28-abcdef0")
        )
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)
        self.assertEqual(self.tenant.openclaw_migration["status"], "ROLLED_BACK")
        self.assertEqual(self.tenant.openclaw_migration["previous"]["undo"]["share_snapshot"], "snap-1")

    def test_unhealthy_old_revision_keeps_db_on_94_and_fenced(self):
        with (
            patch.object(azure_client, "restore_tenant_share", return_value={"restored": 0, "deleted": 0}),
            patch.object(azure_client, "copy_revision"),
            patch.object(m, "wait_healthy", side_effect=m.MigrationError("health")),
            self.assertRaises(m.MigrationError),
        ):
            m.rollback_tenant(self.tenant.pk)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_version, m.VERSION)
        self.assertTrue(self.tenant.openclaw_migration_cron_fenced)

    def test_refuses_without_undo_point(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration={"status": "FAILED"})
        with self.assertRaisesRegex(m.MigrationError, "no_undo_point"):
            m.rollback_tenant(self.tenant.pk)

    def test_refuses_while_a_live_owner_holds_the_lease(self):
        lease = (timezone.now() + timezone.timedelta(minutes=5)).isoformat()
        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_migration=failed_record(status="RUNNING", lease_until=lease)
        )
        with self.assertRaisesRegex(m.MigrationError, "migration_running"):
            m.rollback_tenant(self.tenant.pk)

    def test_a_rolled_back_tenant_can_start_a_fresh_attempt(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_version="2026.5.28",
            container_image_tag="2026.5.28-abcdef0",
            openclaw_migration={"status": "ROLLED_BACK", "previous": failed_record()},
        )
        result = m.migrate_tenant(self.tenant.pk, TAG, dry_run=True)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["steps"], list(m.STEPS))


class FakeFile:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def download_file(self):
        return SimpleNamespace(readall=lambda: self.store[self.path])

    def upload_file(self, payload, length):
        self.store[self.path] = payload

    def delete_file(self):
        del self.store[self.path]


class FakeDir:
    def __init__(self, share, path):
        self.share, self.path = share, path

    def list_directories_and_files(self):
        prefix = self.path + "/" if self.path else ""
        names = {}
        for key in list(self.share.files) + list(self.share.dirs):
            if key.startswith(prefix) and key != self.path:
                head = key[len(prefix) :].split("/", 1)[0]
                names[head] = (prefix + head) in self.share.dirs
        return [{"name": n, "is_directory": d} for n, d in sorted(names.items())]

    def create_directory(self):
        from azure.core.exceptions import ResourceExistsError

        if self.path in self.share.dirs:
            raise ResourceExistsError("exists")
        self.share.dirs.add(self.path)


class FakeShare:
    def __init__(self, files, dirs):
        self.files, self.dirs = files, dirs

    def get_directory_client(self, path=""):
        return FakeDir(self, path)

    def get_file_client(self, path):
        return FakeFile(self.files, path)


class RestoreShareTests(SimpleTestCase):
    def test_mirror_restores_changed_and_removed_files_and_deletes_new_ones(self):
        snapshot = FakeShare(
            {"openclaw.json": b"528", "cron/jobs.json": b"jobs", "agents/x": b"skip"}, {"cron", "agents"}
        )
        live = FakeShare({"openclaw.json": b"94", "state.sqlite": b"new", "agents/y": b"keep"}, {"agents"})
        # Mirrors _share_clients: the snapshot is the default, None selects live.
        factory = lambda key, snap="snap-1": snapshot if snap else live  # noqa: E731
        with (
            patch.object(azure_client, "_is_mock", return_value=False),
            patch.object(azure_client, "_share_clients", return_value=(object(), factory)),
            patch(
                "apps.orchestrator.storage_credentials.run_with_lease",
                side_effect=lambda tid, lease, op: (op("key"), lease),
            ),
        ):
            result = azure_client.restore_tenant_share("tenant", "snap-1")
        self.assertEqual(live.files["openclaw.json"], b"528")
        self.assertEqual(live.files["cron/jobs.json"], b"jobs")
        self.assertNotIn("state.sqlite", live.files)
        self.assertEqual(live.files["agents/y"], b"keep")  # shadowed EmptyDir path untouched
        self.assertNotIn("agents/x", live.files)
        self.assertEqual(result, {"restored": 2, "deleted": 1})
