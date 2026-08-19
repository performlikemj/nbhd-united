"""The profile timezone gate — upstream of every generated cron.

``user.timezone`` is copied into the ``tz`` of every cron the platform
generates, so a wrong value here is not one wrong answer: it silently re-times
the user's entire schedule. ``ZoneInfo`` alone does not catch the two ways a
model gets this wrong, because it resolves both of them:

  - ``Etc/GMT+9`` — POSIX sign inversion. It is UTC MINUS 9, eighteen hours
    from the Tokyo the model was reaching for.
  - ``EST`` / ``Japan`` — legacy aliases that carry no DST rules.
"""

from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

from apps.platform_logs.models import ToolContractEvent
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeProfileTimezoneTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="ProfileTz", telegram_chat_id=838383)
        seed_internal_key(self.tenant)

    def _patch(self, timezone_name):
        return self.client.patch(
            f"/api/v1/integrations/runtime/{self.tenant.id}/profile/",
            data={"timezone": timezone_name},
            content_type="application/json",
            HTTP_X_NBHD_INTERNAL_KEY="shared-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    def test_area_location_names_are_accepted(self):
        for name in ("Asia/Tokyo", "America/New_York", "Europe/London", "UTC"):
            with self.subTest(timezone=name):
                resp = self._patch(name)
                self.assertEqual(resp.status_code, 200, resp.content)
                self.tenant.user.refresh_from_db()
                self.assertEqual(self.tenant.user.timezone, name)

    def test_etc_name_is_rejected_and_the_profile_is_untouched(self):
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])

        resp = self._patch("Etc/GMT+9")

        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body["error"], "invalid_timezone")
        self.assertIn("INVERTED", body["detail"])
        self.assertIn("Asia/Tokyo", body["detail"])
        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.timezone, "Asia/Tokyo")
        self.assertEqual(
            list(ToolContractEvent.objects.filter(namespace="cron").values_list("reason_code", flat=True)),
            ["tz_etc_rejected"],
        )

    def test_abbreviations_are_rejected_even_when_zoneinfo_resolves_them(self):
        for name in ("EST", "JST", "GMT"):
            with self.subTest(timezone=name):
                resp = self._patch(name)
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("Area/Location", resp.json()["detail"])

    def test_unknown_zone_is_rejected(self):
        resp = self._patch("Mars/Olympus")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Unknown timezone", resp.json()["detail"])
        self.assertEqual(
            list(ToolContractEvent.objects.filter(namespace="cron").values_list("reason_code", flat=True)),
            ["profile_tz_rejected"],
        )
