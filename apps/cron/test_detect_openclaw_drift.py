"""The daily OpenClaw drift cron endpoint: auth, the default-OFF gate, and
the gated-vs-real alert split. SimpleTestCase — the Azure sweep is patched.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse

from apps.cron.management.commands.register_system_crons import SYSTEM_CRONS, iter_system_crons
from apps.cron.views import detect_openclaw_drift
from apps.orchestrator.openclaw_drift import FieldDrift, FleetDriftReport, TenantDrift

_TID = "11111111-1111-1111-1111-111111111111"


def _report(*, alerting: bool) -> FleetDriftReport:
    fld = (
        FieldDrift("env:OPENCLAW_STATE_DIR", "/home/node/oc-state", "missing")
        if alerting
        else FieldDrift("image", "2026.9.5-x", "2026.9.4-y", alert=False, note="stale-gated")
    )
    return FleetDriftReport(checked=3, tenants=[TenantDrift(tenant_id=_TID, container_name="oc-x", fields=[fld])])


@override_settings(DEPLOY_SECRET="deploy-secret")
@patch("apps.cron.views.verify_qstash_signature", return_value=False)
class DetectOpenclawDriftViewTest(SimpleTestCase):
    def _post(self, secret: str | None = "deploy-secret"):
        request = RequestFactory().post(reverse("cron-detect-openclaw-drift"))
        if secret is not None:
            request.META["HTTP_X_DEPLOY_SECRET"] = secret
        return detect_openclaw_drift(request)

    def test_unauthorized_without_signature_or_secret(self, _sig):
        with patch("apps.orchestrator.openclaw_drift.check_fleet_drift") as sweep:
            self.assertEqual(self._post(secret=None).status_code, 401)
            self.assertEqual(self._post(secret="wrong").status_code, 401)
            sweep.assert_not_called()

    @override_settings(OPENCLAW_DRIFT_ALERTS_ENABLED=False)
    def test_default_off_is_a_noop_that_never_touches_azure(self, _sig):
        with (
            patch("apps.orchestrator.openclaw_drift.check_fleet_drift") as sweep,
            patch("apps.cron.views._send_alert_via_pushover") as send,
        ):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["skipped"], "disabled")
        sweep.assert_not_called()
        send.assert_not_called()

    @override_settings(OPENCLAW_DRIFT_ALERTS_ENABLED=True)
    def test_real_drift_sends_one_alert(self, _sig):
        with (
            patch("apps.orchestrator.openclaw_drift.active_tenant_queryset", return_value=[]),
            patch("apps.orchestrator.openclaw_drift.check_fleet_drift", return_value=_report(alerting=True)),
            patch("apps.cron.views._send_alert_via_pushover", return_value="delivered") as send,
        ):
            resp = self._post()
        body = json.loads(resp.content)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["alerting"], 1)
        self.assertEqual(body["alert_status"], "delivered")
        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertIn("oc-x (11111111)", message)
        self.assertIn("env:OPENCLAW_STATE_DIR", message)

    @override_settings(OPENCLAW_DRIFT_ALERTS_ENABLED=True)
    def test_gated_image_drift_never_alerts(self, _sig):
        with (
            patch("apps.orchestrator.openclaw_drift.active_tenant_queryset", return_value=[]),
            patch("apps.orchestrator.openclaw_drift.check_fleet_drift", return_value=_report(alerting=False)),
            patch("apps.cron.views._send_alert_via_pushover") as send,
        ):
            resp = self._post()
        body = json.loads(resp.content)
        self.assertEqual(body["alerting"], 0)
        self.assertEqual(body["gated_only"], 1)
        self.assertEqual(body["alert_status"], "skipped")
        send.assert_not_called()


class DetectOpenclawDriftRegistryTest(SimpleTestCase):
    def test_registered_daily_at_the_view_url(self):
        entries = {name: (cron, path) for name, cron, path, _retries in iter_system_crons()}
        self.assertIn("detect-openclaw-drift", entries)
        cron, path = entries["detect-openclaw-drift"]
        # Registry paths use the /api/cron/ mount (urls.py is included at both
        # /api/cron/ and /api/v1/cron/); the path must resolve to our view.
        self.assertEqual(path, "/api/cron/detect-openclaw-drift/")
        self.assertIs(resolve(path).func, detect_openclaw_drift)
        self.assertEqual(cron, "40 8 * * *")
        self.assertEqual(len({e[0] for e in SYSTEM_CRONS}), len(SYSTEM_CRONS), "duplicate cron names")
