"""Tests for ``apps.insights.yesterdays_signals.compute``.

Kept in a separate file from ``tests.py`` (which is already 1000+ lines and
covers the unrelated pillar/topic register).
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta

from django.test import TestCase, override_settings

from apps.fuel.models import Workout, WorkoutCategory, WorkoutStatus
from apps.journal.models import JournalEntry
from apps.lessons.models import Lesson
from apps.tenants.models import Tenant, User
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .yesterdays_signals import (
    NOTABLE_ENERGY_STALE_DAYS,
    NOTABLE_FUEL_QUIET_DAYS,
    NOTABLE_JOURNAL_DARK_DAYS,
    compute,
)


def _make_tenant(*, chat_id: int, tz: str = "UTC") -> Tenant:
    tenant = create_tenant(display_name=f"YS-{chat_id}", telegram_chat_id=chat_id)
    User.objects.filter(pk=tenant.user_id).update(timezone=tz)
    Tenant.objects.filter(pk=tenant.pk).update(status=Tenant.Status.ACTIVE)
    tenant.refresh_from_db()
    tenant.user.refresh_from_db()
    return tenant


def _add_workout(tenant: Tenant, *, on_date: date, status: str = WorkoutStatus.DONE) -> Workout:
    return Workout.objects.create(
        tenant=tenant,
        date=on_date,
        status=status,
        category=WorkoutCategory.STRENGTH,
        activity="Push",
    )


def _add_journal(tenant: Tenant, *, on_date: date, energy: str = JournalEntry.Energy.MEDIUM) -> JournalEntry:
    return JournalEntry.objects.create(
        tenant=tenant,
        date=on_date,
        mood="ok",
        energy=energy,
        raw_text="...",
    )


def _add_approved_lesson(tenant: Tenant, *, approved_at: datetime) -> Lesson:
    lesson = Lesson.objects.create(
        tenant=tenant,
        text="An insight",
        source_type="conversation",
        status="approved",
        approved_at=approved_at,
    )
    return lesson


class YesterdaysSignalsEmptyTenantTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant(chat_id=900_001)
        # Anchor "now" so the test is deterministic across runs.
        self.now = datetime(2026, 5, 23, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))

    def test_empty_tenant_returns_zeroed_signals(self):
        signals = compute(self.tenant, now=self.now)

        self.assertEqual(signals["today_date"], "2026-05-23")
        self.assertEqual(signals["yesterday_date"], "2026-05-22")

        self.assertEqual(signals["fuel"]["yesterday"]["workouts_done"], 0)
        self.assertEqual(signals["fuel"]["today_so_far"]["workouts_done"], 0)
        self.assertIsNone(signals["fuel"]["days_since_last_workout"])

        self.assertEqual(signals["journal"]["yesterday"]["entries"], 0)
        self.assertIsNone(signals["journal"]["yesterday"]["energy"])
        self.assertIsNone(signals["journal"]["days_since_last_entry"])
        self.assertIsNone(signals["journal"]["last_energy_reading"])

        self.assertEqual(signals["lessons"]["yesterday"]["approved"], 0)
        self.assertEqual(signals["lessons"]["pending"], 0)

        # An empty tenant has no "days since last" to flag either — they may
        # have never logged anything, which is different from "stopped logging".
        self.assertEqual(signals["notable_gaps"], [])

    def test_core_pillar_zeroed_for_empty_tenant(self):
        # Core now has a data model, so it's included (zeroed), not omitted.
        signals = compute(self.tenant, now=self.now)
        self.assertIn("core", signals)
        self.assertEqual(signals["core"]["yesterday"]["sessions"], 0)
        self.assertEqual(signals["core"]["today_so_far"]["sessions"], 0)
        self.assertIsNone(signals["core"]["days_since_last_session"])

    def test_gravity_omitted_when_not_finance_active(self):
        # Default tenant isn't finance_active → the Gravity block stays out.
        signals = compute(self.tenant, now=self.now)
        self.assertNotIn("gravity", signals)

    def test_journal_carries_goals_and_tasks_keys(self):
        signals = compute(self.tenant, now=self.now)
        self.assertEqual(signals["journal"]["active_goals"], 0)
        self.assertEqual(signals["journal"]["yesterday"]["tasks_completed"], 0)


class YesterdaysSignalsActiveTenantTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant(chat_id=900_002)
        self.now = datetime(2026, 5, 23, 9, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.yesterday = date(2026, 5, 22)
        self.today = date(2026, 5, 23)

    def test_active_yesterday_no_gaps_flagged(self):
        _add_workout(self.tenant, on_date=self.yesterday)
        _add_journal(self.tenant, on_date=self.yesterday, energy=JournalEntry.Energy.HIGH)
        _add_approved_lesson(
            self.tenant,
            approved_at=datetime(2026, 5, 22, 18, 0, tzinfo=zoneinfo.ZoneInfo("UTC")),
        )

        signals = compute(self.tenant, now=self.now)

        self.assertEqual(signals["fuel"]["yesterday"]["workouts_done"], 1)
        self.assertEqual(signals["fuel"]["days_since_last_workout"], 1)
        self.assertEqual(signals["journal"]["yesterday"]["entries"], 1)
        self.assertEqual(signals["journal"]["yesterday"]["energy"], "high")
        self.assertEqual(signals["journal"]["days_since_last_entry"], 1)
        self.assertEqual(signals["journal"]["last_energy_reading"]["value"], "high")
        self.assertEqual(signals["journal"]["last_energy_reading"]["days_ago"], 1)
        self.assertEqual(signals["lessons"]["yesterday"]["approved"], 1)

        self.assertEqual(signals["notable_gaps"], [])

    def test_late_logged_workout_counted_in_today_so_far(self):
        # Workout dated today (logged this morning before heartbeat fired).
        _add_workout(self.tenant, on_date=self.today)
        signals = compute(self.tenant, now=self.now)

        self.assertEqual(signals["fuel"]["yesterday"]["workouts_done"], 0)
        self.assertEqual(signals["fuel"]["today_so_far"]["workouts_done"], 1)
        # days_since_last_workout looks at date__lt=today, so today's workout
        # doesn't count here — the field is meant for "how long since
        # something happened" framing.
        self.assertIsNone(signals["fuel"]["days_since_last_workout"])

    def test_skipped_workout_does_not_count_as_done(self):
        _add_workout(self.tenant, on_date=self.yesterday, status=WorkoutStatus.SKIPPED)
        signals = compute(self.tenant, now=self.now)
        self.assertEqual(signals["fuel"]["yesterday"]["workouts_done"], 0)
        self.assertIsNone(signals["fuel"]["days_since_last_workout"])

    def test_pending_lesson_counted_separately(self):
        Lesson.objects.create(
            tenant=self.tenant,
            text="proposed",
            source_type="conversation",
            status="pending",
        )
        signals = compute(self.tenant, now=self.now)
        self.assertEqual(signals["lessons"]["pending"], 1)
        self.assertEqual(signals["lessons"]["yesterday"]["approved"], 0)


class YesterdaysSignalsNotableGapsTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant(chat_id=900_003)
        self.now = datetime(2026, 5, 23, 9, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.today = date(2026, 5, 23)

    def test_journal_dark_flag_at_threshold(self):
        # Last entry exactly NOTABLE_JOURNAL_DARK_DAYS ago → flag fires.
        old = self.today - timedelta(days=NOTABLE_JOURNAL_DARK_DAYS)
        _add_journal(self.tenant, on_date=old)
        signals = compute(self.tenant, now=self.now)
        self.assertIn(f"journal_dark_{NOTABLE_JOURNAL_DARK_DAYS}_days", signals["notable_gaps"])

    def test_journal_dark_flag_does_not_fire_below_threshold(self):
        # 1 day quiet is normal life.
        _add_journal(self.tenant, on_date=self.today - timedelta(days=1))
        signals = compute(self.tenant, now=self.now)
        self.assertNotIn("journal_dark_1_days", signals["notable_gaps"])

    def test_fuel_quiet_flag_at_threshold(self):
        old = self.today - timedelta(days=NOTABLE_FUEL_QUIET_DAYS)
        _add_workout(self.tenant, on_date=old)
        signals = compute(self.tenant, now=self.now)
        self.assertIn(f"fuel_quiet_{NOTABLE_FUEL_QUIET_DAYS}_days", signals["notable_gaps"])

    def test_energy_stale_flag(self):
        # Last journal entry is older than the energy-staleness threshold.
        old = self.today - timedelta(days=NOTABLE_ENERGY_STALE_DAYS)
        _add_journal(self.tenant, on_date=old, energy=JournalEntry.Energy.LOW)
        signals = compute(self.tenant, now=self.now)
        self.assertIn(f"energy_stale_{NOTABLE_ENERGY_STALE_DAYS}_days", signals["notable_gaps"])
        self.assertEqual(signals["journal"]["last_energy_reading"]["value"], "low")

    def test_multiple_gaps_coexist(self):
        # Six days of nothing — both fuel_quiet and journal_dark fire.
        old = self.today - timedelta(days=6)
        _add_journal(self.tenant, on_date=old)
        _add_workout(self.tenant, on_date=old)
        signals = compute(self.tenant, now=self.now)
        self.assertIn("journal_dark_6_days", signals["notable_gaps"])
        self.assertIn("fuel_quiet_6_days", signals["notable_gaps"])


class YesterdaysSignalsTimezoneTests(TestCase):
    """Lessons use a tz-aware datetime range; Workout/Journal use DateField.

    The risk these tests guard: using Django's ``__date`` lookup on
    ``approved_at`` would extract in the database connection's timezone
    (typically UTC), not the tenant's, and silently misclassify lessons
    approved near midnight tenant-local.
    """

    def test_lesson_approved_late_yesterday_local_is_counted_under_yesterday(self):
        tenant = _make_tenant(chat_id=900_010, tz="Europe/Berlin")
        berlin = zoneinfo.ZoneInfo("Europe/Berlin")

        # Anchor "now" as 09:00 Berlin on May 23 → yesterday = May 22 (Berlin).
        now = datetime(2026, 5, 23, 9, 0, tzinfo=berlin)

        # Lesson approved at 23:30 Berlin on May 22 (which is 21:30 UTC May 22).
        approved_local = datetime(2026, 5, 22, 23, 30, tzinfo=berlin)
        _add_approved_lesson(tenant, approved_at=approved_local)

        signals = compute(tenant, now=now)
        self.assertEqual(signals["yesterday_date"], "2026-05-22")
        self.assertEqual(signals["lessons"]["yesterday"]["approved"], 1)

    def test_lesson_approved_after_midnight_local_is_not_yesterday(self):
        tenant = _make_tenant(chat_id=900_011, tz="Europe/Berlin")
        berlin = zoneinfo.ZoneInfo("Europe/Berlin")

        now = datetime(2026, 5, 23, 9, 0, tzinfo=berlin)
        # Lesson approved 00:30 Berlin on May 23 → today, not yesterday.
        approved_local = datetime(2026, 5, 23, 0, 30, tzinfo=berlin)
        _add_approved_lesson(tenant, approved_at=approved_local)

        signals = compute(tenant, now=now)
        self.assertEqual(signals["lessons"]["yesterday"]["approved"], 0)

    def test_tenant_local_yesterday_differs_from_utc(self):
        # 23:00 UTC May 22 → 01:00 Berlin May 23 → yesterday = May 22 (Berlin)
        tenant = _make_tenant(chat_id=900_012, tz="Europe/Berlin")
        now_utc = datetime(2026, 5, 22, 23, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        signals = compute(tenant, now=now_utc)
        self.assertEqual(signals["today_date"], "2026-05-23")
        self.assertEqual(signals["yesterday_date"], "2026-05-22")


@override_settings(NBHD_INTERNAL_API_KEY="test-runtime-key")
class RuntimeYesterdaysSignalsEndpointTests(TestCase):
    """Auth + happy-path smoke test for the runtime endpoint.

    Heavy timezone / signal-shape coverage lives in the compute tests
    above — this only verifies wiring + auth.
    """

    def setUp(self):
        self.tenant = _make_tenant(chat_id=900_100)
        seed_internal_key(self.tenant)
        self.other_tenant = _make_tenant(chat_id=900_101)

    def _headers(self, *, tenant_id=None, key="test-runtime-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _url(self, tenant=None):
        tid = (tenant or self.tenant).id
        return f"/api/v1/insights/runtime/{tid}/yesterdays-signals/"

    def test_missing_key_401(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 401)

    def test_tenant_scope_mismatch_401(self):
        # Header asserts self.tenant but URL is other_tenant — should reject.
        resp = self.client.get(self._url(tenant=self.other_tenant), **self._headers())
        self.assertEqual(resp.status_code, 401)

    def test_happy_path_returns_signal_shape(self):
        resp = self.client.get(self._url(), **self._headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Spot-check the contract — full shape coverage is in compute tests.
        self.assertIn("as_of", body)
        self.assertIn("yesterday_date", body)
        self.assertIn("today_date", body)
        self.assertIn("fuel", body)
        self.assertIn("journal", body)
        self.assertIn("lessons", body)
        self.assertIn("core", body)
        self.assertIn("notable_gaps", body)
        # Gravity is opt-in — absent for a non-finance-active tenant.
        self.assertNotIn("gravity", body)


class YesterdaysSignalsCoreAndGravityTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant(chat_id=900_050)
        self.now = datetime(2026, 5, 23, 9, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.yesterday = date(2026, 5, 22)
        self.today = date(2026, 5, 23)

    def test_core_sessions_counted(self):
        from apps.core.models import MeditationSession, MeditationStatus

        MeditationSession.objects.create(tenant=self.tenant, date=self.yesterday, status=MeditationStatus.READY)
        MeditationSession.objects.create(tenant=self.tenant, date=self.today, status=MeditationStatus.DELIVERED)
        # A pending sit should not count.
        MeditationSession.objects.create(tenant=self.tenant, date=self.today, status=MeditationStatus.PENDING)

        signals = compute(self.tenant, now=self.now)
        self.assertEqual(signals["core"]["yesterday"]["sessions"], 1)
        self.assertEqual(signals["core"]["today_so_far"]["sessions"], 1)
        self.assertEqual(signals["core"]["days_since_last_session"], 1)

    def test_core_quiet_gap_flagged(self):
        from apps.core.models import MeditationSession, MeditationStatus

        MeditationSession.objects.create(tenant=self.tenant, date=date(2026, 5, 10), status=MeditationStatus.DELIVERED)
        signals = compute(self.tenant, now=self.now)
        gaps = signals["notable_gaps"]
        self.assertTrue(any(g.startswith("core_quiet_") for g in gaps))

    def test_gravity_block_present_and_counts_only_when_finance_active(self):
        from apps.finance.models import FinanceAccount, FinanceTransaction

        Tenant.objects.filter(pk=self.tenant.pk).update(finance_enabled=True)
        self.tenant.refresh_from_db()
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.CHECKING,
            nickname="Everyday",
            current_balance="1000.00",
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=account,
            transaction_type=FinanceTransaction.TransactionType.PAYMENT,
            amount="42.00",
            date=self.yesterday,
        )

        signals = compute(self.tenant, now=self.now)
        self.assertIn("gravity", signals)
        self.assertEqual(signals["gravity"]["yesterday"]["transactions"], 1)
        self.assertEqual(signals["gravity"]["days_since_last_transaction"], 1)
        # Counts only — never amounts (privacy).
        self.assertNotIn("amount", signals["gravity"]["yesterday"])
