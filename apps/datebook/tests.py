from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, time, timedelta
from time import monotonic, sleep
from unittest.mock import patch

from django.db import close_old_connections
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.common.tenant_tz import tenant_tz
from apps.orchestrator.envelope_registry import suppress_refresh
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .hashing import ItemValidationError, content_hash_v1, manifest_digest_v1
from .models import CalendarContext, DatebookGateway, DeviceCommand, MirrorEvent, MirrorReminder, SyncPage, SyncRun
from .readiness import datebook_delivery_ready
from .services import (
    ProtocolError,
    claim_device_command,
    create_device_command,
    disable_datebook,
    event_overlaps_window,
    sweep_device_commands,
)


def _source_key(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _ready_tenant(chat_id: int, *, timezone_name="UTC"):
    tenant = create_tenant(display_name="Datebook Test", telegram_chat_id=chat_id)
    tenant.status = Tenant.Status.ACTIVE
    tenant.datebook_manifest_ok = True
    tenant.datebook_enabled = True
    tenant.datebook_events_consent_at = timezone.now()
    tenant.datebook_reminders_consent_at = timezone.now()
    tenant.user.timezone = timezone_name
    tenant.user.save(update_fields=["timezone"])
    tenant.save(
        update_fields=[
            "status",
            "datebook_manifest_ok",
            "datebook_enabled",
            "datebook_events_consent_at",
            "datebook_reminders_consent_at",
        ]
    )
    return tenant


def _zoned_event(seed="event", *, title="Planning", start=None, **overrides):
    start = start or timezone.now().astimezone(UTC).replace(microsecond=123456)
    item = {
        "source_key": _source_key(seed),
        "external_id": f"external-{seed}",
        "series_id": "",
        "source_fingerprint": _source_key(f"source-{seed}"),
        "source_type": "icloud",
        "source_title": "Personal iCloud",
        "calendar_title": "Home",
        "is_read_only": False,
        "authorization_status": "full_access",
        "time": {
            "kind": "zoned",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=1)).isoformat(),
            "tz_id": "UTC",
        },
        "title": title,
        "location": "Kitchen",
        "notes": "Bring notes",
        "is_recurring": False,
    }
    item.update(overrides)
    item["content_hash"] = content_hash_v1("event", item)
    return item


def _all_day_event(seed="all-day", *, day=None, title="Holiday"):
    day = day or timezone.localdate()
    item = {
        "source_key": _source_key(seed),
        "external_id": f"external-{seed}",
        "series_id": "",
        "source_fingerprint": _source_key(f"source-{seed}"),
        "source_type": "local",
        "source_title": "On My iPhone",
        "calendar_title": "Days",
        "is_read_only": False,
        "authorization_status": "full_access",
        "time": {
            "kind": "all_day",
            "start_date": day.isoformat(),
            "end_date_exclusive": (day + timedelta(days=1)).isoformat(),
        },
        "title": title,
        "location": "",
        "notes": "",
        "is_recurring": False,
    }
    item["content_hash"] = content_hash_v1("event", item)
    return item


def _floating_event(seed="floating", *, day=None):
    day = day or timezone.localdate()
    item = {
        "source_key": _source_key(seed),
        "external_id": f"external-{seed}",
        "series_id": "series-1",
        "source_fingerprint": _source_key(f"source-{seed}"),
        "source_type": "caldav",
        "source_title": "Floating Source",
        "calendar_title": "Floating",
        "is_read_only": True,
        "authorization_status": "full_access",
        "time": {
            "kind": "floating",
            "start_local": f"{day.isoformat()}T09:15:00.900000",
            "end_local": f"{day.isoformat()}T10:45:00.100000",
        },
        "title": "Floating work",
        "location": "Desk",
        "notes": "Wall clock only",
        "is_recurring": True,
    }
    item["content_hash"] = content_hash_v1("event", item)
    return item


def _reminder(seed="reminder", *, due=None, completed=False, completed_at=None, title="Buy tea"):
    item = {
        "source_key": _source_key(seed),
        "external_id": f"external-{seed}",
        "series_id": "",
        "source_fingerprint": _source_key(f"source-{seed}"),
        "source_type": "icloud",
        "source_title": "iCloud Reminders",
        "list_title": "Errands",
        "is_read_only": False,
        "authorization_status": "full_access",
        "due": due or {"kind": "none"},
        "title": title,
        "location": "Market",
        "notes": "Green tea",
        "completed": completed,
        "completed_at": completed_at,
        "priority": 5,
    }
    item["content_hash"] = content_hash_v1("reminder", item)
    return item


class DatebookAPIMixin:
    chat_id = 920000

    def setUp(self):
        super().setUp()
        self.tenant = _ready_tenant(self.chat_id)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        registered = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-a",
                "events_consent": True,
                "reminders_consent": True,
            },
            format="json",
        )
        self.assertEqual(registered.status_code, 200, registered.data)
        self.epoch = registered.data["gateway_epoch"]

    def open_run(
        self,
        client_run_id,
        *,
        events_auth="full_access",
        events_complete=True,
        reminders_auth="not_determined",
        reminders_complete=False,
    ):
        response = self.client.post(
            "/api/v1/datebook/sync/open/",
            {
                "installation_id": "install-a",
                "gateway_epoch": self.epoch,
                "client_run_id": client_run_id,
                "events": {
                    "authorization": events_auth,
                    "coverage_complete": events_complete,
                },
                "reminders": {
                    "authorization": reminders_auth,
                    "coverage_complete": reminders_complete,
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def stage_page(self, run_id, *, events=None, reminders=None, page_index=0):
        return self.client.post(
            "/api/v1/datebook/sync/page/",
            {
                "run_id": run_id,
                "page_index": page_index,
                "installation_id": "install-a",
                "gateway_epoch": self.epoch,
                "events": events or [],
                "reminders": reminders or [],
            },
            format="json",
        )

    def commit(self, run_id, *, events=None, reminders=None):
        return self.client.post(
            "/api/v1/datebook/sync/commit/",
            {
                "run_id": run_id,
                "installation_id": "install-a",
                "gateway_epoch": self.epoch,
                "events": events,
                "reminders": reminders,
            },
            format="json",
        )

    @staticmethod
    def manifest(items):
        pairs = [(item["source_key"], item["content_hash"]) for item in items]
        return {
            "item_count": len(items),
            "manifest_digest": manifest_digest_v1(pairs),
            "absent_source_keys": [],
        }


class DatebookGateAndGatewayTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Gate", telegram_chat_id=920001)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def test_readiness_requires_manifest_enablement_and_one_consent(self):
        self.assertFalse(datebook_delivery_ready(self.tenant))
        self.tenant.datebook_manifest_ok = True
        self.tenant.datebook_enabled = True
        self.assertFalse(datebook_delivery_ready(self.tenant))
        self.tenant.datebook_events_consent_at = timezone.now()
        self.assertTrue(datebook_delivery_ready(self.tenant))
        self.tenant.datebook_events_consent_at = None
        self.tenant.datebook_reminders_consent_at = timezone.now()
        self.assertTrue(datebook_delivery_ready(self.tenant))

    def test_consent_writes_bump_pending_config_on_each_readiness_flip(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.datebook_manifest_ok = True
        self.tenant.datebook_enabled = True
        self.tenant.save(update_fields=["status", "datebook_manifest_ok", "datebook_enabled"])
        initial_version = self.tenant.pending_config_version

        granted = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "config-bump",
                "events_consent": True,
                "reminders_consent": False,
            },
            format="json",
        )
        self.assertEqual(granted.status_code, 200, granted.data)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pending_config_version, initial_version + 1)

        revoked = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "config-bump",
                "events_consent": False,
                "reminders_consent": False,
            },
            format="json",
        )
        self.assertEqual(revoked.status_code, 200, revoked.data)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pending_config_version, initial_version + 2)

    def test_feature_off_is_409_and_no_store(self):
        response = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-a",
                "events_consent": True,
                "reminders_consent": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"error": "datebook_disabled"})
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_suspended_precedes_feature_gate(self):
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save(update_fields=["status"])
        response = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-a",
                "events_consent": True,
                "reminders_consent": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {"error": "suspended"})

    def test_routes_require_consumer_authentication(self):
        response = APIClient().post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-a",
                "events_consent": True,
                "reminders_consent": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_all_consumer_routes_are_mounted_and_post_only(self):
        command_id = "00000000-0000-0000-0000-000000000001"
        paths = [
            "/api/v1/datebook/register/",
            "/api/v1/datebook/sync/open/",
            "/api/v1/datebook/sync/page/",
            "/api/v1/datebook/sync/commit/",
            "/api/v1/datebook/commands/claim/",
            f"/api/v1/datebook/commands/{command_id}/start/",
            f"/api/v1/datebook/commands/{command_id}/result/",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 405)

    def test_register_takeover_advances_epoch_and_stale_device_is_409(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.datebook_manifest_ok = True
        self.tenant.datebook_enabled = True
        self.tenant.datebook_events_consent_at = timezone.now()
        self.tenant.save(
            update_fields=[
                "status",
                "datebook_manifest_ok",
                "datebook_enabled",
                "datebook_events_consent_at",
            ]
        )
        first = self.client.post(
            "/api/v1/datebook/register/",
            {"installation_id": "install-a", "events_consent": True, "reminders_consent": False},
            format="json",
        )
        repeated = self.client.post(
            "/api/v1/datebook/register/",
            {"installation_id": "install-a", "events_consent": True, "reminders_consent": False},
            format="json",
        )
        denied = self.client.post(
            "/api/v1/datebook/register/",
            {"installation_id": "install-b", "events_consent": True, "reminders_consent": False},
            format="json",
        )
        takeover = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-b",
                "takeover": True,
                "events_consent": True,
                "reminders_consent": False,
            },
            format="json",
        )
        stale = self.client.post(
            "/api/v1/datebook/sync/open/",
            {
                "installation_id": "install-a",
                "gateway_epoch": first.data["gateway_epoch"],
                "client_run_id": "stale-run",
                "events": {"authorization": "full_access", "coverage_complete": True},
            },
            format="json",
        )
        stale_epoch = self.client.post(
            "/api/v1/datebook/sync/open/",
            {
                "installation_id": "install-b",
                "gateway_epoch": first.data["gateway_epoch"],
                "client_run_id": "stale-epoch-run",
                "events": {"authorization": "full_access", "coverage_complete": True},
            },
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(repeated.data["gateway_epoch"], first.data["gateway_epoch"])
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(takeover.data["gateway_epoch"], first.data["gateway_epoch"] + 1)
        self.assertTrue(takeover.data["taken_over"])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.data["error"], "stale_gateway")
        self.assertEqual(stale_epoch.status_code, 409)
        self.assertEqual(stale_epoch.data["error"], "stale_gateway")
        self.assertEqual(
            DatebookGateway.objects.filter(tenant=self.tenant, status=DatebookGateway.Status.ACTIVE).count(),
            1,
        )

    def test_first_consent_bootstrap_works_without_manifest(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.datebook_enabled = True
        self.tenant.datebook_manifest_ok = False
        self.tenant.save(update_fields=["status", "datebook_enabled", "datebook_manifest_ok"])

        registered = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "bootstrap-install",
                "events_consent": True,
                "reminders_consent": False,
            },
            format="json",
        )

        self.assertEqual(registered.status_code, 200, registered.data)
        self.assertEqual(registered.data["gateway_status"], DatebookGateway.Status.ACTIVE)
        self.assertFalse(registered.data["taken_over"])
        self.assertIsNotNone(registered.data["scopes"]["events"]["consent_at"])
        self.assertIsNone(registered.data["scopes"]["reminders"]["consent_at"])
        self.assertTrue(registered.data["scopes"]["events"]["full_snapshot_required"])
        self.assertFalse(registered.data["delivery_ready"])

        opened = self.client.post(
            "/api/v1/datebook/sync/open/",
            {
                "installation_id": "bootstrap-install",
                "gateway_epoch": registered.data["gateway_epoch"],
                "client_run_id": "first-consent",
                "events": {"authorization": "full_access", "coverage_complete": True},
                "reminders": {"authorization": "full_access", "coverage_complete": True},
            },
            format="json",
        )
        self.assertEqual(opened.status_code, 200, opened.data)
        self.assertTrue(opened.data["scopes"]["events"]["committable"])
        self.assertFalse(opened.data["scopes"]["reminders"]["enabled"])
        self.assertFalse(opened.data["scopes"]["reminders"]["committable"])

    def test_register_requires_both_explicit_boolean_consents(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.datebook_enabled = True
        self.tenant.save(update_fields=["status", "datebook_enabled"])

        cases = [
            ({"installation_id": "install-a"}, "invalid_events_consent"),
            (
                {"installation_id": "install-a", "events_consent": True},
                "invalid_reminders_consent",
            ),
            (
                {
                    "installation_id": "install-a",
                    "events_consent": "yes",
                    "reminders_consent": False,
                },
                "invalid_events_consent",
            ),
            (
                {
                    "installation_id": "install-a",
                    "events_consent": True,
                    "reminders_consent": 1,
                },
                "invalid_reminders_consent",
            ),
        ]
        for payload, error in cases:
            with self.subTest(error=error, payload=payload):
                response = self.client.post("/api/v1/datebook/register/", payload, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {"error": error})
        self.assertFalse(DatebookGateway.objects.filter(tenant=self.tenant).exists())

    def test_reregister_true_preserves_original_consent_timestamp(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.datebook_enabled = True
        self.tenant.save(update_fields=["status", "datebook_enabled"])
        payload = {
            "installation_id": "install-a",
            "events_consent": True,
            "reminders_consent": False,
        }
        first = self.client.post("/api/v1/datebook/register/", payload, format="json")
        second = self.client.post("/api/v1/datebook/register/", payload, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(
            second.data["scopes"]["events"]["consent_at"],
            first.data["scopes"]["events"]["consent_at"],
        )

    def test_scope_revocation_cascades_and_reenable_requires_full_snapshot(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.datebook_enabled = True
        self.tenant.datebook_manifest_ok = True
        self.tenant.save(update_fields=["status", "datebook_enabled", "datebook_manifest_ok"])
        payload = {
            "installation_id": "install-a",
            "events_consent": True,
            "reminders_consent": True,
        }
        registered = self.client.post("/api/v1/datebook/register/", payload, format="json")
        self.tenant.refresh_from_db()
        gateway = DatebookGateway.objects.get(tenant=self.tenant, status=DatebookGateway.Status.ACTIVE)
        gateway.current_generation = 7
        gateway.events_full_snapshot_required = False
        gateway.reminders_full_snapshot_required = False
        gateway.events_authorization = "full_access"
        gateway.events_last_complete_sync_at = timezone.now()
        gateway.events_window_start = timezone.now() - timedelta(days=30)
        gateway.events_window_end = timezone.now() + timedelta(days=180)
        gateway.reminders_last_complete_sync_at = timezone.now()
        gateway.save()
        with suppress_refresh():
            event = MirrorEvent.objects.create(
                tenant=self.tenant,
                source_key=_source_key("revoke-event"),
                content_hash="a" * 64,
                time_kind="all_day",
                all_day_start_date=timezone.localdate(),
                all_day_end_date_exclusive=timezone.localdate() + timedelta(days=1),
                active=True,
            )
            reminder = MirrorReminder.objects.create(
                tenant=self.tenant,
                source_key=_source_key("revoke-reminder"),
                content_hash="b" * 64,
                due_kind="none",
                active=True,
            )
        calendar_command, _ = create_device_command(
            self.tenant,
            request_id="revoke-calendar-command",
            command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
            payload={"items": [{"title": "Event"}]},
        )
        reminder_command, _ = create_device_command(
            self.tenant,
            request_id="keep-reminder-command",
            command_type=DeviceCommand.CommandType.REMINDER_CREATE,
            payload={"items": [{"title": "Reminder"}]},
        )
        claimed = self.client.post(
            "/api/v1/datebook/commands/claim/",
            {
                "installation_id": "install-a",
                "gateway_epoch": registered.data["gateway_epoch"],
            },
            format="json",
        )
        self.assertEqual(claimed.data["command"]["command_type"], DeviceCommand.CommandType.CALENDAR_CREATE)
        opened = self.client.post(
            "/api/v1/datebook/sync/open/",
            {
                "installation_id": "install-a",
                "gateway_epoch": registered.data["gateway_epoch"],
                "client_run_id": "revoke-open-run",
                "events": {"authorization": "full_access", "coverage_complete": True},
                "reminders": {"authorization": "full_access", "coverage_complete": True},
            },
            format="json",
        )

        revoked = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-a",
                "events_consent": False,
                "reminders_consent": True,
            },
            format="json",
        )
        self.assertEqual(revoked.status_code, 200, revoked.data)
        event.refresh_from_db()
        reminder.refresh_from_db()
        calendar_command.refresh_from_db()
        reminder_command.refresh_from_db()
        gateway.refresh_from_db()
        self.assertFalse(event.active)
        self.assertEqual(event.inactive_generation, 7)
        self.assertTrue(reminder.active)
        self.assertEqual(calendar_command.state, DeviceCommand.State.CANCELLED)
        self.assertEqual(reminder_command.state, DeviceCommand.State.PENDING)
        self.assertTrue(gateway.events_full_snapshot_required)
        self.assertFalse(gateway.reminders_full_snapshot_required)
        self.assertIsNone(gateway.events_last_complete_sync_at)
        self.assertIsNone(gateway.events_window_start)
        self.assertIsNone(gateway.events_window_end)
        self.assertEqual(SyncRun.objects.get(id=opened.data["run_id"]).state, SyncRun.State.ABORTED)

        reenabled = self.client.post("/api/v1/datebook/register/", payload, format="json")
        self.assertEqual(reenabled.status_code, 200, reenabled.data)
        self.assertTrue(reenabled.data["scopes"]["events"]["full_snapshot_required"])
        next_run = self.client.post(
            "/api/v1/datebook/sync/open/",
            {
                "installation_id": "install-a",
                "gateway_epoch": registered.data["gateway_epoch"],
                "client_run_id": "reenabled-run",
                "events": {"authorization": "full_access", "coverage_complete": True},
                "reminders": {"authorization": "full_access", "coverage_complete": True},
            },
            format="json",
        )
        self.assertTrue(next_run.data["scopes"]["events"]["full_snapshot_required"])

    def test_both_revoked_makes_consumer_sync_require_consent(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.datebook_enabled = True
        self.tenant.save(update_fields=["status", "datebook_enabled"])
        self.client.post(
            "/api/v1/datebook/register/",
            {"installation_id": "install-a", "events_consent": True, "reminders_consent": True},
            format="json",
        )
        revoked = self.client.post(
            "/api/v1/datebook/register/",
            {"installation_id": "install-a", "events_consent": False, "reminders_consent": False},
            format="json",
        )
        denied = self.client.post("/api/v1/datebook/sync/open/", {}, format="json")
        self.assertEqual(revoked.status_code, 200, revoked.data)
        self.assertFalse(revoked.data["delivery_ready"])
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.data, {"error": "consent_required"})


class DatebookSyncTests(DatebookAPIMixin, TestCase):
    chat_id = 920002

    def test_zero_duration_timed_events_are_accepted_stored_and_hashed_stably(self):
        zoned = _zoned_event(
            "zero-zoned",
            start=timezone.now().astimezone(UTC).replace(microsecond=123456),
        )
        zoned["time"]["end_at"] = zoned["time"]["start_at"]
        zoned["content_hash"] = content_hash_v1("event", zoned)

        floating = _floating_event("zero-floating")
        floating["time"]["end_local"] = floating["time"]["start_local"]
        floating["content_hash"] = content_hash_v1("event", floating)

        opened = self.open_run("zero-duration-events")
        page = self.stage_page(opened.data["run_id"], events=[zoned, floating])
        self.assertEqual(page.status_code, 200, page.data)
        self.assertEqual(page.data["events"]["accepted"], 2)
        committed = self.commit(opened.data["run_id"], events=self.manifest([zoned, floating]))
        self.assertEqual(committed.status_code, 200, committed.data)

        zoned_row = MirrorEvent.objects.get(source_key=zoned["source_key"])
        floating_row = MirrorEvent.objects.get(source_key=floating["source_key"])
        self.assertTrue(zoned_row.active)
        self.assertEqual(zoned_row.zoned_end_at, zoned_row.zoned_start_at)
        self.assertEqual(zoned_row.content_hash, zoned["content_hash"])
        self.assertEqual(zoned_row.content_hash, content_hash_v1("event", zoned))
        self.assertTrue(floating_row.active)
        self.assertEqual(floating_row.floating_end_date, floating_row.floating_start_date)
        self.assertEqual(floating_row.floating_end_time, floating_row.floating_start_time)

    def test_open_and_page_are_idempotent_without_duplicate_staging(self):
        first = self.open_run("idempotent-open")
        second = self.open_run("idempotent-open")
        self.assertEqual(first.data["run_id"], second.data["run_id"])
        self.assertFalse(first.data["idempotent"])
        self.assertTrue(second.data["idempotent"])
        self.assertLess(first.data["event_window"]["start"], first.data["event_window"]["end"])
        self.assertTrue(first.data["scopes"]["events"]["full_snapshot_required"])

        item = _zoned_event("page-idempotent")
        page1 = self.stage_page(first.data["run_id"], events=[item])
        page2 = self.stage_page(first.data["run_id"], events=[item])
        self.assertEqual(page1.status_code, 200, page1.data)
        self.assertFalse(page1.data["idempotent"])
        self.assertTrue(page2.data["idempotent"])
        self.assertEqual(SyncPage.objects.filter(run_id=first.data["run_id"]).count(), 1)
        self.assertEqual(MirrorEvent.objects.filter(tenant=self.tenant).count(), 1)
        staged = MirrorEvent.objects.get(tenant=self.tenant)
        self.assertFalse(staged.active)
        self.assertEqual(staged.content_hash, "")
        self.assertEqual(staged.staged_run_id.hex, first.data["run_id"].replace("-", ""))

        conflict = self.client.post(
            "/api/v1/datebook/sync/open/",
            {
                "installation_id": "install-a",
                "gateway_epoch": self.epoch,
                "client_run_id": "idempotent-open",
                "events": {"authorization": "full_access", "coverage_complete": False},
                "reminders": {"authorization": "not_determined", "coverage_complete": False},
            },
            format="json",
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["error"], "client_run_conflict")

    def test_new_client_run_supersedes_staged_same_gateway_run(self):
        consent = self.client.post(
            "/api/v1/datebook/register/",
            {"installation_id": "install-a", "events_consent": True, "reminders_consent": False},
            format="json",
        )
        self.assertEqual(consent.status_code, 200, consent.data)
        old = self.open_run("consent-toggle-events-only")
        self.assertFalse(old.data["scopes"]["reminders"]["enabled"])
        abandoned = _zoned_event("consent-toggle-abandoned")
        self.assertEqual(self.stage_page(old.data["run_id"], events=[abandoned]).status_code, 200)

        consent = self.client.post(
            "/api/v1/datebook/register/",
            {"installation_id": "install-a", "events_consent": True, "reminders_consent": True},
            format="json",
        )
        self.assertEqual(consent.status_code, 200, consent.data)
        # Open and commit both lock the active gateway row: a concurrent commit
        # either finishes first, or resumes after this supersede and sees ABORTED.
        replacement = self.open_run(
            "consent-toggle-both",
            reminders_auth="full_access",
            reminders_complete=True,
        )
        self.assertTrue(replacement.data["scopes"]["reminders"]["enabled"])

        old_run = SyncRun.objects.get(id=old.data["run_id"])
        self.assertEqual(old_run.state, SyncRun.State.ABORTED)
        self.assertFalse(old_run.requires_full_snapshot)
        self.assertFalse(MirrorEvent.objects.filter(source_key=abandoned["source_key"]).exists())

        event = _zoned_event("consent-toggle-event")
        reminder = _reminder("consent-toggle-reminder")
        page = self.stage_page(replacement.data["run_id"], events=[event], reminders=[reminder])
        self.assertEqual(page.status_code, 200, page.data)
        committed = self.commit(
            replacement.data["run_id"],
            events=self.manifest([event]),
            reminders=self.manifest([reminder]),
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        self.assertEqual(
            set(MirrorEvent.objects.filter(tenant=self.tenant, active=True).values_list("source_key", flat=True)),
            {event["source_key"]},
        )
        self.assertEqual(
            set(MirrorReminder.objects.filter(tenant=self.tenant, active=True).values_list("source_key", flat=True)),
            {reminder["source_key"]},
        )

    def test_superseded_run_cannot_commit_from_zombie_client(self):
        old = self.open_run("zombie-old")
        replacement = self.open_run("zombie-replacement")
        self.assertNotEqual(old.data["run_id"], replacement.data["run_id"])

        zombie = self.commit(old.data["run_id"], events=self.manifest([]))

        self.assertEqual(zombie.status_code, 409)
        self.assertEqual(zombie.data["error"], "run_aborted")

    def test_datebook_request_body_is_bounded(self):
        response = self.client.post(
            "/api/v1/datebook/sync/open/",
            {"padding": "x" * 1_048_576},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "request_too_large")

    def test_invalid_item_makes_only_that_scope_non_committable(self):
        opened = self.open_run(
            "invalid-event-valid-reminder",
            reminders_auth="full_access",
            reminders_complete=True,
        )
        bad = _zoned_event("bad-hash")
        bad["title"] = "changed after hash"
        reminder = _reminder("valid-reminder")
        page = self.stage_page(opened.data["run_id"], events=[bad], reminders=[reminder])
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.data["events"]["committable"])
        self.assertEqual(page.data["events"]["errors"][0]["error"], "content_hash_mismatch")
        self.assertTrue(page.data["reminders"]["committable"])
        committed = self.commit(
            opened.data["run_id"],
            reminders=self.manifest([reminder]),
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        self.assertEqual(committed.data["events"], "not_committed")
        self.assertEqual(MirrorEvent.objects.filter(tenant=self.tenant, active=True).count(), 0)
        self.assertEqual(MirrorReminder.objects.filter(tenant=self.tenant, active=True).count(), 1)

    def test_page_cap_rejects_atomically_before_any_staging(self):
        opened = self.open_run("page-cap")
        response = self.stage_page(opened.data["run_id"], events=[{}] * 51)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "too_many_events")
        self.assertFalse(SyncPage.objects.filter(run_id=opened.data["run_id"]).exists())
        self.assertFalse(MirrorEvent.objects.filter(tenant=self.tenant).exists())

    def test_idempotent_page_retry_reports_run_level_scope_failure(self):
        opened = self.open_run("page-run-level-failure")
        good = _zoned_event("page-good")
        first = self.stage_page(opened.data["run_id"], events=[good], page_index=0)
        self.assertTrue(first.data["events"]["committable"])

        bad = _zoned_event("page-bad")
        bad["title"] = "changed after hash"
        rejected = self.stage_page(opened.data["run_id"], events=[bad], page_index=1)
        self.assertFalse(rejected.data["events"]["committable"])
        retried = self.stage_page(opened.data["run_id"], events=[good], page_index=0)
        self.assertTrue(retried.data["idempotent"])
        self.assertFalse(retried.data["events"]["committable"])

    def test_partial_fetch_does_not_touch_mirror_or_advance_freshness(self):
        item = _zoned_event("partial-existing")
        initial = self.open_run("partial-initial")
        self.assertEqual(self.stage_page(initial.data["run_id"], events=[item]).status_code, 200)
        committed = self.commit(initial.data["run_id"], events=self.manifest([item]))
        self.assertEqual(committed.status_code, 200, committed.data)
        gateway = DatebookGateway.objects.get(tenant=self.tenant, status=DatebookGateway.Status.ACTIVE)
        freshness = gateway.events_last_complete_sync_at
        generation = gateway.current_generation

        partial = self.open_run("partial-run", events_auth="denied", events_complete=False)
        response = self.commit(partial.data["run_id"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "no_committable_scopes")
        gateway.refresh_from_db()
        row = MirrorEvent.objects.get(tenant=self.tenant, source_key=item["source_key"])
        self.assertTrue(row.active)
        self.assertEqual(row.content_hash, item["content_hash"])
        self.assertEqual(gateway.events_last_complete_sync_at, freshness)
        self.assertEqual(gateway.current_generation, generation)
        self.assertEqual(gateway.events_authorization, "denied")

    def test_stale_base_and_epoch_are_conflicts(self):
        run2 = self.open_run("base-two")
        stale_item = _zoned_event("stale-base-staged")
        self.assertEqual(self.stage_page(run2.data["run_id"], events=[stale_item]).status_code, 200)
        DatebookGateway.objects.filter(tenant=self.tenant, status=DatebookGateway.Status.ACTIVE).update(
            current_generation=1
        )
        empty_manifest = self.manifest([])
        stale_base = self.commit(run2.data["run_id"], events=empty_manifest)
        self.assertEqual(stale_base.status_code, 409)
        self.assertEqual(stale_base.data["error"], "stale_base_generation")
        self.assertEqual(SyncRun.objects.get(id=run2.data["run_id"]).state, SyncRun.State.ABORTED)
        self.assertFalse(MirrorEvent.objects.filter(source_key=stale_item["source_key"]).exists())

        run3 = self.open_run("epoch-old")
        staged = _zoned_event("epoch-old-staged")
        self.assertEqual(self.stage_page(run3.data["run_id"], events=[staged]).status_code, 200)
        takeover = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-b",
                "takeover": True,
                "events_consent": True,
                "reminders_consent": True,
            },
            format="json",
        )
        self.assertEqual(takeover.status_code, 200)
        stale_epoch = self.commit(run3.data["run_id"], events=empty_manifest)
        self.assertEqual(stale_epoch.status_code, 409)
        self.assertEqual(stale_epoch.data["error"], "stale_gateway")
        self.assertEqual(SyncRun.objects.get(id=run3.data["run_id"]).state, SyncRun.State.ABORTED)
        self.assertFalse(MirrorEvent.objects.filter(source_key=staged["source_key"]).exists())

    @patch("apps.datebook.services.push_visibility_refresh")
    def test_manifest_mismatch_aborts_without_visibility_push(self, refresh):
        opened = self.open_run("mismatch")
        item = _zoned_event("mismatch-event")
        self.assertEqual(self.stage_page(opened.data["run_id"], events=[item]).status_code, 200)
        mismatch = self.commit(opened.data["run_id"], events=self.manifest([]))
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.data["error"], "full_snapshot_required")
        self.assertEqual(mismatch.data["reason"], "manifest_mismatch")
        run = SyncRun.objects.get(id=opened.data["run_id"])
        self.assertEqual(run.state, SyncRun.State.ABORTED)
        self.assertTrue(run.requires_full_snapshot)
        self.assertFalse(MirrorEvent.objects.filter(tenant=self.tenant).exists())
        refresh.assert_not_called()

    @patch("apps.datebook.services.push_visibility_refresh")
    def test_successful_commit_publishes_once_and_retry_is_idempotent(self, refresh):
        opened = self.open_run("commit-idempotent")
        item = _zoned_event("commit-idempotent-event")
        self.stage_page(opened.data["run_id"], events=[item])
        manifest = self.manifest([item])
        with self.captureOnCommitCallbacks(execute=True):
            first = self.commit(opened.data["run_id"], events=manifest)
            second = self.commit(opened.data["run_id"], events=manifest)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data["idempotent"])
        self.assertEqual(first.data["generation"], second.data["generation"])
        refresh.assert_called_once_with(str(self.tenant.id))

    def test_window_slide_evicts_without_deleting_and_reappearance_reactivates(self):
        item = _zoned_event("window-slide")
        first = self.open_run("window-first")
        self.stage_page(first.data["run_id"], events=[item])
        self.assertEqual(self.commit(first.data["run_id"], events=self.manifest([item])).status_code, 200)
        row = MirrorEvent.objects.get(tenant=self.tenant, source_key=item["source_key"])
        original_id = row.id

        second = self.open_run("window-second")
        future = timezone.now() + timedelta(days=400)
        SyncRun.objects.filter(id=second.data["run_id"]).update(
            event_window_start=future,
            event_window_end=future + timedelta(days=10),
        )
        self.assertEqual(self.commit(second.data["run_id"], events=self.manifest([])).status_code, 200)
        row.refresh_from_db()
        self.assertFalse(row.active)
        self.assertEqual(MirrorEvent.objects.filter(id=original_id).count(), 1)

        third = self.open_run("window-third")
        self.stage_page(third.data["run_id"], events=[item])
        self.assertEqual(self.commit(third.data["run_id"], events=self.manifest([item])).status_code, 200)
        row.refresh_from_db()
        self.assertTrue(row.active)
        self.assertEqual(row.id, original_id)
        self.assertIsNone(row.inactive_generation)

    def test_takeover_full_snapshot_replaces_prior_installation_source_keys(self):
        old = _zoned_event("old-install-source")
        first = self.open_run("takeover-before")
        self.stage_page(first.data["run_id"], events=[old])
        self.assertEqual(self.commit(first.data["run_id"], events=self.manifest([old])).status_code, 200)
        gateway = DatebookGateway.objects.get(tenant=self.tenant, status=DatebookGateway.Status.ACTIVE)
        self.assertFalse(gateway.events_full_snapshot_required)

        takeover = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-b",
                "takeover": True,
                "events_consent": True,
                "reminders_consent": True,
            },
            format="json",
        )
        new_epoch = takeover.data["gateway_epoch"]
        opened = self.client.post(
            "/api/v1/datebook/sync/open/",
            {
                "installation_id": "install-b",
                "gateway_epoch": new_epoch,
                "client_run_id": "takeover-after",
                "events": {"authorization": "full_access", "coverage_complete": True},
                "reminders": {"authorization": "not_determined", "coverage_complete": False},
            },
            format="json",
        )
        self.assertTrue(opened.data["scopes"]["events"]["full_snapshot_required"])
        replacement = dict(old)
        replacement["source_key"] = _source_key("new-install-source")
        replacement["content_hash"] = content_hash_v1("event", replacement)
        page = self.client.post(
            "/api/v1/datebook/sync/page/",
            {
                "run_id": opened.data["run_id"],
                "page_index": 0,
                "installation_id": "install-b",
                "gateway_epoch": new_epoch,
                "events": [replacement],
                "reminders": [],
            },
            format="json",
        )
        self.assertEqual(page.status_code, 200, page.data)
        committed = self.client.post(
            "/api/v1/datebook/sync/commit/",
            {
                "run_id": opened.data["run_id"],
                "installation_id": "install-b",
                "gateway_epoch": new_epoch,
                "events": self.manifest([replacement]),
            },
            format="json",
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        self.assertFalse(MirrorEvent.objects.get(source_key=old["source_key"]).active)
        self.assertTrue(MirrorEvent.objects.get(source_key=replacement["source_key"]).active)

    def test_tagged_time_round_trips_and_tenant_timezone_controls_all_day_overlap(self):
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])
        local_day = timezone.now().astimezone(tenant_tz(self.tenant)).date()
        all_day = _all_day_event("tag-all-day", day=local_day)
        floating = _floating_event("tag-floating", day=local_day)
        zoned = _zoned_event("tag-zoned")
        due_day = _reminder(
            "due-day",
            due={"kind": "all_day", "due_date": local_day.isoformat()},
        )
        due_floating = _reminder(
            "due-floating",
            due={"kind": "floating", "due_local": f"{local_day.isoformat()}T12:30:00"},
        )
        due_zoned = _reminder(
            "due-zoned",
            due={"kind": "zoned", "due_at": timezone.now().isoformat(), "tz_id": "Asia/Tokyo"},
            completed=True,
            completed_at=timezone.now().isoformat(),
        )
        due_none = _reminder("due-none")
        opened = self.open_run(
            "tagged-times",
            reminders_auth="full_access",
            reminders_complete=True,
        )
        page = self.stage_page(
            opened.data["run_id"],
            events=[all_day, floating, zoned],
            reminders=[due_day, due_floating, due_zoned, due_none],
        )
        self.assertEqual(page.status_code, 200, page.data)
        committed = self.commit(
            opened.data["run_id"],
            events=self.manifest([all_day, floating, zoned]),
            reminders=self.manifest([due_day, due_floating, due_zoned, due_none]),
        )
        self.assertEqual(committed.status_code, 200, committed.data)

        day_row = MirrorEvent.objects.get(source_key=all_day["source_key"])
        floating_row = MirrorEvent.objects.get(source_key=floating["source_key"])
        zoned_row = MirrorEvent.objects.get(source_key=zoned["source_key"])
        self.assertEqual(day_row.all_day_start_date, local_day)
        self.assertIsNone(day_row.zoned_start_at)
        self.assertEqual(floating_row.floating_start_time, time(9, 15))
        self.assertIsNone(floating_row.zoned_start_at)
        self.assertEqual(zoned_row.zoned_start_at.microsecond, 0)
        self.assertEqual(MirrorReminder.objects.get(source_key=due_day["source_key"]).due_kind, "all_day")
        self.assertEqual(
            MirrorReminder.objects.get(source_key=due_floating["source_key"]).floating_due_time,
            time(12, 30),
        )
        zoned_reminder = MirrorReminder.objects.get(source_key=due_zoned["source_key"])
        self.assertIsNotNone(zoned_reminder.zoned_due_at)
        self.assertTrue(zoned_reminder.completed)
        self.assertEqual(zoned_reminder.completed_at.microsecond, 0)
        self.assertEqual(MirrorReminder.objects.get(source_key=due_none["source_key"]).due_kind, "none")

        local_midnight_utc = datetime.combine(local_day, time.min, tzinfo=tenant_tz(self.tenant)).astimezone(UTC)
        self.assertFalse(
            event_overlaps_window(
                day_row,
                self.tenant,
                local_midnight_utc - timedelta(hours=2),
                local_midnight_utc - timedelta(seconds=1),
            )
        )
        self.assertTrue(
            event_overlaps_window(
                day_row,
                self.tenant,
                local_midnight_utc,
                local_midnight_utc + timedelta(minutes=1),
            )
        )


class ContentHashTests(TestCase):
    def test_zero_duration_zoned_event_content_hash_vector(self):
        item = _zoned_event(
            "zero-duration-vector",
            start=datetime(2026, 8, 12, 3, 4, 5, 987654, tzinfo=UTC),
        )
        item["time"]["end_at"] = item["time"]["start_at"]
        item.pop("content_hash")

        self.assertEqual(
            content_hash_v1("event", item),
            "aee96a8b033c94eb5913d4d0962d98e80aa3b75d3a8aa98d47317e10ce86dd48",
        )

    def test_inverted_timed_events_and_equal_all_day_are_invalid_time_order(self):
        cases = [
            (
                _zoned_event(
                    "inverted-zoned",
                    start=datetime(2026, 8, 12, 3, 4, 5, tzinfo=UTC),
                ),
                {
                    "kind": "zoned",
                    "start_at": "2026-08-12T03:04:05Z",
                    "end_at": "2026-08-12T03:04:04Z",
                    "tz_id": "UTC",
                },
            ),
            (
                _floating_event("inverted-floating", day=datetime(2026, 8, 12).date()),
                {
                    "kind": "floating",
                    "start_local": "2026-08-12T03:04:05",
                    "end_local": "2026-08-12T03:04:04",
                },
            ),
            (
                _all_day_event("equal-all-day", day=datetime(2026, 8, 12).date()),
                {
                    "kind": "all_day",
                    "start_date": "2026-08-12",
                    "end_date_exclusive": "2026-08-12",
                },
            ),
        ]
        for item, invalid_time in cases:
            with self.subTest(kind=invalid_time["kind"]):
                item["time"] = invalid_time
                item.pop("content_hash")
                with self.assertRaises(ItemValidationError) as ctx:
                    content_hash_v1("event", item)
                self.assertEqual(ctx.exception.code, "invalid_time_order")

    def test_nfc_lf_truncation_sorted_keys_and_utc_seconds_vectors(self):
        start = datetime(2026, 8, 11, 12, 0, 0, 999999, tzinfo=UTC)
        decomposed = _zoned_event("canon-a", title="Cafe\u0301\r\n" + "x" * 300, start=start)
        composed = dict(decomposed)
        composed["title"] = "Caf\u00e9\n" + "x" * 252 + "ignored-tail"
        composed["time"] = {
            "tz_id": "UTC",
            "end_at": "2026-08-11T13:00:00Z",
            "kind": "zoned",
            "start_at": "2026-08-11T12:00:00Z",
        }
        decomposed.pop("content_hash")
        composed.pop("content_hash", None)
        self.assertEqual(content_hash_v1("event", decomposed), content_hash_v1("event", composed))

    def test_floats_are_rejected(self):
        item = _reminder("float")
        item["priority"] = 1.5
        item.pop("content_hash")
        with self.assertRaises(ItemValidationError) as ctx:
            content_hash_v1("reminder", item)
        self.assertEqual(ctx.exception.code, "floats_not_allowed")


class DeviceCommandTests(DatebookAPIMixin, TestCase):
    chat_id = 920003

    def create_command(self, request_id="request-1", *, items=1, target_at=None):
        return create_device_command(
            self.tenant,
            request_id=request_id,
            command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
            payload={"items": [{"title": f"Meeting {index}"} for index in range(items)]},
            display_text="Create meetings",
            destination_name="Home",
            destination_fingerprint="destination-a",
            target_at=target_at,
        )

    def claim(self):
        return self.client.post(
            "/api/v1/datebook/commands/claim/",
            {"installation_id": "install-a", "gateway_epoch": self.epoch},
            format="json",
        )

    def start(self, command, lease_token):
        return self.client.post(
            f"/api/v1/datebook/commands/{command.id}/start/",
            {
                "installation_id": "install-a",
                "gateway_epoch": self.epoch,
                "lease_token": lease_token,
                "destination_fingerprint": "destination-a",
            },
            format="json",
        )

    def test_command_create_is_idempotent(self):
        first, created = self.create_command()
        second, created_again = self.create_command()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, second.id)
        self.assertEqual(DeviceCommand.objects.filter(tenant=self.tenant).count(), 1)

    def test_start_requires_valid_lease_and_expired_prestart_lease_requeues(self):
        command, _ = self.create_command()
        claimed = self.claim()
        token = claimed.data["command"]["lease_token"]
        invalid = self.start(command, "00000000-0000-0000-0000-000000000000")
        self.assertEqual(invalid.status_code, 409)
        self.assertEqual(invalid.data["error"], "invalid_lease_token")
        DeviceCommand.objects.filter(id=command.id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        expired = self.start(command, token)
        self.assertEqual(expired.status_code, 409)
        self.assertTrue(expired.data["requeueable"])
        command.refresh_from_db()
        self.assertEqual(command.state, DeviceCommand.State.PENDING)
        reclaimed = self.claim()
        self.assertNotEqual(reclaimed.data["command"]["lease_token"], token)

    def test_start_revalidates_destination_before_execution(self):
        command, _ = self.create_command("destination-change")
        claimed = self.claim()
        denied = self.client.post(
            f"/api/v1/datebook/commands/{command.id}/start/",
            {
                "installation_id": "install-a",
                "gateway_epoch": self.epoch,
                "lease_token": claimed.data["command"]["lease_token"],
                "destination_fingerprint": "changed-destination",
            },
            format="json",
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.data["error"], "destination_changed")
        command.refresh_from_db()
        self.assertEqual(command.state, DeviceCommand.State.LEASED)
        self.assertIsNone(command.started_at)

    def test_stale_executing_becomes_ambiguous_and_never_requeues(self):
        command, _ = self.create_command()
        claimed = self.claim()
        started = self.start(command, claimed.data["command"]["lease_token"])
        self.assertEqual(started.status_code, 200, started.data)
        DeviceCommand.objects.filter(id=command.id).update(execution_deadline_at=timezone.now() - timedelta(seconds=1))
        counts = sweep_device_commands(tenant=self.tenant)
        self.assertEqual(counts["ambiguous"], 1)
        self.assertEqual(sweep_device_commands(tenant=self.tenant)["requeued"], 0)
        command.refresh_from_db()
        self.assertEqual(command.state, DeviceCommand.State.AMBIGUOUS)
        self.assertEqual(command.execution_status, DeviceCommand.ExecutionStatus.AMBIGUOUS)
        self.assertIsNone(self.claim().data["command"])

    def test_result_retry_is_idempotent_and_mirror_failure_does_not_change_execution_truth(self):
        command, _ = self.create_command()
        claimed = self.claim()
        token = claimed.data["command"]["lease_token"]
        self.assertEqual(self.start(command, token).status_code, 200)
        payload = {
            "lease_token": token,
            "result_id": "journal-result-1",
            "execution_status": "executed",
            "mirror_status": "failed",
            "safe_error": "",
            "result_identifiers": {"calendar_item_id": "device-id-1"},
            "result_display": "Saved to Home",
            "journaled_at": timezone.now().isoformat(),
        }
        first = self.client.post(f"/api/v1/datebook/commands/{command.id}/result/", payload, format="json")
        second = self.client.post(f"/api/v1/datebook/commands/{command.id}/result/", payload, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data["idempotent"])
        command.refresh_from_db()
        self.assertEqual(command.state, DeviceCommand.State.EXECUTED)
        self.assertEqual(command.execution_status, DeviceCommand.ExecutionStatus.SUCCEEDED)
        self.assertEqual(command.mirror_status, DeviceCommand.MirrorStatus.FAILED)

    def test_expiry_applies_only_before_start(self):
        pending, _ = self.create_command("pending-expiry")
        DeviceCommand.objects.filter(id=pending.id).update(expires_at=timezone.now() - timedelta(seconds=1))
        sweep_device_commands(tenant=self.tenant)
        pending.refresh_from_db()
        self.assertEqual(pending.state, DeviceCommand.State.EXPIRED)
        self.assertIsNone(pending.started_at)

        executing, _ = self.create_command("executing-expiry")
        claimed = self.claim()
        token = claimed.data["command"]["lease_token"]
        self.assertEqual(self.start(executing, token).status_code, 200)
        DeviceCommand.objects.filter(id=executing.id).update(expires_at=timezone.now() - timedelta(seconds=1))
        sweep_device_commands(tenant=self.tenant)
        executing.refresh_from_db()
        self.assertEqual(executing.state, DeviceCommand.State.EXECUTING)

    def test_old_device_cannot_claim_after_takeover(self):
        command, _ = self.create_command("takeover-command")
        takeover = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-b",
                "takeover": True,
                "events_consent": True,
                "reminders_consent": True,
            },
            format="json",
        )
        self.assertEqual(takeover.status_code, 200)
        denied = self.claim()
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.data["error"], "stale_gateway")
        command.refresh_from_db()
        self.assertEqual(command.state, DeviceCommand.State.CANCELLED)


class DatebookDisableAndDeletionTests(DatebookAPIMixin, TestCase):
    chat_id = 920004

    def test_disable_cancels_never_started_increments_epoch_and_optional_purge(self):
        opened = self.open_run("disable-open-run")
        staged = _zoned_event("disable-staged-event")
        self.assertEqual(self.stage_page(opened.data["run_id"], events=[staged]).status_code, 200)
        command, _ = create_device_command(
            self.tenant,
            request_id="disable-command",
            command_type=DeviceCommand.CommandType.REMINDER_CREATE,
            payload={"items": [{"title": "Call"}]},
        )
        MirrorEvent.objects.create(
            tenant=self.tenant,
            source_key=_source_key("disable-event"),
            content_hash=_source_key("disable-content"),
            time_kind="all_day",
            all_day_start_date=timezone.localdate(),
            all_day_end_date_exclusive=timezone.localdate() + timedelta(days=1),
            active=True,
        )
        gateway = DatebookGateway.objects.get(tenant=self.tenant, status=DatebookGateway.Status.ACTIVE)
        epoch = gateway.gateway_epoch
        pending_config_version = self.tenant.pending_config_version
        disable_datebook(self.tenant, purge=False)
        gateway.refresh_from_db()
        command.refresh_from_db()
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.datebook_enabled)
        self.assertEqual(self.tenant.pending_config_version, pending_config_version + 1)
        self.assertEqual(gateway.gateway_epoch, epoch + 1)
        self.assertEqual(command.state, DeviceCommand.State.CANCELLED)
        self.assertTrue(MirrorEvent.objects.filter(tenant=self.tenant).exists())
        self.assertEqual(SyncRun.objects.get(id=opened.data["run_id"]).state, SyncRun.State.ABORTED)
        self.assertFalse(MirrorEvent.objects.filter(source_key=staged["source_key"]).exists())

        disable_datebook(self.tenant, purge=True)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pending_config_version, pending_config_version + 2)
        self.assertFalse(MirrorEvent.objects.filter(tenant=self.tenant).exists())

    def test_tenant_hard_delete_cascades_every_datebook_row(self):
        opened = self.open_run("cascade-run")
        item = _zoned_event("cascade-event")
        self.stage_page(opened.data["run_id"], events=[item])
        create_device_command(
            self.tenant,
            request_id="cascade-command",
            command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
            payload={"items": [{"title": "Cascade"}]},
        )
        tenant_id = self.tenant.id
        self.tenant.delete()
        self.assertFalse(DatebookGateway.objects.filter(tenant_id=tenant_id).exists())
        self.assertFalse(SyncRun.objects.filter(tenant_id=tenant_id).exists())
        self.assertFalse(SyncPage.objects.filter(tenant_id=tenant_id).exists())
        self.assertFalse(MirrorEvent.objects.filter(tenant_id=tenant_id).exists())
        self.assertFalse(MirrorReminder.objects.filter(tenant_id=tenant_id).exists())
        self.assertFalse(DeviceCommand.objects.filter(tenant_id=tenant_id).exists())


class DatebookConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.tenant = _ready_tenant(920005)
        self.gateway = DatebookGateway.objects.create(tenant=self.tenant, installation_id="install-a")

    def _command(self, request_id, items=1):
        return create_device_command(
            self.tenant,
            request_id=request_id,
            command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
            payload={"items": [{"title": f"Item {index}"} for index in range(items)]},
        )

    def test_calendar_put_and_sync_open_do_not_deadlock(self):
        from django.db import connection

        from . import services

        a_holds_tenant = threading.Event()
        release_a = threading.Event()
        b_done = threading.Event()
        errors = []
        original_gateway_lock = services._locked_active_gateway

        def gated_gateway_lock(tenant):
            # Thread A pauses between its tenant lock and its gateway lock — the
            # exact window production hits while PUT calendars authors PII.
            if threading.current_thread().name == "put-calendars":
                a_holds_tenant.set()
                assert release_a.wait(timeout=15)
            return original_gateway_lock(tenant)

        def put_calendars():
            close_old_connections()
            try:
                services.replace_calendar_contexts(
                    Tenant.objects.get(id=self.tenant.id),
                    installation_id="install-a",
                    gateway_epoch=1,
                    calendars=[
                        {
                            "calendar_fingerprint": _source_key("deadlock-calendar"),
                            "entity_scope": "event",
                            "included": True,
                            "container_title": "Family",
                            "source_title": "iCloud",
                            "source_type": "icloud",
                            "context_note": "Shared family calendar",
                        }
                    ],
                )
            except Exception as exc:  # noqa: BLE001 — the assertion is "no error of any kind"
                errors.append(("put", repr(exc)))
            finally:
                close_old_connections()

        def sync_open():
            close_old_connections()
            try:
                services.open_sync_run(
                    Tenant.objects.get(id=self.tenant.id),
                    installation_id="install-a",
                    gateway_epoch=1,
                    client_run_id="run-deadlock",
                    events={"authorization": "full_access", "coverage_complete": True},
                    reminders=None,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(("open", repr(exc)))
            finally:
                b_done.set()
                close_old_connections()

        def another_backend_waits_on_a_lock() -> bool:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND wait_event_type = 'Lock'"
                )
                return cursor.fetchone()[0] > 0

        with patch.object(services, "_locked_active_gateway", gated_gateway_lock):
            thread_a = threading.Thread(target=put_calendars, name="put-calendars")
            thread_b = threading.Thread(target=sync_open, name="sync-open")
            thread_a.start()
            self.assertTrue(a_holds_tenant.wait(timeout=15))
            thread_b.start()
            deadline = monotonic() + 10
            while not b_done.is_set() and not another_backend_waits_on_a_lock() and monotonic() < deadline:
                sleep(0.05)
            release_a.set()
            thread_a.join(timeout=30)
            thread_b.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(SyncRun.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(CalendarContext.objects.filter(tenant=self.tenant).count(), 1)

    def test_two_claim_racers_have_one_winner(self):
        command, _ = self._command("race-claim")
        barrier = threading.Barrier(2)
        results = []

        def worker():
            close_old_connections()
            barrier.wait()
            try:
                claimed = claim_device_command(
                    Tenant.objects.get(id=self.tenant.id),
                    installation_id="install-a",
                    gateway_epoch=1,
                )
                results.append(str(claimed.id) if claimed else None)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(str(command.id)), 1)
        self.assertEqual(results.count(None), 1)

    def test_combined_twenty_per_day_cap_is_atomic_under_concurrency(self):
        for index in range(3):
            self._command(f"seed-{index}", items=5)
        barrier = threading.Barrier(2)
        results = []

        def worker(index):
            close_old_connections()
            barrier.wait()
            try:
                _command, created = create_device_command(
                    Tenant.objects.get(id=self.tenant.id),
                    request_id=f"racer-{index}",
                    command_type=DeviceCommand.CommandType.REMINDER_CREATE,
                    payload={"items": [{"title": f"Racer {item}"} for item in range(5)]},
                )
                results.append("created" if created else "idempotent")
            except ProtocolError as exc:
                results.append(exc.code)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("created"), 1)
        self.assertEqual(results.count("daily_command_cap"), 1)
        self.assertEqual(
            DeviceCommand.objects.filter(tenant=self.tenant).aggregate(total=Sum("item_count"))["total"],
            20,
        )
