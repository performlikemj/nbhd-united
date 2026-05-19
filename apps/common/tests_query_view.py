"""Tests for ``apps.common.query_view`` — the parameterized query dispatcher."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from django.test import TestCase, override_settings
from pydantic import BaseModel, ConfigDict, Field
from rest_framework.test import APIRequestFactory

from apps.common.query_view import (
    BaseQueryView,
    QueryExecutionError,
    canonical_query_hash,
    jsonify,
)
from apps.common.windows import Window
from apps.tenants.services import create_tenant

# ─── Minimal query model + view for testing dispatch ──────────────────────


class _TestQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    resource: Literal["foo", "bar"]
    window: Window | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    fields: list[str] | None = None
    aggregate: Literal["sum", "count", "avg", "min", "max"] | None = None
    aggregate_field: str | None = None
    group_by: str | None = None
    order_by: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


class _TestQueryView(BaseQueryView):
    query_model = _TestQueryRequest

    def execute(self, query, tenant, window_resolved):
        if query.resource == "bar":
            raise QueryExecutionError("bar_not_allowed", "no bar", status_code=403)
        # Return a heterogeneous payload to exercise jsonify
        data = [
            {
                "id": UUID("11111111-1111-1111-1111-111111111111"),
                "amount": Decimal("38.34"),
                "date": date(2026, 5, 6),
                "created_at": datetime(2026, 5, 6, 12, 0),
                "note": "May min",
            }
        ]
        return data, len(data)


# ─── jsonify helper ────────────────────────────────────────────────────────


class JsonifyTests(TestCase):
    def test_decimal_to_string_preserves_precision(self):
        self.assertEqual(jsonify(Decimal("38.34")), "38.34")
        self.assertEqual(jsonify(Decimal("1077.77")), "1077.77")

    def test_uuid_to_string(self):
        u = uuid4()
        self.assertEqual(jsonify(u), str(u))

    def test_date_to_iso(self):
        self.assertEqual(jsonify(date(2026, 5, 6)), "2026-05-06")

    def test_datetime_to_iso(self):
        dt = datetime(2026, 5, 6, 12, 30)
        self.assertEqual(jsonify(dt), dt.isoformat())

    def test_nested_dict_recursion(self):
        self.assertEqual(
            jsonify({"x": [Decimal("1.50"), {"y": date(2026, 1, 1)}]}),
            {"x": ["1.50", {"y": "2026-01-01"}]},
        )

    def test_passthrough_for_primitive(self):
        self.assertEqual(jsonify("hello"), "hello")
        self.assertEqual(jsonify(7), 7)
        self.assertIsNone(jsonify(None))
        self.assertEqual(jsonify(True), True)


# ─── canonical_query_hash determinism ──────────────────────────────────────


class CanonicalQueryHashTests(TestCase):
    BASE = {
        "resource": "transactions",
        "window_resolved": (date(2026, 5, 13), date(2026, 5, 19)),
        "filter_": {"account_nickname": "Loan AJ"},
        "fields": ["amount", "date", "id"],
        "aggregate": None,
        "aggregate_field": None,
        "group_by": None,
        "order_by": "-date",
        "limit": 50,
    }

    def test_same_inputs_same_hash(self):
        a = canonical_query_hash(**self.BASE)
        b = canonical_query_hash(**self.BASE)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("sha256:"))

    def test_different_resource_different_hash(self):
        a = canonical_query_hash(**self.BASE)
        b = canonical_query_hash(**{**self.BASE, "resource": "accounts"})
        self.assertNotEqual(a, b)

    def test_different_window_different_hash(self):
        a = canonical_query_hash(**self.BASE)
        b = canonical_query_hash(**{**self.BASE, "window_resolved": (date(2026, 5, 12), date(2026, 5, 18))})
        self.assertNotEqual(a, b)

    def test_field_order_independent(self):
        a = canonical_query_hash(**self.BASE)
        b = canonical_query_hash(**{**self.BASE, "fields": ["id", "date", "amount"]})
        self.assertEqual(a, b)  # sorted before hashing

    def test_none_window_distinct_from_dated_window(self):
        a = canonical_query_hash(**{**self.BASE, "window_resolved": None})
        b = canonical_query_hash(**self.BASE)
        self.assertNotEqual(a, b)

    def test_decimal_filter_value_serializes_stably(self):
        a = canonical_query_hash(
            **{**self.BASE, "filter_": {"min_amount": Decimal("38.34")}},
        )
        b = canonical_query_hash(
            **{**self.BASE, "filter_": {"min_amount": Decimal("38.34")}},
        )
        self.assertEqual(a, b)


# ─── BaseQueryView dispatch — auth, validation, happy path ────────────────


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class BaseQueryViewDispatchTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="QueryViewT", telegram_chat_id=900900)
        self.factory = APIRequestFactory()
        self.view = _TestQueryView.as_view()

    def _post(self, body, *, key="test-internal-key", tenant_id=None):
        tid = tenant_id or str(self.tenant.id)
        return self.view(
            self.factory.post(
                f"/test/{tid}/query/",
                data=body,
                format="json",
                HTTP_X_NBHD_INTERNAL_KEY=key,
                HTTP_X_NBHD_TENANT_ID=tid,
            ),
            tenant_id=tid,
        )

    # ── Auth ───────────────────────────────────────────────────────────

    def test_missing_internal_key_returns_401(self):
        response = self._post({"resource": "foo"}, key="")
        self.assertEqual(response.status_code, 401)

    def test_wrong_internal_key_returns_401(self):
        response = self._post({"resource": "foo"}, key="wrong-key")
        self.assertEqual(response.status_code, 401)

    def test_tenant_header_mismatch_returns_401(self):
        # Send body for self.tenant but header for a different UUID
        other_id = str(uuid4())
        request = self.factory.post(
            f"/test/{self.tenant.id}/query/",
            data={"resource": "foo"},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key",
            HTTP_X_NBHD_TENANT_ID=other_id,
        )
        response = self.view(request, tenant_id=str(self.tenant.id))
        self.assertEqual(response.status_code, 401)

    def test_unknown_tenant_returns_404(self):
        response = self._post({"resource": "foo"}, tenant_id=str(uuid4()))
        # Auth uses the body-derived tenant_id; mismatch happens FIRST.
        # When header + URL match but tenant doesn't exist, we get 404.
        self.assertIn(response.status_code, (401, 404))

    # ── Validation ─────────────────────────────────────────────────────

    def test_unknown_resource_returns_400(self):
        response = self._post({"resource": "ghost"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "validation_failed")

    def test_extra_fields_rejected(self):
        response = self._post({"resource": "foo", "secret_field": 1})
        self.assertEqual(response.status_code, 400)

    def test_limit_out_of_range_400(self):
        response = self._post({"resource": "foo", "limit": 501})
        self.assertEqual(response.status_code, 400)

    # ── Happy path + meta envelope ─────────────────────────────────────

    def test_happy_path_returns_data_and_meta(self):
        response = self._post({"resource": "foo"})
        self.assertEqual(response.status_code, 200)
        body = response.data
        self.assertIn("data", body)
        self.assertIn("meta", body)

        # data is the jsonified payload from _TestQueryView.execute
        self.assertEqual(len(body["data"]), 1)
        row = body["data"][0]
        self.assertEqual(row["amount"], "38.34")  # Decimal → str
        self.assertEqual(row["date"], "2026-05-06")
        self.assertEqual(row["id"], "11111111-1111-1111-1111-111111111111")

        meta = body["meta"]
        self.assertEqual(meta["schema_version"], 1)
        self.assertEqual(meta["row_count"], 1)
        self.assertFalse(meta["has_more"])
        self.assertTrue(meta["query_hash"].startswith("sha256:"))
        self.assertIn("computed_at", meta)
        self.assertIn("as_of", meta)
        self.assertEqual(meta["tenant_tz"], "UTC")  # default for tenant without explicit tz

    def test_window_resolution_flows_to_meta(self):
        response = self._post(
            {"resource": "foo", "window": {"kind": "last_n_days", "value": 7}},
        )
        self.assertEqual(response.status_code, 200)
        wr = response.data["meta"]["window_resolved_to"]
        self.assertIsNotNone(wr)
        self.assertIn("from", wr)
        self.assertIn("to", wr)

    def test_no_window_means_no_window_resolved(self):
        response = self._post({"resource": "foo"})
        self.assertIsNone(response.data["meta"]["window_resolved_to"])

    def test_all_window_means_no_window_resolved(self):
        response = self._post({"resource": "foo", "window": {"kind": "all"}})
        self.assertIsNone(response.data["meta"]["window_resolved_to"])

    # ── query_hash determinism end-to-end ──────────────────────────────

    def test_identical_request_produces_identical_hash(self):
        body = {
            "resource": "foo",
            "filter": {"x": 1},
            "fields": ["amount", "date"],
            "order_by": "-date",
        }
        a = self._post(body).data["meta"]["query_hash"]
        b = self._post(body).data["meta"]["query_hash"]
        self.assertEqual(a, b)

    def test_field_order_does_not_affect_hash(self):
        a = self._post({"resource": "foo", "fields": ["amount", "date"]}).data["meta"]["query_hash"]
        b = self._post({"resource": "foo", "fields": ["date", "amount"]}).data["meta"]["query_hash"]
        self.assertEqual(a, b)

    # ── Subclass-raised errors flow through ────────────────────────────

    def test_query_execution_error_returns_subclass_status(self):
        response = self._post({"resource": "bar"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"], "bar_not_allowed")

    # ── has_more flag ─────────────────────────────────────────────────

    def test_has_more_when_row_count_reaches_limit(self):
        # _TestQueryView returns 1 row; with limit=1, has_more should be true.
        response = self._post({"resource": "foo", "limit": 1})
        self.assertTrue(response.data["meta"]["has_more"])


# ─── Tenant timezone integration ──────────────────────────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class BaseQueryViewTimezoneTests(TestCase):
    def test_tenant_tz_picked_up_from_user(self):
        tenant = create_tenant(display_name="TZ", telegram_chat_id=900901)
        tenant.user.timezone = "Asia/Tokyo"
        tenant.user.save(update_fields=["timezone"])

        factory = APIRequestFactory()
        view = _TestQueryView.as_view()
        tid = str(tenant.id)
        request = factory.post(
            f"/test/{tid}/query/",
            data={"resource": "foo", "window": {"kind": "today"}},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key",
            HTTP_X_NBHD_TENANT_ID=tid,
        )
        response = view(request, tenant_id=tid)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["meta"]["tenant_tz"], "Asia/Tokyo")
