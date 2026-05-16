"""Unit tests for apps.common.llm_contracts.

Date resolution is exercised against a fake tenant carrying a
``user.timezone`` attribute, since the function only reads that one
field. No DB needed.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from pydantic import BaseModel, ValidationError

from apps.common.llm_contracts import (
    LLMValidationError,
    resolve_relative_date,
    today_in_tenant_tz,
)


def _fake_tenant(tz: str | None = "America/New_York"):
    """Return an object shaped like a Tenant with .user.timezone."""
    return SimpleNamespace(user=SimpleNamespace(timezone=tz))


class ResolveRelativeDateTests(SimpleTestCase):
    def setUp(self):
        # Freeze "now" at a moment when UTC and US/Eastern disagree on date.
        # 2026-05-17 03:00 UTC = 2026-05-16 23:00 EDT.
        # A naive UTC date.today() returns 2026-05-17, but the user is
        # still living in May 16.
        self.frozen_now = datetime(2026, 5, 17, 3, 0, 0, tzinfo=UTC)
        self.patcher = patch("apps.common.llm_contracts.dj_tz.now", return_value=self.frozen_now)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_today_in_eastern_lands_yesterday_vs_utc(self):
        """The reason this module exists — bug #3 from the 2026-05-16 session."""
        eastern = _fake_tenant("America/New_York")
        self.assertEqual(resolve_relative_date(eastern, "today"), date(2026, 5, 16))

    def test_today_in_utc_returns_utc_date(self):
        utc = _fake_tenant("UTC")
        self.assertEqual(resolve_relative_date(utc, "today"), date(2026, 5, 17))

    def test_empty_phrase_is_today(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), ""), date(2026, 5, 16))

    def test_none_phrase_is_today(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), None), date(2026, 5, 16))

    def test_yesterday(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), "yesterday"), date(2026, 5, 15))

    def test_tomorrow(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), "tomorrow"), date(2026, 5, 17))

    def test_iso_date_passes_through(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), "2026-01-01"), date(2026, 1, 1))

    def test_weekday_resolves_to_most_recent_past(self):
        # 2026-05-16 is a Saturday. "Monday" = previous Monday = May 11.
        self.assertEqual(resolve_relative_date(_fake_tenant(), "Monday"), date(2026, 5, 11))

    def test_weekday_today_means_last_week(self):
        # 2026-05-16 is Saturday. "Saturday" → previous Saturday, May 9.
        self.assertEqual(resolve_relative_date(_fake_tenant(), "Saturday"), date(2026, 5, 9))

    def test_weekday_abbreviation(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), "Fri"), date(2026, 5, 15))

    def test_n_days_ago(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), "3 days ago"), date(2026, 5, 13))

    def test_in_n_days(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), "in 5 days"), date(2026, 5, 21))

    def test_n_days_no_in(self):
        self.assertEqual(resolve_relative_date(_fake_tenant(), "5 days"), date(2026, 5, 21))

    def test_unknown_phrase_returns_none(self):
        self.assertIsNone(resolve_relative_date(_fake_tenant(), "the day before the third moon"))

    def test_invalid_timezone_falls_back_to_utc(self):
        """A bad tz string should not raise — UTC is the documented default."""
        bad = _fake_tenant("Mars/Olympus_Mons")
        self.assertEqual(resolve_relative_date(bad, "today"), date(2026, 5, 17))

    def test_missing_timezone_falls_back_to_utc(self):
        no_tz = _fake_tenant(None)
        self.assertEqual(resolve_relative_date(no_tz, "today"), date(2026, 5, 17))


class TodayInTenantTzTests(SimpleTestCase):
    def test_matches_resolve_relative_date_today(self):
        """Shortcut helper must agree with the long form."""
        # Not frozen — both reads happen back-to-back so they see the same `now`.
        tenant = _fake_tenant("UTC")
        self.assertEqual(today_in_tenant_tz(tenant), resolve_relative_date(tenant, "today"))


class LLMValidationErrorTests(SimpleTestCase):
    def test_from_pydantic_extracts_field_paths(self):
        class M(BaseModel):
            a: int
            b: str

        try:
            M(a="not an int", b=42)
        except ValidationError as exc:
            err = LLMValidationError.from_pydantic(exc)
        else:  # pragma: no cover - sanity check, the model must reject
            self.fail("expected Pydantic to raise ValidationError")

        # Two issues; surface both, with paths intact.
        self.assertEqual(len(err.details), 2)
        loc_paths = {tuple(d["loc"]) for d in err.details}
        self.assertEqual(loc_paths, {("a",), ("b",)})
        # Pydantic v2 error types are stable strings the LLM can branch on.
        types_seen = {d["type"] for d in err.details}
        self.assertTrue(types_seen.issubset({"int_parsing", "string_type"}))
        # Plural form in the message when multiple issues.
        self.assertIn("2 issues", err.message)

    def test_from_pydantic_singular_message(self):
        class M(BaseModel):
            a: int

        try:
            M(a="x")
        except ValidationError as exc:
            err = LLMValidationError.from_pydantic(exc)
        else:  # pragma: no cover
            self.fail("expected Pydantic to raise ValidationError")

        self.assertIn("1 issue", err.message)
        self.assertNotIn("issues", err.message)

    def test_as_tool_result_returns_dict_shape(self):
        err = LLMValidationError(message="oops", details=[{"loc": ["a"], "msg": "bad", "type": "x"}])
        out = err.as_tool_result()
        self.assertEqual(out["error"], "validation_failed")
        self.assertEqual(out["message"], "oops")
        self.assertEqual(out["details"][0]["loc"], ["a"])
