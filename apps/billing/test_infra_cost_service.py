"""Tests for the daily Azure infra-cost refresh.

Covers the two failure modes that let this cron silently serve flat estimates
for months:

1. The request sent Azure a *bare date* ("2026-06-01"); the SDK's strict
   ISO-8601 parser rejected it, the query threw, and the cron quietly fell
   back to estimates. The datetime-format test is the regression guard.
2. Every fallback path (hard query failure *and* a query that returns nothing
   useful) must now raise a tagged Sentry alert so the degradation is visible.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.billing.infra_cost_service import (
    _alert_cost_degradation,
    _query_resource_costs,
    calculate_database_share,
    calculate_platform_share,
    refresh_infra_costs,
)
from apps.billing.models import InfraCostSnapshot
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

MODULE = "apps.billing.infra_cost_service"


class QueryResourceCostsDatetimeTest(TestCase):
    """The bug: a bare date sent to Azure Cost Management is rejected."""

    @override_settings(AZURE_SUBSCRIPTION_ID="sub-123")
    @patch(f"{MODULE}._get_cost_management_client")
    def test_time_period_uses_full_iso_datetime(self, mock_factory):
        mock_client = MagicMock()
        mock_client.query.usage.return_value.rows = []
        mock_factory.return_value = mock_client

        _query_resource_costs(date(2026, 6, 1), date(2026, 6, 24))

        _, kwargs = mock_client.query.usage.call_args
        period = kwargs["parameters"]["time_period"]

        # Regression guard: a bare "2026-06-01" (no "T") is exactly what Azure's
        # deserialize_iso rejected with "Invalid datetime string".
        self.assertIn("T", period["from_property"])
        self.assertIn("T", period["to"])

        # And both must round-trip as real datetimes on the right calendar days.
        self.assertEqual(datetime.fromisoformat(period["from_property"]).date(), date(2026, 6, 1))
        self.assertEqual(datetime.fromisoformat(period["to"]).date(), date(2026, 6, 24))


class RefreshInfraCostsTest(TestCase):
    def setUp(self):
        # CI sets AZURE_MOCK=true (ci-cd.yml), which short-circuits
        # refresh_infra_costs to the estimate path before our patched Azure
        # query runs. Force the real path here; the mock-mode test below
        # re-enables it within its own scope.
        mock_off = patch.dict(os.environ, {"AZURE_MOCK": "false"})
        mock_off.start()
        self.addCleanup(mock_off.stop)

        self.tenant = create_tenant(display_name="Cost Test", telegram_chat_id=700700700)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-abc"
        self.tenant.save(update_fields=["status", "container_id"])

    def _snapshot(self):
        return InfraCostSnapshot.objects.get(tenant=self.tenant)

    def _snapshot_for(self, month):
        return InfraCostSnapshot.objects.get(tenant=self.tenant, month=month)

    def _seed_azure_snapshot(self, month):
        """A real 'azure' snapshot for a given month (prior-month = pipeline
        proven; current-month = real data already collected this month)."""
        return InfraCostSnapshot.objects.create(
            tenant=self.tenant,
            month=month,
            container_cost=Decimal("3.50"),
            storage_cost=Decimal("0.10"),
            database_share=Decimal("8.33"),
            total_cost=Decimal("11.93"),
            source="azure",
        )

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_happy_path_writes_azure_source_no_alert(self, mock_query, mock_alert):
        mock_query.return_value = {"oc-abc": Decimal("3.50"), "ws-abc": Decimal("0.10")}

        result = refresh_infra_costs()

        self.assertFalse(result["degraded"])
        self.assertEqual(result["source"], "azure")
        self.assertEqual(self._snapshot().source, "azure")
        self.assertEqual(self._snapshot().container_cost, Decimal("3.50"))
        mock_alert.assert_not_called()

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_query_failure_falls_back_and_alerts(self, mock_query, mock_alert):
        mock_query.side_effect = RuntimeError("Invalid datetime string: 2026-06-01")

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_query_failed")
        self.assertEqual(self._snapshot().source, "estimate")
        # Alert fired with the exception attached.
        mock_alert.assert_called_once()
        args, kwargs = mock_alert.call_args
        self.assertEqual(args[0], "azure_query_failed")
        self.assertIsInstance(kwargs["exc"], RuntimeError)

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_empty_result_alerts_as_degraded(self, mock_query, mock_alert):
        mock_query.return_value = {}

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_returned_empty")
        self.assertEqual(self._snapshot().source, "estimate")
        mock_alert.assert_called_once()
        self.assertEqual(mock_alert.call_args.args[0], "azure_returned_empty")

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_resources_but_no_tenant_match_alerts(self, mock_query, mock_alert):
        # Azure returned oc-* costs, but none for *our* tenant's container.
        mock_query.return_value = {"oc-someone-else": Decimal("2.00")}

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_no_tenant_match")
        self.assertEqual(self._snapshot().source, "estimate")
        mock_alert.assert_called_once()

    # --- early-month billing-lag grace (month-rollover false-alarm fix) ---

    @patch(f"{MODULE}.timezone")
    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_early_month_empty_with_prior_azure_suppresses_alert(self, mock_query, mock_alert, mock_tz):
        # 1st of the month, Azure hasn't posted July actuals yet (billing lag),
        # and last month DID produce real data → expected, not a degradation.
        mock_tz.now.return_value = datetime(2026, 7, 1, 6, 30)
        self._seed_azure_snapshot(date(2026, 6, 1))
        mock_query.return_value = {}

        result = refresh_infra_costs()

        self.assertFalse(result["degraded"])
        self.assertEqual(result["reason"], "early_month_billing_lag")
        # July snapshot is still written as a conservative estimate placeholder.
        self.assertEqual(self._snapshot_for(date(2026, 7, 1)).source, "estimate")
        mock_alert.assert_not_called()

    @patch(f"{MODULE}.timezone")
    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_early_month_partial_posting_suppresses_alert(self, mock_query, mock_alert, mock_tz):
        # Non-container resources (Django app / storage account) post first, so
        # resource_costs is non-empty but has no oc-* containers →
        # azure_no_container_resources. Still expected billing lag in the window.
        mock_tz.now.return_value = datetime(2026, 7, 2, 6, 30)
        self._seed_azure_snapshot(date(2026, 6, 1))
        mock_query.return_value = {"nbhd-django-westus2": Decimal("5.00")}

        result = refresh_infra_costs()

        self.assertFalse(result["degraded"])
        self.assertEqual(result["reason"], "early_month_billing_lag")
        mock_alert.assert_not_called()

    @patch(f"{MODULE}.timezone")
    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_early_month_without_prior_azure_still_alerts(self, mock_query, mock_alert, mock_tz):
        # No prior-month Azure data → the pipeline never worked; an empty result
        # on day 1 is a genuine break, not lag, so it must alert immediately.
        mock_tz.now.return_value = datetime(2026, 7, 1, 6, 30)
        mock_query.return_value = {}

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_returned_empty")
        mock_alert.assert_called_once()

    @patch(f"{MODULE}.timezone")
    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_mid_month_empty_still_alerts(self, mock_query, mock_alert, mock_tz):
        # Past the grace window an empty result is a real degradation.
        mock_tz.now.return_value = datetime(2026, 7, 15, 6, 30)
        self._seed_azure_snapshot(date(2026, 6, 1))
        mock_query.return_value = {}

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_returned_empty")
        mock_alert.assert_called_once()

    @patch(f"{MODULE}.timezone")
    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_no_tenant_match_alerts_even_in_grace_window(self, mock_query, mock_alert, mock_tz):
        # Resources + containers WERE found, just none matching our tenant — a
        # naming/config break, not billing lag. Must alert even on the 1st.
        mock_tz.now.return_value = datetime(2026, 7, 1, 6, 30)
        self._seed_azure_snapshot(date(2026, 6, 1))
        mock_query.return_value = {"oc-someone-else": Decimal("2.00")}

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_no_tenant_match")
        mock_alert.assert_called_once()

    # --- non-destructive writes: a transient miss must not wipe real data ---

    @patch(f"{MODULE}.timezone")
    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_transient_empty_preserves_real_azure_row(self, mock_query, mock_alert, mock_tz):
        # Real Azure data already collected earlier this month; a later empty
        # return mid-month must keep it, not downgrade to an estimate.
        mock_tz.now.return_value = datetime(2026, 7, 15, 6, 30)
        self._seed_azure_snapshot(date(2026, 7, 1))
        mock_query.return_value = {}

        result = refresh_infra_costs()

        self.assertFalse(result["degraded"])
        self.assertEqual(result["tenants_preserved_real"], 1)
        snap = self._snapshot_for(date(2026, 7, 1))
        self.assertEqual(snap.source, "azure")
        self.assertEqual(snap.container_cost, Decimal("3.50"))
        mock_alert.assert_not_called()

    @patch(f"{MODULE}.timezone")
    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_query_failure_preserves_real_azure_row(self, mock_query, mock_alert, mock_tz):
        # A hard query failure (e.g. 429) still alerts, but must not overwrite an
        # already-collected real Azure row back to a flat estimate.
        mock_tz.now.return_value = datetime(2026, 7, 15, 6, 30)
        self._seed_azure_snapshot(date(2026, 7, 1))
        mock_query.side_effect = RuntimeError("(429) Too many requests")

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_query_failed")
        mock_alert.assert_called_once()
        snap = self._snapshot_for(date(2026, 7, 1))
        self.assertEqual(snap.source, "azure")
        self.assertEqual(snap.container_cost, Decimal("3.50"))

    # --- fully-loaded platform share flows into the snapshot + total ---

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_azure_row_includes_platform_share_and_total(self, mock_query, mock_alert):
        # Residual (non oc-*/ws-*) rg cost is amortized across active tenants and
        # folded into total_cost. One active tenant → the whole residual lands on it.
        mock_query.return_value = {
            "oc-abc": Decimal("3.50"),
            "ws-abc": Decimal("0.10"),
            "nbhd-django-westus2": Decimal("40.00"),  # always-on control plane
        }

        result = refresh_infra_costs()

        self.assertEqual(result["source"], "azure")
        snap = self._snapshot()
        # platform = (43.60 total − 3.60 attributed) / 1 active tenant = 40.00
        self.assertEqual(snap.platform_share, Decimal("40.0000"))
        # db share capped at 0.50 for a single-tenant fleet.
        self.assertEqual(snap.database_share, Decimal("0.5000"))
        self.assertEqual(
            snap.total_cost,
            Decimal("3.50") + Decimal("0.10") + Decimal("0.5000") + Decimal("40.0000"),
        )
        mock_alert.assert_not_called()

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_estimate_fallback_writes_flat_platform_share(self, mock_query, mock_alert):
        # A hard query failure falls back to estimates — the platform line uses
        # the flat INFRA_PLATFORM_SHARE_ESTIMATE (default $2.00), folded into total.
        mock_query.side_effect = RuntimeError("boom")

        refresh_infra_costs()

        snap = self._snapshot()
        self.assertEqual(snap.source, "estimate")
        self.assertEqual(snap.platform_share, Decimal("2.0000"))
        # 4.00 container + 0.25 storage + 0.50 db + 2.00 platform.
        self.assertEqual(snap.total_cost, Decimal("6.75"))

    @override_settings()
    @patch(f"{MODULE}._alert_cost_degradation")
    def test_mock_mode_uses_estimates_without_alert(self, mock_alert):
        with patch.dict("os.environ", {"AZURE_MOCK": "true"}):
            result = refresh_infra_costs()

        self.assertFalse(result["degraded"])
        self.assertEqual(result["source"], "estimate")
        self.assertEqual(self._snapshot().source, "estimate")
        mock_alert.assert_not_called()


class AlertCostDegradationTest(TestCase):
    """The alert helper itself: logs a WARNING and emits a tagged Sentry event."""

    @patch(f"{MODULE}.sentry_sdk")
    def test_message_path_tags_and_captures(self, mock_sentry):
        scope = MagicMock()
        mock_sentry.new_scope.return_value.__enter__.return_value = scope

        with self.assertLogs(MODULE, level="WARNING"):
            _alert_cost_degradation("azure_returned_empty", tenants=3)

        scope.set_tag.assert_called_once_with("infra_cost_degraded", "azure_returned_empty")
        # Grouping is set intentionally by reason, not derived from the message.
        self.assertEqual(scope.fingerprint, ["infra_cost_degraded", "azure_returned_empty"])
        mock_sentry.capture_message.assert_called_once()
        mock_sentry.capture_exception.assert_not_called()

    @patch(f"{MODULE}.sentry_sdk")
    def test_exception_path_captures_exception(self, mock_sentry):
        scope = MagicMock()
        mock_sentry.new_scope.return_value.__enter__.return_value = scope
        boom = RuntimeError("boom")

        with self.assertLogs(MODULE, level="WARNING"):
            _alert_cost_degradation("azure_query_failed", exc=boom)

        scope.set_tag.assert_called_once_with("infra_cost_degraded", "azure_query_failed")
        mock_sentry.capture_exception.assert_called_once_with(boom)
        mock_sentry.capture_message.assert_not_called()

    def test_helper_is_safe_when_sentry_uninitialised(self):
        # Real (uninitialised in tests) sentry_sdk → no exception, just a log.
        with self.assertLogs(MODULE, level="WARNING"):
            _alert_cost_degradation("azure_no_tenant_match", tenants=1)


class CalculateDatabaseShareTest(TestCase):
    """The per-tenant DB share must be capped so a small fleet isn't overcharged
    into a structural $0 surplus (no donation ever shows)."""

    def test_small_fleet_capped(self):
        # $25 / 3 = $8.33 would exceed the $12 price on its own; cap holds it.
        self.assertEqual(calculate_database_share(3), Decimal("0.5000"))

    def test_single_tenant_capped(self):
        self.assertEqual(calculate_database_share(1), Decimal("0.5000"))

    def test_zero_tenants_capped(self):
        # Degenerate N<=0 must not return the whole $25 bill.
        self.assertEqual(calculate_database_share(0), Decimal("0.5000"))

    def test_large_fleet_below_cap_uses_even_split(self):
        # $25 / 100 = $0.25 < cap → the real split applies (cap doesn't bind).
        self.assertEqual(calculate_database_share(100), Decimal("0.2500"))

    @override_settings(INFRA_DB_SHARE_CAP=1.00)
    def test_cap_is_configurable(self):
        # $25 / 10 = $2.50 capped to the configured $1.00.
        self.assertEqual(calculate_database_share(10), Decimal("1.0000"))


class CalculatePlatformShareTest(TestCase):
    """Fully-loaded attribution: the rg-nbhd-prod residual not attributed to any
    per-tenant oc-*/ws-* resource is split evenly across active tenants."""

    def test_even_split_of_unattributed_cost(self):
        resource_costs = {
            "oc-abc": Decimal("4.00"),
            "ws-abc": Decimal("0.25"),
            "nbhd-django-westus2": Decimal("40.00"),
            "nbhdunited": Decimal("5.00"),  # container registry
            "kv-nbhd-prod": Decimal("1.00"),  # key vault
        }
        # residual = 50.25 total − 4.25 attributed = 46.00; / 2 tenants = 23.00
        self.assertEqual(calculate_platform_share(resource_costs, 2), Decimal("23.0000"))

    def test_zero_active_tenants_guarded(self):
        # Degenerate N<=0 must not load the whole platform bill onto a phantom tenant.
        resource_costs = {"nbhd-django-westus2": Decimal("40.00")}
        self.assertEqual(calculate_platform_share(resource_costs, 0), Decimal("0.0000"))

    def test_all_cost_attributed_yields_zero(self):
        # Every resource maps to a tenant → no shared residual to spread.
        resource_costs = {"oc-abc": Decimal("4.00"), "ws-abc": Decimal("0.25")}
        self.assertEqual(calculate_platform_share(resource_costs, 3), Decimal("0.0000"))

    def test_empty_costs_yields_zero(self):
        self.assertEqual(calculate_platform_share({}, 3), Decimal("0.0000"))
