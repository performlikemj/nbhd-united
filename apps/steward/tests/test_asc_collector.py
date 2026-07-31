from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.steward.collectors import asc
from apps.steward.models import (
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    ReleaseTrain,
    TrackedItem,
)
from apps.steward.services import stored_evidence_fingerprint
from apps.steward.trains import open_train

ASC_SETTINGS = {
    "STEWARD_ASC_KEY_ID": "FAKEKEY1",
    "STEWARD_ASC_ISSUER_ID": "00000000-0000-0000-0000-000000000000",
    "STEWARD_ASC_PRIVATE_KEY": ("-----BEGIN PRIVATE KEY-----\nTEST-ONLY-NOT-A-REAL-KEY\n-----END PRIVATE KEY-----"),
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def versions_payload(
    state: str,
    *,
    version_string: str = "2.1.6",
    phased_state: str | None = None,
    day_number: int | None = None,
):
    relationships = {"build": {"data": {"type": "builds", "id": "build-1"}}}
    included = [
        {
            "type": "builds",
            "id": "build-1",
            "attributes": {"version": "42", "processingState": "VALID"},
        }
    ]
    if phased_state is not None:
        relationships["appStoreVersionPhasedRelease"] = {
            "data": {
                "type": "appStoreVersionPhasedReleases",
                "id": "phased-1",
            }
        }
        included.append(
            {
                "type": "appStoreVersionPhasedReleases",
                "id": "phased-1",
                "attributes": {
                    "phasedReleaseState": phased_state,
                    "currentDayNumber": day_number,
                },
            }
        )
    return {
        "data": [
            {
                "type": "appStoreVersions",
                "id": "version-1",
                "attributes": {
                    "appStoreState": state,
                    "versionString": version_string,
                },
                "relationships": relationships,
            }
        ],
        "included": included,
    }


class ASCCollectorTests(TestCase):
    def setUp(self):
        asc._app_id_cache = None
        asc._jwt_cache.update(
            {
                "token": None,
                "expires_at": 0,
                "key_id": None,
                "issuer_id": None,
            }
        )

    def _client(self, payload):
        client = MagicMock()

        def get(path, params=None):
            if path == "/v1/apps":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "type": "apps",
                                "id": "app-1",
                                "attributes": {},
                            }
                        ]
                    }
                )
            if path == "/v1/builds":
                return FakeResponse({"data": []})
            if path == "/v1/apps/app-1/appStoreVersions":
                return FakeResponse(payload)
            raise AssertionError(f"unexpected ASC path {path}")

        client.get.side_effect = get
        context = MagicMock()
        context.__enter__.return_value = client
        context.__exit__.return_value = False
        return context

    @override_settings(
        STEWARD_ASC_KEY_ID="",
        STEWARD_ASC_ISSUER_ID="",
        STEWARD_ASC_PRIVATE_KEY="",
    )
    @patch("apps.steward.collectors.asc.httpx.Client")
    def test_disabled_when_any_credential_is_unset(self, client_class):
        self.assertEqual(asc.collect_asc()["versions"], 0)
        client_class.assert_not_called()

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_state_transition_evidence_and_review_advances(self, _encode):
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        )
        waiting_context = self._client(versions_payload("WAITING_FOR_REVIEW"))
        with patch(
            "apps.steward.collectors.asc.httpx.Client",
            return_value=waiting_context,
        ):
            asc.collect_asc()
        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.SUBMITTED)

        in_review_context = self._client(versions_payload("IN_REVIEW"))
        with patch(
            "apps.steward.collectors.asc.httpx.Client",
            return_value=in_review_context,
        ):
            asc.collect_asc()
        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.IN_REVIEW)
        self.assertTrue(
            EvidenceEvent.objects.filter(
                fingerprint=stored_evidence_fingerprint(
                    EvidenceSource.ASC_VERSION_STATE,
                    "asc:version-1:WAITING_FOR_REVIEW",
                )
            ).exists()
        )
        self.assertTrue(
            EvidenceEvent.objects.filter(
                fingerprint=stored_evidence_fingerprint(
                    EvidenceSource.ASC_VERSION_STATE,
                    "asc:version-1:IN_REVIEW",
                )
            ).exists()
        )

        replay_context = self._client(versions_payload("IN_REVIEW"))
        with patch(
            "apps.steward.collectors.asc.httpx.Client",
            return_value=replay_context,
        ):
            result = asc.collect_asc()
        self.assertEqual(result["evidence"], 0)

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_ready_for_sale_advances_matching_train_to_released(self, _encode):
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        )
        context = self._client(versions_payload("READY_FOR_SALE"))
        with patch(
            "apps.steward.collectors.asc.httpx.Client",
            return_value=context,
        ):
            result = asc.collect_asc()

        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.RELEASED)
        self.assertEqual(result["train_advances"], 1)

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_phased_complete_satisfies_legacy_rollout_deadline(self, _encode):
        expectation = Expectation.objects.create(
            kind=Expectation.Kind.DEADLINE,
            due_at=timezone.now(),
            grace_s=6 * 60 * 60,
            evidence_source=EvidenceSource.ASC_VERSION_STATE,
            subject="nbhd-ios-2.1.5-rollout",
            on_miss=Expectation.OnMiss.DIGEST,
        )
        context = self._client(
            versions_payload(
                "READY_FOR_SALE",
                version_string="2.1.5",
                phased_state="COMPLETE",
                day_number=7,
            )
        )
        with patch(
            "apps.steward.collectors.asc.httpx.Client",
            return_value=context,
        ):
            asc.collect_asc()

        expectation.refresh_from_db()
        self.assertEqual(expectation.state, Expectation.State.SATISFIED)
        self.assertTrue(
            EvidenceEvent.objects.filter(
                fingerprint=stored_evidence_fingerprint(
                    EvidenceSource.ASC_VERSION_STATE,
                    "asc-phased-rollout:version-1:COMPLETE:7",
                ),
                subject="nbhd-ios-2.1.5-rollout",
            ).exists()
        )

    @override_settings(**ASC_SETTINGS)
    def test_jwt_is_short_lived_cached_and_never_logged(self):
        private_key = ASC_SETTINGS["STEWARD_ASC_PRIVATE_KEY"]
        with (
            patch("jwt.encode", return_value="signed-test-jwt") as encode,
            patch.object(asc, "logger") as logger,
        ):
            token = asc._asc_jwt(
                key_id=ASC_SETTINGS["STEWARD_ASC_KEY_ID"],
                issuer_id=ASC_SETTINGS["STEWARD_ASC_ISSUER_ID"],
                private_key=private_key,
                now=1000,
            )
            cached = asc._asc_jwt(
                key_id=ASC_SETTINGS["STEWARD_ASC_KEY_ID"],
                issuer_id=ASC_SETTINGS["STEWARD_ASC_ISSUER_ID"],
                private_key=private_key,
                now=1001,
            )

        self.assertEqual(token, cached)
        encode.assert_called_once()
        claims = encode.call_args.args[0]
        self.assertEqual(claims["aud"], "appstoreconnect-v1")
        self.assertLessEqual(claims["exp"] - claims["iat"], 15 * 60)
        self.assertNotIn(private_key, str(logger.method_calls))
