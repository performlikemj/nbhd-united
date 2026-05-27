"""Tests for ``apps.common.tenant_tz`` — the canonical tenant-tz resolver."""

from __future__ import annotations

import zoneinfo
from types import SimpleNamespace
from unittest import TestCase

from apps.common.tenant_tz import safe_zoneinfo, tenant_tz, tenant_tz_name


def _fake_tenant(tz: str | None) -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(timezone=tz))


class TenantTzNameTests(TestCase):
    def test_iana_name_passes_through(self):
        self.assertEqual(tenant_tz_name(_fake_tenant("America/New_York")), "America/New_York")
        self.assertEqual(tenant_tz_name(_fake_tenant("Asia/Tokyo")), "Asia/Tokyo")

    def test_missing_user_returns_utc(self):
        self.assertEqual(tenant_tz_name(SimpleNamespace()), "UTC")

    def test_user_with_no_timezone_returns_utc(self):
        self.assertEqual(tenant_tz_name(SimpleNamespace(user=SimpleNamespace())), "UTC")

    def test_empty_string_timezone_returns_utc(self):
        self.assertEqual(tenant_tz_name(_fake_tenant("")), "UTC")

    def test_none_timezone_returns_utc(self):
        self.assertEqual(tenant_tz_name(_fake_tenant(None)), "UTC")

    def test_unknown_iana_zone_returns_utc(self):
        self.assertEqual(tenant_tz_name(_fake_tenant("Mars/Olympus_Mons")), "UTC")


class TenantTzTests(TestCase):
    def test_returns_zoneinfo_instance(self):
        zi = tenant_tz(_fake_tenant("America/Los_Angeles"))
        self.assertIsInstance(zi, zoneinfo.ZoneInfo)
        self.assertEqual(str(zi), "America/Los_Angeles")

    def test_falls_back_to_utc(self):
        zi = tenant_tz(_fake_tenant(None))
        self.assertEqual(str(zi), "UTC")


class SafeZoneinfoTests(TestCase):
    def test_known_zone(self):
        self.assertEqual(str(safe_zoneinfo("Europe/Berlin")), "Europe/Berlin")

    def test_unknown_falls_back(self):
        self.assertEqual(str(safe_zoneinfo("Bogus/Zone")), "UTC")

    def test_empty_falls_back(self):
        self.assertEqual(str(safe_zoneinfo("")), "UTC")

    def test_none_falls_back(self):
        self.assertEqual(str(safe_zoneinfo(None)), "UTC")
