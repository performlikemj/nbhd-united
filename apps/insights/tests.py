"""Tests for the insights app: Phase 0 (topic resolver + seed) and
Phase 1 Day 1 (Gravity snapshot service, weekly task, history/drill/compare
API endpoints)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.finance.models import FinanceAccount, FinanceTransaction
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .models import PillarSnapshot, TopicAlias, TopicRegistry
from .pillars import Pillar
from .seed import seed_topics
from .snapshots import compute_gravity_snapshot
from .tasks import snapshot_gravity_weekly_task
from .topic_resolver import resolve_topic


class TopicResolverTests(TestCase):
    def setUp(self):
        self.canonical = TopicRegistry.objects.create(
            pillar=Pillar.GRAVITY.value,
            slug="dining",
            display_name="Dining",
            status=TopicRegistry.Status.CANONICAL,
            source=TopicRegistry.Source.SEED,
        )
        TopicAlias.objects.create(
            topic=self.canonical,
            alias="eating out",
            source=TopicAlias.Source.SEED,
        )

    def test_exact_slug_match(self):
        topic = resolve_topic(Pillar.GRAVITY.value, "dining")
        self.assertEqual(topic.pk, self.canonical.pk)

    def test_alias_match_case_insensitive(self):
        topic = resolve_topic(Pillar.GRAVITY.value, "Eating Out")
        self.assertEqual(topic.pk, self.canonical.pk)

    def test_alias_match_with_whitespace(self):
        topic = resolve_topic(Pillar.GRAVITY.value, "  eating out  ")
        self.assertEqual(topic.pk, self.canonical.pk)

    def test_proposed_creation_when_no_match(self):
        topic = resolve_topic(Pillar.GRAVITY.value, "Vintage Wine Hunting", model_version="opus-4.7")
        self.assertEqual(topic.status, TopicRegistry.Status.PROPOSED)
        self.assertEqual(topic.slug, "vintage_wine_hunting")
        self.assertEqual(topic.proposed_by_model_version, "opus-4.7")

    def test_proposed_slug_collision_increments(self):
        TopicRegistry.objects.create(
            pillar=Pillar.GRAVITY.value,
            slug="freelance_income",
            display_name="Freelance income",
            status=TopicRegistry.Status.PROPOSED,
            source=TopicRegistry.Source.PROPOSED_BY_MODEL,
        )
        topic = resolve_topic(Pillar.GRAVITY.value, "Freelance Income")
        self.assertEqual(topic.slug, "freelance_income_2")

    def test_pillar_scoped_resolution(self):
        TopicRegistry.objects.create(
            pillar=Pillar.FUEL.value,
            slug="dining",
            display_name="Dining (fuel meaning)",
            status=TopicRegistry.Status.CANONICAL,
            source=TopicRegistry.Source.SEED,
        )
        topic = resolve_topic(Pillar.GRAVITY.value, "dining")
        self.assertEqual(topic.pillar, Pillar.GRAVITY.value)
        self.assertEqual(topic.pk, self.canonical.pk)

    def test_non_canonical_slug_does_not_match(self):
        TopicRegistry.objects.create(
            pillar=Pillar.GRAVITY.value,
            slug="weekend_takeout",
            display_name="Weekend Takeout",
            status=TopicRegistry.Status.PROPOSED,
            source=TopicRegistry.Source.PROPOSED_BY_MODEL,
        )
        # Same string should create a second proposed (collision-suffixed) since
        # the existing row is not canonical, so step 1 misses; step 4 fires.
        topic = resolve_topic(Pillar.GRAVITY.value, "Weekend Takeout")
        self.assertEqual(topic.status, TopicRegistry.Status.PROPOSED)
        self.assertEqual(topic.slug, "weekend_takeout_2")


class SeedTopicsTests(TestCase):
    def test_seed_creates_gravity_and_fuel_canonical_topics(self):
        seed_topics()
        gravity_count = TopicRegistry.objects.filter(
            pillar=Pillar.GRAVITY.value, status=TopicRegistry.Status.CANONICAL
        ).count()
        fuel_count = TopicRegistry.objects.filter(
            pillar=Pillar.FUEL.value, status=TopicRegistry.Status.CANONICAL
        ).count()
        self.assertGreaterEqual(gravity_count, 5)
        self.assertGreaterEqual(fuel_count, 5)

    def test_seed_is_idempotent(self):
        seed_topics()
        first = TopicRegistry.objects.count()
        first_aliases = TopicAlias.objects.count()
        seed_topics()
        self.assertEqual(TopicRegistry.objects.count(), first)
        self.assertEqual(TopicAlias.objects.count(), first_aliases)

    def test_seeded_aliases_resolve_to_canonical(self):
        seed_topics()
        topic = resolve_topic(Pillar.GRAVITY.value, "eating out")
        self.assertEqual(topic.slug, "dining")
        self.assertEqual(topic.status, TopicRegistry.Status.CANONICAL)

    def test_seeded_topic_resolves_by_slug(self):
        seed_topics()
        topic = resolve_topic(Pillar.FUEL.value, "sleep_quality")
        self.assertEqual(topic.slug, "sleep_quality")
        self.assertEqual(topic.pillar, Pillar.FUEL.value)


def _make_finance_tenant(*, display_name: str, chat_id: int) -> Tenant:
    """Create an ACTIVE, finance-enabled tenant for insights tests.

    Uses ``.update()`` rather than ``.save()`` to bypass post-save signals
    (config-version bumps, AGENTS.md refresh, welcome-cron scheduling) that
    would otherwise leave background connections open and break test teardown.
    ``create_tenant`` defaults to ``Status.PENDING``, so we have to flip it.
    """
    tenant = create_tenant(display_name=display_name, telegram_chat_id=chat_id)
    Tenant.objects.filter(pk=tenant.pk).update(
        finance_enabled=True,
        status=Tenant.Status.ACTIVE,
    )
    tenant.refresh_from_db()
    return tenant


class ComputeGravitySnapshotTests(TestCase):
    def setUp(self):
        self.tenant = _make_finance_tenant(display_name="Snap1", chat_id=900100)

    def test_empty_tenant_returns_zeros(self):
        payload = compute_gravity_snapshot(self.tenant)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["totals"]["debt"], "0")
        self.assertEqual(payload["totals"]["savings"], "0")
        self.assertEqual(payload["account_counts"], {"debt": 0, "savings": 0})
        self.assertEqual(payload["accounts"], [])
        self.assertIsNone(payload["active_plan"])
        self.assertEqual(payload["recent_transactions"], [])

    def test_aggregates_active_accounts_only(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1500"),
            original_balance=Decimal("3000"),
            minimum_payment=Decimal("50"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="savings",
            nickname="Sav",
            current_balance=Decimal("400"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Archived",
            current_balance=Decimal("999"),
            is_active=False,
        )

        payload = compute_gravity_snapshot(self.tenant)
        # FinanceAccount stores DecimalField(max_digits=12, decimal_places=2),
        # so Decimal("1500") round-trips as "1500.00".
        self.assertEqual(payload["totals"]["debt"], "1500.00")
        self.assertEqual(payload["totals"]["savings"], "400.00")
        self.assertEqual(payload["totals"]["minimum_payments"], "50.00")
        self.assertEqual(payload["account_counts"], {"debt": 1, "savings": 1})
        self.assertEqual(len(payload["accounts"]), 2)  # archived excluded

    def test_recent_transactions_capped_at_ten(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("100"),
        )
        today = timezone.now().date()
        for i in range(15):
            FinanceTransaction.objects.create(
                tenant=self.tenant,
                account=account,
                transaction_type=FinanceTransaction.TransactionType.PAYMENT,
                amount=Decimal("10"),
                date=today - timedelta(days=i),
                description=f"tx-{i}",
            )
        payload = compute_gravity_snapshot(self.tenant)
        self.assertEqual(len(payload["recent_transactions"]), 10)


class SnapshotGravityWeeklyTaskTests(TestCase):
    def setUp(self):
        self.tenant = _make_finance_tenant(display_name="Task1", chat_id=900200)
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("500"),
        )

    def test_writes_snapshot_for_eligible_tenant(self):
        counts = snapshot_gravity_weekly_task()
        self.assertEqual(counts["written"], 1)
        self.assertEqual(counts["skipped_existing"], 0)
        self.assertEqual(counts["errored"], 0)
        self.assertEqual(
            PillarSnapshot.objects.filter(tenant=self.tenant, pillar=Pillar.GRAVITY.value).count(),
            1,
        )

    def test_idempotent_within_same_iso_week(self):
        snapshot_gravity_weekly_task()
        counts = snapshot_gravity_weekly_task()
        self.assertEqual(counts["written"], 0)
        self.assertEqual(counts["skipped_existing"], 1)
        self.assertEqual(PillarSnapshot.objects.count(), 1)

    def test_skips_hibernated_tenants(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=timezone.now())
        counts = snapshot_gravity_weekly_task()
        self.assertEqual(counts["written"], 0)
        self.assertEqual(PillarSnapshot.objects.count(), 0)

    def test_skips_finance_disabled_tenants(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(finance_enabled=False)
        counts = snapshot_gravity_weekly_task()
        self.assertEqual(counts["written"], 0)
        self.assertEqual(PillarSnapshot.objects.count(), 0)

    def test_skips_inactive_tenants(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.SUSPENDED)
        counts = snapshot_gravity_weekly_task()
        self.assertEqual(counts["written"], 0)
        self.assertEqual(PillarSnapshot.objects.count(), 0)


class InsightsApiTests(TestCase):
    def setUp(self):
        self.tenant = _make_finance_tenant(display_name="API", chat_id=900300)
        self.other_tenant = _make_finance_tenant(display_name="Other", chat_id=900301)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
        )

    def _make_snapshot(self, tenant: Tenant, *, days_ago: int = 0, debt: str = "1000") -> PillarSnapshot:
        return PillarSnapshot.objects.create(
            tenant=tenant,
            pillar=Pillar.GRAVITY.value,
            granularity=PillarSnapshot.Granularity.WEEKLY,
            ts=timezone.now() - timedelta(days=days_ago),
            payload={"schema_version": 1, "totals": {"debt": debt, "savings": "0", "minimum_payments": "0"}},
        )

    # ── history ────────────────────────────────────────────────────────
    def test_history_returns_recent_snapshots(self):
        self._make_snapshot(self.tenant, days_ago=2)
        self._make_snapshot(self.tenant, days_ago=10)
        resp = self.client.get("/api/v1/insights/history/?pillar=gravity&window=8w")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["pillar"], "gravity")
        self.assertEqual(body["count"], 2)

    def test_history_window_bounds_results(self):
        self._make_snapshot(self.tenant, days_ago=2)
        self._make_snapshot(self.tenant, days_ago=60)
        resp = self.client.get("/api/v1/insights/history/?pillar=gravity&window=4w")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)

    def test_history_empty_returns_zero(self):
        resp = self.client.get("/api/v1/insights/history/?pillar=gravity")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 0)

    def test_history_rejects_invalid_pillar(self):
        resp = self.client.get("/api/v1/insights/history/?pillar=fuel")
        self.assertEqual(resp.status_code, 404)

    def test_history_rejects_invalid_window(self):
        resp = self.client.get("/api/v1/insights/history/?pillar=gravity&window=garbage")
        self.assertEqual(resp.status_code, 400)

    def test_history_does_not_leak_other_tenant(self):
        self._make_snapshot(self.other_tenant, days_ago=2)
        resp = self.client.get("/api/v1/insights/history/?pillar=gravity")
        self.assertEqual(resp.json()["count"], 0)

    def test_history_requires_auth(self):
        unauthed = APIClient()
        resp = unauthed.get("/api/v1/insights/history/?pillar=gravity")
        self.assertEqual(resp.status_code, 401)

    # ── snapshot detail ────────────────────────────────────────────────
    def test_snapshot_detail_returns_full_payload(self):
        snap = self._make_snapshot(self.tenant, days_ago=1, debt="1500")
        resp = self.client.get(f"/api/v1/insights/snapshots/{snap.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["payload"]["totals"]["debt"], "1500")

    def test_snapshot_detail_other_tenant_404(self):
        snap = self._make_snapshot(self.other_tenant, days_ago=1)
        resp = self.client.get(f"/api/v1/insights/snapshots/{snap.id}/")
        self.assertEqual(resp.status_code, 404)

    # ── compare ────────────────────────────────────────────────────────
    def test_compare_returns_signed_totals_delta(self):
        a = self._make_snapshot(self.tenant, days_ago=14, debt="2000")
        b = self._make_snapshot(self.tenant, days_ago=0, debt="1500")
        resp = self.client.get(f"/api/v1/insights/compare/?pillar=gravity&period_a={a.id}&period_b={b.id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["totals_delta"]["debt"], "-500")

    def test_compare_requires_both_periods(self):
        resp = self.client.get("/api/v1/insights/compare/?pillar=gravity")
        self.assertEqual(resp.status_code, 400)


@override_settings(NBHD_INTERNAL_API_KEY="test-runtime-key")
class RuntimeInsightsViewTests(TestCase):
    """Internal-runtime endpoints called by the nbhd-insights-tools plugin.

    Authenticates via ``X-NBHD-Internal-Key`` + ``X-NBHD-Tenant-Id`` headers
    (no JWT). Tenant identity comes from the URL; the header must match.
    """

    def setUp(self):
        self.tenant = _make_finance_tenant(display_name="RT", chat_id=900400)
        self.other_tenant = _make_finance_tenant(display_name="RTOther", chat_id=900401)

    def _headers(self, tenant_id=None, key="test-runtime-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _history_url(self, tenant=None, query="?pillar=gravity"):
        tid = (tenant or self.tenant).id
        return f"/api/v1/insights/runtime/{tid}/history/{query}"

    def _snapshot_url(self, snapshot_id, tenant=None):
        tid = (tenant or self.tenant).id
        return f"/api/v1/insights/runtime/{tid}/snapshots/{snapshot_id}/"

    def _compare_url(self, tenant=None, **params):
        tid = (tenant or self.tenant).id
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"/api/v1/insights/runtime/{tid}/compare/?{query}"

    def _make_snapshot(self, tenant: Tenant, *, days_ago: int = 0, debt: str = "1000") -> PillarSnapshot:
        return PillarSnapshot.objects.create(
            tenant=tenant,
            pillar=Pillar.GRAVITY.value,
            granularity=PillarSnapshot.Granularity.WEEKLY,
            ts=timezone.now() - timedelta(days=days_ago),
            payload={"schema_version": 1, "totals": {"debt": debt, "savings": "0", "minimum_payments": "0"}},
        )

    # ── auth ───────────────────────────────────────────────────────────
    def test_runtime_history_missing_key_401(self):
        resp = self.client.get(self._history_url())
        self.assertEqual(resp.status_code, 401)

    def test_runtime_history_wrong_key_401(self):
        resp = self.client.get(self._history_url(), **self._headers(key="wrong"))
        self.assertEqual(resp.status_code, 401)

    def test_runtime_history_tenant_scope_mismatch_401(self):
        # Header says self.tenant, URL is for other_tenant — auth rejects
        resp = self.client.get(
            self._history_url(tenant=self.other_tenant),
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 401)

    # ── history ────────────────────────────────────────────────────────
    def test_runtime_history_returns_snapshots(self):
        self._make_snapshot(self.tenant, days_ago=2)
        resp = self.client.get(self._history_url(), **self._headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["pillar"], "gravity")
        self.assertEqual(body["count"], 1)

    def test_runtime_history_isolates_tenant(self):
        # Other tenant's snapshot must not leak through this tenant's URL+auth
        self._make_snapshot(self.other_tenant, days_ago=2)
        resp = self.client.get(self._history_url(), **self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 0)

    def test_runtime_history_rejects_invalid_pillar(self):
        resp = self.client.get(
            self._history_url(query="?pillar=fuel"),
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 404)

    def test_runtime_history_rejects_invalid_window(self):
        resp = self.client.get(
            self._history_url(query="?pillar=gravity&window=garbage"),
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 400)

    # ── snapshot detail ────────────────────────────────────────────────
    def test_runtime_snapshot_detail_returns_payload(self):
        snap = self._make_snapshot(self.tenant, debt="1500")
        resp = self.client.get(self._snapshot_url(snap.id), **self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["payload"]["totals"]["debt"], "1500")

    def test_runtime_snapshot_detail_cross_tenant_404(self):
        # Snapshot belongs to other_tenant, but URL+auth are for self.tenant
        snap = self._make_snapshot(self.other_tenant)
        resp = self.client.get(self._snapshot_url(snap.id), **self._headers())
        self.assertEqual(resp.status_code, 404)

    # ── compare ────────────────────────────────────────────────────────
    def test_runtime_compare_returns_signed_delta(self):
        a = self._make_snapshot(self.tenant, days_ago=14, debt="2000")
        b = self._make_snapshot(self.tenant, days_ago=0, debt="1500")
        resp = self.client.get(
            self._compare_url(pillar="gravity", period_a=a.id, period_b=b.id),
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["totals_delta"]["debt"], "-500")
