"""Owner web agenda (/api/v1/datebook/agenda/): honest coverage, owner rehydration,
excluded calendars hidden, tenant-scoped, tz/DST-correct day coverage."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.common.tenant_tz import tenant_today, tenant_tz
from apps.tenants.services import create_tenant

from .models import AuthorizationStatus, CalendarContext, DatebookGateway, MirrorEvent
from .owner_agenda import _covered_days


def _key(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class OwnerAgendaTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Agenda Owner", telegram_chat_id=930001)
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _enable(self, *, events=True, reminders=False):
        now = timezone.now()
        self.tenant.datebook_enabled = True
        self.tenant.datebook_manifest_ok = True
        self.tenant.datebook_events_consent_at = now if events else None
        self.tenant.datebook_reminders_consent_at = now if reminders else None
        self.tenant.save()

    def _gateway(self, *, days=7, authorization=AuthorizationStatus.FULL_ACCESS, complete=True):
        tz = tenant_tz(self.tenant)
        today = tenant_today(self.tenant)
        return DatebookGateway.objects.create(
            tenant=self.tenant,
            installation_id="install-a",
            events_authorization=authorization,
            events_full_snapshot_required=not complete,
            events_last_complete_sync_at=timezone.now() - timedelta(minutes=12) if complete else None,
            events_window_start=datetime.combine(today, time.min, tzinfo=tz),
            events_window_end=datetime.combine(today + timedelta(days=days), time.min, tzinfo=tz),
        )

    def _event(self, label, *, title="Dinner", day_offset=0, calendar="cal-a", tenant=None):
        day = tenant_today(self.tenant) + timedelta(days=day_offset)
        return MirrorEvent.objects.create(
            tenant=tenant or self.tenant,
            source_key=_key(label),
            content_hash=_key(label + "c"),
            calendar_fingerprint=_key(calendar),
            time_kind="all_day",
            all_day_start_date=day,
            all_day_end_date_exclusive=day + timedelta(days=1),
            title=title,
            active=True,
        )

    def _get(self, **params):
        resp = self.client.get("/api/v1/datebook/agenda/", params)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data

    def test_disabled_and_unconsented_are_states_not_errors(self):
        self.assertEqual(self._get()["state"], "datebook_disabled")
        self._enable(events=False, reminders=False)
        self.assertEqual(self._get()["state"], "consent_required")

    def test_covered_days_only_inside_a_complete_full_access_sync(self):
        self._enable()
        self._gateway(days=3)
        data = self._get(days=7)
        self.assertEqual(data["state"], "ok")
        today = tenant_today(self.tenant)
        self.assertEqual(data["covered_days"], [(today + timedelta(days=i)).isoformat() for i in range(3)])
        self.assertIsNotNone(data["freshness"]["events_last_complete_sync_at"])
        self.assertEqual(data["requested"]["start_day"], today.isoformat())
        self.assertEqual(data["requested"]["end_day_exclusive"], (today + timedelta(days=7)).isoformat())

    def test_no_coverage_without_full_access_or_complete_sync(self):
        self._enable()
        self._gateway(authorization=AuthorizationStatus.WRITE_ONLY)
        data = self._get()
        self.assertIsNone(data["covered"])
        self.assertEqual(data["covered_days"], [])
        DatebookGateway.objects.all().delete()
        self._gateway(complete=False)
        self.assertEqual(self._get()["covered_days"], [])

    def test_owner_sees_rehydrated_titles(self):
        self._enable()
        self._gateway()
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        self._event("e1", title="[PERSON_1] dinner")
        titles = [item["title"] for item in self._get()["items"]]
        self.assertEqual(titles, ["Alice dinner"])

    def test_excluded_calendar_is_hidden_and_other_tenants_never_show(self):
        self._enable()
        self._gateway()
        self._event("shown", title="Shown", calendar="cal-a")
        self._event("hidden", title="Hidden", calendar="cal-b")
        CalendarContext.objects.create(
            tenant=self.tenant, entity_scope="event", calendar_fingerprint=_key("cal-b"), included=False
        )
        other = create_tenant(display_name="Other", telegram_chat_id=930002)
        self._event("foreign", title="Foreign", tenant=other)
        items = self._get()["items"]
        self.assertEqual([item["title"] for item in items], ["Shown"])
        self.assertNotIn("calendar_fingerprint", items[0])

    def test_all_day_exclusive_end_stays_on_one_day(self):
        self._enable()
        self._gateway()
        self._event("one-day", day_offset=1)
        items = self._get()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["day"], (tenant_today(self.tenant) + timedelta(days=1)).isoformat())

    def test_days_param_is_bounded(self):
        self._enable()
        self.assertEqual(self.client.get("/api/v1/datebook/agenda/", {"days": 0}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/datebook/agenda/", {"days": 99}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/datebook/agenda/", {"days": "x"}).status_code, 400)


class CoveredDaysDstTest(TestCase):
    """A 25-hour DST day is covered only when the whole local day is inside."""

    def test_dst_fall_back_day(self):
        tenant = create_tenant(display_name="NY", telegram_chat_id=930003)
        tenant.user.timezone = "America/New_York"
        tenant.user.save(update_fields=["timezone"])
        ny = ZoneInfo("America/New_York")
        start, end = date(2026, 10, 31), date(2026, 11, 3)
        lo = datetime.combine(start, time.min, tzinfo=ny)
        full = (lo, datetime.combine(end, time.min, tzinfo=ny))
        self.assertEqual(_covered_days(tenant, start, end, full), ["2026-10-31", "2026-11-01", "2026-11-02"])
        # Ending 1 hour into Nov 2 local leaves the 25-hour Nov 1 covered, Nov 2 not.
        short = (lo, datetime.combine(date(2026, 11, 2), time(1, 0), tzinfo=ny))
        self.assertEqual(_covered_days(tenant, start, end, short), ["2026-10-31", "2026-11-01"])
        # A window that stops 30 minutes before local midnight of Nov 2 does NOT cover Nov 1.
        early = (lo, datetime.combine(date(2026, 11, 1), time(23, 30), tzinfo=ny))
        self.assertEqual(_covered_days(tenant, start, end, early), ["2026-10-31"])
