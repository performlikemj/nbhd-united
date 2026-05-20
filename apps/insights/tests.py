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
from apps.journal.models import Document, Goal
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .baselines import compute_baseline
from .models import AssistantInsight, PillarSnapshot, TopicAlias, TopicRegistry, UserVoicePref
from .pillars import Pillar
from .seed import seed_topics
from .signals import compute_signals
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


# ── Phase 2 ──────────────────────────────────────────────────────────────


def _seed_gravity_topic(slug: str = "debt") -> TopicRegistry:
    return TopicRegistry.objects.create(
        pillar=Pillar.GRAVITY.value,
        slug=slug,
        display_name=slug.title(),
        status=TopicRegistry.Status.CANONICAL,
        source=TopicRegistry.Source.SEED,
    )


def _make_weekly_debt_snapshot(tenant, *, weeks_ago: int, debt: str) -> PillarSnapshot:
    return PillarSnapshot.objects.create(
        tenant=tenant,
        pillar=Pillar.GRAVITY.value,
        granularity=PillarSnapshot.Granularity.WEEKLY,
        ts=timezone.now() - timedelta(weeks=weeks_ago),
        payload={
            "schema_version": 1,
            "totals": {"debt": debt, "savings": "0", "minimum_payments": "0"},
        },
    )


class ComputeBaselineTests(TestCase):
    def setUp(self):
        self.tenant = _make_finance_tenant(display_name="Baseline", chat_id=900500)
        _seed_gravity_topic("debt")

    def test_unsupported_topic_returns_supported_false(self):
        out = compute_baseline(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="dining")
        self.assertFalse(out["supported"])
        self.assertEqual(out["sample_size"], 0)

    def test_empty_history_returns_supported_true_zero_samples(self):
        out = compute_baseline(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertTrue(out["supported"])
        self.assertEqual(out["sample_size"], 0)
        self.assertIsNone(out["mean"])

    def test_single_point_mean_only_stdev_zero(self):
        _make_weekly_debt_snapshot(self.tenant, weeks_ago=0, debt="1000")
        out = compute_baseline(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertEqual(out["sample_size"], 1)
        self.assertEqual(out["mean"], 1000.0)
        self.assertEqual(out["stdev"], 0.0)
        self.assertEqual(out["latest_z"], 0.0)

    def test_multi_point_mean_stdev_trend(self):
        # 4 weeks rising debt: 1000, 1200, 1500, 2000
        for weeks_ago, debt in [(3, "1000"), (2, "1200"), (1, "1500"), (0, "2000")]:
            _make_weekly_debt_snapshot(self.tenant, weeks_ago=weeks_ago, debt=debt)
        out = compute_baseline(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt", window_weeks=8)
        self.assertEqual(out["sample_size"], 4)
        self.assertAlmostEqual(out["mean"], 1425.0, places=1)
        self.assertGreater(out["trend"], 0)  # rising
        self.assertEqual(out["latest"], 2000.0)
        # latest above mean → positive z
        self.assertGreater(out["latest_z"], 0)

    def test_window_bounds_results(self):
        # One inside window, one outside
        _make_weekly_debt_snapshot(self.tenant, weeks_ago=2, debt="1000")
        _make_weekly_debt_snapshot(self.tenant, weeks_ago=20, debt="500")
        out = compute_baseline(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt", window_weeks=4)
        self.assertEqual(out["sample_size"], 1)
        self.assertEqual(out["mean"], 1000.0)


@override_settings(NBHD_INTERNAL_API_KEY="test-runtime-key")
class Phase2EndpointTests(TestCase):
    """Covers both user-facing (JWT) and runtime (internal-key) endpoint sets."""

    def setUp(self):
        self.tenant = _make_finance_tenant(display_name="P2", chat_id=900600)
        self.other_tenant = _make_finance_tenant(display_name="P2Other", chat_id=900601)

        # JWT client for user-facing tests
        self.jwt_client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.jwt_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

        # Seed canonical 'debt' topic so resolve_topic finds it
        self.debt_topic = _seed_gravity_topic("debt")

    def _runtime_headers(self, tenant_id=None, key="test-runtime-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    # ── baseline ───────────────────────────────────────────────────────
    def test_user_baseline_returns_stats(self):
        _make_weekly_debt_snapshot(self.tenant, weeks_ago=0, debt="1500")
        resp = self.jwt_client.get("/api/v1/insights/baseline/?pillar=gravity&topic=debt")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["topic"], "debt")
        self.assertEqual(resp.json()["sample_size"], 1)

    def test_user_baseline_missing_topic_400(self):
        resp = self.jwt_client.get("/api/v1/insights/baseline/?pillar=gravity")
        self.assertEqual(resp.status_code, 400)

    def test_runtime_baseline_returns_stats(self):
        _make_weekly_debt_snapshot(self.tenant, weeks_ago=0, debt="2000")
        resp = self.client.get(
            f"/api/v1/insights/runtime/{self.tenant.id}/baseline/?pillar=gravity&topic=debt",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["latest"], 2000.0)

    def test_runtime_baseline_missing_key_401(self):
        resp = self.client.get(f"/api/v1/insights/runtime/{self.tenant.id}/baseline/?pillar=gravity&topic=debt")
        self.assertEqual(resp.status_code, 401)

    # ── record ─────────────────────────────────────────────────────────
    def test_user_record_creates_open_insight(self):
        resp = self.jwt_client.post(
            "/api/v1/insights/insights/record/",
            data={
                "pillar": "gravity",
                "topic": "debt",
                "statement": "Debt is trending up.",
                "evidence_refs": {"window": "8w"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["status"], "open")
        self.assertEqual(body["topic_slug"], "debt")
        self.assertEqual(body["statement"], "Debt is trending up.")
        self.assertEqual(AssistantInsight.objects.filter(tenant=self.tenant).count(), 1)

    def test_record_auto_proposes_new_topic(self):
        resp = self.jwt_client.post(
            "/api/v1/insights/insights/record/",
            data={
                "pillar": "gravity",
                "topic": "Vintage Wine Spend",
                "statement": "Unusual wine purchases the last 3 weeks.",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        topic_slug = resp.json()["topic_slug"]
        proposed = TopicRegistry.objects.get(slug=topic_slug)
        self.assertEqual(proposed.status, TopicRegistry.Status.PROPOSED)

    def test_record_rejects_invalid_pillar(self):
        resp = self.jwt_client.post(
            "/api/v1/insights/insights/record/",
            data={"pillar": "fuel", "topic": "sleep_quality", "statement": "."},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_record_rejects_missing_statement(self):
        resp = self.jwt_client.post(
            "/api/v1/insights/insights/record/",
            data={"pillar": "gravity", "topic": "debt"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_record_rejects_invalid_confidence(self):
        resp = self.jwt_client.post(
            "/api/v1/insights/insights/record/",
            data={
                "pillar": "gravity",
                "topic": "debt",
                "statement": ".",
                "confidence": 2.5,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_runtime_record_creates_open_insight(self):
        resp = self.client.post(
            f"/api/v1/insights/runtime/{self.tenant.id}/insights/record/",
            data={"pillar": "gravity", "topic": "debt", "statement": "Runtime path."},
            content_type="application/json",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["status"], "open")

    # ── list ───────────────────────────────────────────────────────────
    def test_user_list_returns_tenant_insights_newest_first(self):
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Older",
        )
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Newer",
        )
        resp = self.jwt_client.get("/api/v1/insights/insights/?pillar=gravity")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["insights"][0]["statement"], "Newer")

    def test_list_isolates_tenant(self):
        AssistantInsight.objects.create(
            tenant=self.other_tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Other tenant",
        )
        resp = self.jwt_client.get("/api/v1/insights/insights/")
        self.assertEqual(resp.json()["count"], 0)

    def test_list_filter_by_status(self):
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Open one",
            status=AssistantInsight.Status.OPEN,
        )
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Confirmed one",
            status=AssistantInsight.Status.CONFIRMED,
        )
        resp = self.jwt_client.get("/api/v1/insights/insights/?status=confirmed")
        self.assertEqual(resp.json()["count"], 1)
        self.assertEqual(resp.json()["insights"][0]["statement"], "Confirmed one")

    def test_list_rejects_invalid_status(self):
        resp = self.jwt_client.get("/api/v1/insights/insights/?status=garbage")
        self.assertEqual(resp.status_code, 400)

    def test_runtime_list_returns_insights(self):
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Visible via runtime",
        )
        resp = self.client.get(
            f"/api/v1/insights/runtime/{self.tenant.id}/insights/?pillar=gravity",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)

    # ── confirm / refute ───────────────────────────────────────────────
    def test_confirm_flips_status(self):
        ins = AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Pending",
        )
        resp = self.jwt_client.post(
            f"/api/v1/insights/insights/{ins.id}/confirm/",
            data={"note": "yep"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        ins.refresh_from_db()
        self.assertEqual(ins.status, AssistantInsight.Status.CONFIRMED)
        self.assertIsNotNone(ins.last_confirmed_at)
        self.assertEqual(len(ins.user_responses), 1)
        self.assertEqual(ins.user_responses[0]["kind"], "confirm")
        self.assertEqual(ins.user_responses[0]["note"], "yep")

    def test_refute_flips_status_and_keeps_row(self):
        ins = AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Pending",
        )
        resp = self.jwt_client.post(
            f"/api/v1/insights/insights/{ins.id}/refute/",
            data={"note": "wedding"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        ins.refresh_from_db()
        self.assertEqual(ins.status, AssistantInsight.Status.REFUTED)
        self.assertIsNotNone(ins.last_refuted_at)
        # Row preserved for memory
        self.assertEqual(AssistantInsight.objects.filter(id=ins.id).count(), 1)

    def test_confirm_cross_tenant_404(self):
        ins = AssistantInsight.objects.create(
            tenant=self.other_tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="Other tenant's",
        )
        resp = self.jwt_client.post(f"/api/v1/insights/insights/{ins.id}/confirm/")
        self.assertEqual(resp.status_code, 404)

    def test_runtime_confirm_flips_status(self):
        ins = AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt_topic,
            statement="To confirm via runtime",
        )
        resp = self.client.post(
            f"/api/v1/insights/runtime/{self.tenant.id}/insights/{ins.id}/confirm/",
            data={},
            content_type="application/json",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        ins.refresh_from_db()
        self.assertEqual(ins.status, AssistantInsight.Status.CONFIRMED)


# ── Phase 3 — signals + voice prefs ──────────────────────────────────────


class ComputeSignalsTests(TestCase):
    def setUp(self):
        self.tenant = _make_finance_tenant(display_name="P3Sig", chat_id=900800)
        self.debt = _seed_gravity_topic("debt")

    def test_unknown_topic_returns_stub(self):
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="not_a_topic")
        self.assertFalse(out["topic_known"])
        self.assertEqual(out["hard_floors"]["reason"], "topic_unknown")
        self.assertFalse(out["hard_floors"]["can_be_direct"])
        self.assertFalse(out["hard_floors"]["can_exceed_observation"])

    def test_empty_history_floors_block_everything(self):
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertTrue(out["topic_known"])
        self.assertEqual(out["data"]["sample_size"], 0)
        self.assertFalse(out["hard_floors"]["can_be_direct"])
        self.assertFalse(out["hard_floors"]["can_exceed_observation"])
        self.assertIsNone(out["calibration"]["ratio"])
        self.assertEqual(out["user_voice_pref"]["register_offset"], 0)
        self.assertFalse(out["intent"]["has_stated_goal"])

    def test_sample_floor_lifts_at_four(self):
        for w in range(4):
            _make_weekly_debt_snapshot(self.tenant, weeks_ago=w, debt=str(1000 + w * 100))
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertEqual(out["data"]["sample_size"], 4)
        self.assertTrue(out["hard_floors"]["can_be_direct"])
        self.assertFalse(out["hard_floors"]["can_exceed_observation"])

    def test_observation_floor_lifts_at_three_responses(self):
        for w in range(4):
            _make_weekly_debt_snapshot(self.tenant, weeks_ago=w, debt="1000")
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
            statement="a",
            status=AssistantInsight.Status.CONFIRMED,
        )
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
            statement="b",
            status=AssistantInsight.Status.CONFIRMED,
        )
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
            statement="c",
            status=AssistantInsight.Status.REFUTED,
        )
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertEqual(out["calibration"]["response_total"], 3)
        self.assertAlmostEqual(out["calibration"]["ratio"], 2 / 3, places=3)
        self.assertTrue(out["hard_floors"]["can_be_direct"])
        self.assertTrue(out["hard_floors"]["can_exceed_observation"])

    def test_pillar_goal_counts_for_intent(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="save-5k",
            title="Save $5k by Dec",
            markdown="### Save $5k by Dec\n- Target: $5,000",
            pillar=Pillar.GRAVITY.value,
            topic=None,
        )
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertTrue(out["intent"]["has_stated_goal"])
        self.assertEqual(out["intent"]["goal_scope"], "pillar")
        self.assertIn("Save $5k", out["intent"]["goal_summary"])

    def test_topic_goal_takes_precedence_over_pillar(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="pillar-goal",
            title="Pillar-level goal",
            markdown="",
            pillar=Pillar.GRAVITY.value,
            topic=None,
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="topic-goal",
            title="Topic-specific debt goal",
            markdown="",
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
        )
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertEqual(out["intent"]["goal_scope"], "topic")
        self.assertIn("Topic-specific", out["intent"]["goal_summary"])

    def test_typed_goal_takes_precedence_over_document(self):
        """Post-#624 dual-read: a typed ACTIVE Goal wins over a legacy Document."""
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="legacy-doc-goal",
            title="Legacy doc goal",
            markdown="### Legacy",
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
        )
        Goal.objects.create(
            tenant=self.tenant,
            title="Typed Goal — pay off card",
            description="Target: zero balance",
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
            status=Goal.Status.ACTIVE,
        )
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertTrue(out["intent"]["has_stated_goal"])
        self.assertEqual(out["intent"]["goal_scope"], "topic")
        self.assertIn("Typed Goal", out["intent"]["goal_summary"])

    def test_typed_goal_falls_back_to_document_when_absent(self):
        """No typed Goal → still read the legacy Document."""
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="doc-only",
            title="Legacy-only goal",
            markdown="",
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
        )
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertTrue(out["intent"]["has_stated_goal"])
        self.assertEqual(out["intent"]["goal_scope"], "topic")
        self.assertIn("Legacy-only", out["intent"]["goal_summary"])

    def test_typed_goal_inactive_does_not_count_as_intent(self):
        """Only ACTIVE typed Goals satisfy intent; ACHIEVED/ABANDONED don't."""
        Goal.objects.create(
            tenant=self.tenant,
            title="Done goal",
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
            status=Goal.Status.ACHIEVED,
        )
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertFalse(out["intent"]["has_stated_goal"])

    def test_typed_pillar_goal_intent(self):
        """Pillar-scoped typed Goal (topic=null) satisfies pillar-level intent."""
        Goal.objects.create(
            tenant=self.tenant,
            title="Save $5k by Dec",
            description="rolling target",
            pillar=Pillar.GRAVITY.value,
            topic=None,
            status=Goal.Status.ACTIVE,
        )
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertTrue(out["intent"]["has_stated_goal"])
        self.assertEqual(out["intent"]["goal_scope"], "pillar")
        self.assertIn("Save $5k", out["intent"]["goal_summary"])

    def test_topic_specific_voice_pref_wins_over_pillar_wide(self):
        UserVoicePref.objects.create(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic=None, register_offset=-1)
        UserVoicePref.objects.create(
            tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic=self.debt, register_offset=1
        )
        out = compute_signals(tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic_slug="debt")
        self.assertEqual(out["user_voice_pref"]["register_offset"], 1)
        self.assertEqual(out["user_voice_pref"]["scope"], "topic")


@override_settings(NBHD_INTERNAL_API_KEY="test-runtime-key")
class Phase3EndpointTests(TestCase):
    def setUp(self):
        self.tenant = _make_finance_tenant(display_name="P3End", chat_id=900900)
        self.other_tenant = _make_finance_tenant(display_name="P3Other", chat_id=900901)
        self.client_jwt = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client_jwt.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        self.debt = _seed_gravity_topic("debt")

    def _runtime_headers(self, tenant_id=None, key="test-runtime-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def test_user_signals_returns_full_shape(self):
        _make_weekly_debt_snapshot(self.tenant, weeks_ago=0, debt="1000")
        resp = self.client_jwt.get("/api/v1/insights/signals/?pillar=gravity&topic=debt")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("data", "calibration", "intent", "user_voice_pref", "hard_floors"):
            self.assertIn(key, body)
        self.assertTrue(body["topic_known"])

    def test_user_signals_missing_topic_400(self):
        resp = self.client_jwt.get("/api/v1/insights/signals/?pillar=gravity")
        self.assertEqual(resp.status_code, 400)

    def test_runtime_signals_missing_key_401(self):
        resp = self.client.get(f"/api/v1/insights/runtime/{self.tenant.id}/signals/?pillar=gravity&topic=debt")
        self.assertEqual(resp.status_code, 401)

    def test_runtime_signals_returns_breakdown(self):
        resp = self.client.get(
            f"/api/v1/insights/runtime/{self.tenant.id}/signals/?pillar=gravity&topic=debt",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["topic_known"])

    def test_set_voice_pref_creates_topic_scoped(self):
        resp = self.client_jwt.post(
            "/api/v1/insights/voice-prefs/set/",
            data={"pillar": "gravity", "topic": "debt", "register_offset": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["register_offset"], 1)
        self.assertEqual(body["topic_slug"], "debt")
        self.assertEqual(body["scope"], "topic")

    def test_set_voice_pref_pillar_wide_when_topic_omitted(self):
        resp = self.client_jwt.post(
            "/api/v1/insights/voice-prefs/set/",
            data={"pillar": "gravity", "register_offset": -1},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["topic_slug"])
        self.assertEqual(resp.json()["scope"], "pillar")

    def test_set_voice_pref_idempotent_upsert(self):
        self.client_jwt.post(
            "/api/v1/insights/voice-prefs/set/",
            data={"pillar": "gravity", "topic": "debt", "register_offset": 1},
            format="json",
        )
        resp = self.client_jwt.post(
            "/api/v1/insights/voice-prefs/set/",
            data={"pillar": "gravity", "topic": "debt", "register_offset": -1},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["register_offset"], -1)
        self.assertEqual(
            UserVoicePref.objects.filter(tenant=self.tenant, pillar="gravity", topic=self.debt).count(),
            1,
        )

    def test_set_voice_pref_rejects_invalid_offset(self):
        resp = self.client_jwt.post(
            "/api/v1/insights/voice-prefs/set/",
            data={"pillar": "gravity", "topic": "debt", "register_offset": 5},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_set_voice_pref_rejects_invalid_pillar(self):
        resp = self.client_jwt.post(
            "/api/v1/insights/voice-prefs/set/",
            data={"pillar": "fuel", "topic": "sleep_quality", "register_offset": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_list_voice_prefs_isolates_tenant(self):
        UserVoicePref.objects.create(
            tenant=self.tenant, pillar=Pillar.GRAVITY.value, topic=self.debt, register_offset=1
        )
        UserVoicePref.objects.create(
            tenant=self.other_tenant, pillar=Pillar.GRAVITY.value, topic=None, register_offset=-1
        )
        resp = self.client_jwt.get("/api/v1/insights/voice-prefs/")
        self.assertEqual(resp.json()["count"], 1)

    def test_runtime_set_voice_pref(self):
        resp = self.client.post(
            f"/api/v1/insights/runtime/{self.tenant.id}/voice-prefs/set/",
            data={"pillar": "gravity", "topic": "debt", "register_offset": 1},
            content_type="application/json",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["register_offset"], 1)

    def test_runtime_set_voice_pref_cross_tenant_401(self):
        resp = self.client.post(
            f"/api/v1/insights/runtime/{self.other_tenant.id}/voice-prefs/set/",
            data={"pillar": "gravity", "topic": "debt", "register_offset": 1},
            content_type="application/json",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.status_code, 401)
