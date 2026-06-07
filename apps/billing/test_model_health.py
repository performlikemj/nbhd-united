"""Tests for the free-model offer health-check cron + transition side effects."""

from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from apps.billing.constants import NEMOTRON_FREE_MODEL
from apps.billing.model_health import model_health_check
from apps.billing.models import FreeModelOffer
from apps.tenants.models import Tenant, User


class _Resp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


def _models_resp(prompt="0", completion="0"):
    return _Resp(
        {
            "data": [
                {
                    "id": "nvidia/nemotron-3-ultra-550b-a55b:free",
                    "pricing": {"prompt": prompt, "completion": completion},
                }
            ]
        }
    )


_PING_OK = _Resp({"choices": [{"message": {"content": "ok"}}]})


def _make_tenant(slug, *, preferred_model="", status=Tenant.Status.ACTIVE):
    user = User.objects.create_user(username=slug, password="x" * 32)
    return Tenant.objects.create(user=user, status=status, preferred_model=preferred_model)


def _activate_offer():
    offer = FreeModelOffer.load()
    offer.is_active = True
    offer.save(update_fields=["is_active"])
    return offer


@override_settings(OPENROUTER_API_KEY="sk-test")
@patch("apps.cron.publish.publish_task")
@patch("apps.router.system_notify.send_system_notification", return_value=True)
class ModelHealthTransitionTest(TestCase):
    @patch("apps.common.openrouter.requests.post", return_value=_PING_OK)
    @patch("apps.billing.model_health.requests.get", return_value=_models_resp())
    def test_activates_when_free_and_reachable(self, _get, _post, mock_notify, mock_publish):
        tenant = _make_tenant("roll-1")
        result = model_health_check()

        self.assertEqual(result["transition"], "activated")
        self.assertTrue(FreeModelOffer.load().is_active)
        tenant.refresh_from_db()
        self.assertGreaterEqual(tenant.pending_config_version, 1)
        self.assertTrue(mock_publish.called)
        self.assertTrue(mock_notify.called)

    @patch("apps.common.openrouter.requests.post", return_value=_PING_OK)
    @patch("apps.billing.model_health.requests.get", return_value=_models_resp(prompt="0.01"))
    def test_deactivates_when_no_longer_free(self, _get, _post, mock_notify, mock_publish):
        _activate_offer()
        tenant = _make_tenant("roll-2")
        result = model_health_check()

        self.assertEqual(result["transition"], "deactivated")
        offer = FreeModelOffer.load()
        self.assertFalse(offer.is_active)
        self.assertEqual(offer.last_transition_reason, "no_longer_free")
        self.assertTrue(mock_notify.called)

    @patch("apps.common.openrouter.requests.post", side_effect=requests.exceptions.ConnectionError("down"))
    @patch("apps.billing.model_health.requests.get", return_value=_models_resp())
    def test_unreachable_holds_until_threshold(self, _get, _post, mock_notify, mock_publish):
        _activate_offer()
        _make_tenant("roll-3")

        # Threshold is 3 consecutive failed ticks; first two hold the offer.
        self.assertEqual(model_health_check()["transition"], "held (healthy or below failure threshold)")
        self.assertTrue(FreeModelOffer.load().is_active)
        self.assertEqual(model_health_check()["transition"], "held (healthy or below failure threshold)")
        self.assertTrue(FreeModelOffer.load().is_active)
        # Third failure crosses the threshold.
        self.assertEqual(model_health_check()["transition"], "deactivated")
        offer = FreeModelOffer.load()
        self.assertFalse(offer.is_active)
        self.assertEqual(offer.last_transition_reason, "unreachable")

    @patch("apps.common.openrouter.requests.post", return_value=_PING_OK)
    @patch("apps.billing.model_health.requests.get", return_value=_models_resp(prompt="0.01"))
    def test_explicit_pick_reverted_on_deactivation(self, _get, _post, mock_notify, mock_publish):
        _activate_offer()
        tenant = _make_tenant("explicit", preferred_model=NEMOTRON_FREE_MODEL)
        model_health_check()

        tenant.refresh_from_db()
        self.assertEqual(tenant.preferred_model, "")

    @patch("apps.common.openrouter.requests.post", return_value=_PING_OK)
    @patch("apps.billing.model_health.requests.get", return_value=_models_resp())
    def test_kill_switch_deactivates(self, _get, _post, mock_notify, mock_publish):
        offer = _activate_offer()
        offer.enabled = False
        offer.save(update_fields=["enabled"])
        _make_tenant("roll-4")

        result = model_health_check()
        self.assertEqual(result["transition"], "deactivated")
        self.assertFalse(FreeModelOffer.load().is_active)

    @patch("apps.common.openrouter.requests.post", return_value=_PING_OK)
    @patch("apps.billing.model_health.requests.get", return_value=_models_resp())
    def test_steady_state_active_no_spam(self, _get, _post, mock_notify, mock_publish):
        _activate_offer()
        _make_tenant("roll-5")
        result = model_health_check()

        self.assertEqual(result["transition"], "held (healthy or below failure threshold)")
        self.assertTrue(FreeModelOffer.load().is_active)
        self.assertFalse(mock_notify.called)
        self.assertFalse(mock_publish.called)
