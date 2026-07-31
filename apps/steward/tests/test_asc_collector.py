from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.steward.collectors import asc
from apps.steward.models import (
    AscVersionSnapshot,
    CollectorStatus,
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
    version_id: str = "version-1",
    version_string: str = "2.1.6",
    build_number: str = "42",
    processing_state: str = "VALID",
    phased_state: str | None = None,
    day_number: int | None = None,
    state_field: str = "appStoreState",
    fallback_state: str | None = None,
    next_link: str | None = None,
):
    relationships = {"build": {"data": {"type": "builds", "id": f"build-{version_id}"}}}
    included = [
        {
            "type": "builds",
            "id": f"build-{version_id}",
            "attributes": {
                "version": build_number,
                "processingState": processing_state,
            },
        }
    ]
    if phased_state is not None:
        relationships["appStoreVersionPhasedRelease"] = {
            "data": {
                "type": "appStoreVersionPhasedReleases",
                "id": f"phased-{version_id}",
            }
        }
        included.append(
            {
                "type": "appStoreVersionPhasedReleases",
                "id": f"phased-{version_id}",
                "attributes": {
                    "phasedReleaseState": phased_state,
                    "currentDayNumber": day_number,
                },
            }
        )
    attributes = {
        state_field: state,
        "versionString": version_string,
    }
    if fallback_state is not None:
        attributes["appStoreState"] = fallback_state
    payload = {
        "data": [
            {
                "type": "appStoreVersions",
                "id": version_id,
                "attributes": attributes,
                "relationships": relationships,
            }
        ],
        "included": included,
    }
    if next_link is not None:
        payload["links"] = {"next": next_link}
    return payload


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

    def _client(self, payload, *, linked_pages=None):
        linked_pages = linked_pages or {}
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
            if path in linked_pages:
                return FakeResponse(linked_pages[path])
            raise AssertionError(f"unexpected ASC path {path}")

        client.get.side_effect = get
        context = MagicMock()
        context.__enter__.return_value = client
        context.__exit__.return_value = False
        return context, client

    def _snapshot(
        self,
        *,
        version_id="version-1",
        version_string="2.1.6",
        state="PREPARE_FOR_SUBMISSION",
        build_number="41",
        processing_state="PROCESSING",
    ):
        return AscVersionSnapshot.objects.create(
            version_id=version_id,
            version_string=version_string,
            app_state=state,
            build_number=build_number,
            build_processing_state=processing_state,
            phased_state="",
            phased_day=None,
        )

    @override_settings(
        STEWARD_ASC_KEY_ID="",
        STEWARD_ASC_ISSUER_ID="",
        STEWARD_ASC_PRIVATE_KEY="",
    )
    @patch("apps.steward.collectors.asc.httpx.Client")
    def test_disabled_when_any_credential_is_unset_records_health(self, client_class):
        self.assertEqual(asc.collect_asc()["versions"], 0)
        client_class.assert_not_called()
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.ASC)
        self.assertEqual(status.last_error_class, "not_configured")
        self.assertIsNone(status.last_success_at)

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_any_typed_tuple_change_emits_evidence_and_upserts_snapshot(self, _encode):
        first_context, _ = self._client(
            versions_payload(
                "WAITING_FOR_REVIEW",
                build_number="41",
                processing_state="PROCESSING",
            )
        )
        with patch("apps.steward.collectors.asc.httpx.Client", return_value=first_context):
            first = asc.collect_asc()

        second_context, _ = self._client(
            versions_payload(
                "WAITING_FOR_REVIEW",
                build_number="42",
                processing_state="VALID",
            )
        )
        with patch("apps.steward.collectors.asc.httpx.Client", return_value=second_context):
            second = asc.collect_asc()

        replay_context, _ = self._client(
            versions_payload(
                "WAITING_FOR_REVIEW",
                build_number="42",
                processing_state="VALID",
            )
        )
        with patch("apps.steward.collectors.asc.httpx.Client", return_value=replay_context):
            replay = asc.collect_asc()

        self.assertEqual((first["evidence"], second["evidence"], replay["evidence"]), (1, 1, 0))
        snapshot = AscVersionSnapshot.objects.get(version_id="version-1")
        self.assertEqual(snapshot.build_number, "42")
        self.assertEqual(snapshot.build_processing_state, "VALID")
        events = list(EvidenceEvent.objects.filter(source=EvidenceSource.ASC_VERSION_STATE).order_by("id"))
        self.assertEqual(len(events), 2)
        self.assertEqual([event.payload["buildNumber"] for event in events], ["41", "42"])
        self.assertTrue(all("version-1" not in event.fingerprint for event in events))
        self.assertTrue(all(len(event.fingerprint.rsplit(":", 1)[1]) == 24 for event in events))

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_first_poll_emits_and_advances_matching_planned_train(self, _encode):
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        )
        context, _ = self._client(versions_payload("WAITING_FOR_REVIEW"))

        with patch("apps.steward.collectors.asc.httpx.Client", return_value=context):
            result = asc.collect_asc()

        train.refresh_from_db()
        self.assertEqual(result["evidence"], 1)
        self.assertEqual(result["train_advances"], 1)
        self.assertEqual(AscVersionSnapshot.objects.count(), 1)
        self.assertEqual(EvidenceEvent.objects.filter(source=EvidenceSource.ASC_VERSION_STATE).count(), 1)
        self.assertEqual(train.phase, ReleaseTrain.Phase.SUBMITTED)

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_state_a_to_b_to_a_increments_revision_and_reemits(self, _encode):
        payloads = [
            versions_payload("WAITING_FOR_REVIEW"),
            versions_payload("IN_REVIEW"),
            versions_payload("WAITING_FOR_REVIEW"),
        ]
        results = []
        for payload in payloads:
            context, _ = self._client(payload)
            with patch(
                "apps.steward.collectors.asc.httpx.Client",
                return_value=context,
            ):
                results.append(asc.collect_asc())

        self.assertEqual([result["evidence"] for result in results], [1, 1, 1])
        events = list(EvidenceEvent.objects.filter(source=EvidenceSource.ASC_VERSION_STATE).order_by("id"))
        self.assertEqual(
            [event.payload["state"] for event in events],
            ["WAITING_FOR_REVIEW", "IN_REVIEW", "WAITING_FOR_REVIEW"],
        )
        self.assertEqual(len({event.fingerprint for event in events}), 3)
        snapshot = AscVersionSnapshot.objects.get(version_id="version-1")
        self.assertEqual(snapshot.revision, 2)

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_state_transitions_advance_exact_matching_fresh_train_only(self, _encode):
        self._snapshot()
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        )
        other = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.7",
        )
        waiting_context, _ = self._client(versions_payload("WAITING_FOR_REVIEW"))
        with patch("apps.steward.collectors.asc.httpx.Client", return_value=waiting_context):
            asc.collect_asc()
        train.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.SUBMITTED)
        self.assertEqual(other.phase, ReleaseTrain.Phase.PLANNED)

        in_review_context, _ = self._client(versions_payload("IN_REVIEW"))
        with patch("apps.steward.collectors.asc.httpx.Client", return_value=in_review_context):
            asc.collect_asc()
        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.IN_REVIEW)

        stale_time = timezone.now() - timedelta(hours=1)
        stale = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.8",
        )
        stale_context, _ = self._client(
            versions_payload(
                "READY_FOR_SALE",
                version_id="version-stale",
                version_string="2.1.8",
            )
        )
        with (
            patch("apps.steward.collectors.asc.timezone.now", return_value=stale_time),
            patch("apps.steward.collectors.asc.httpx.Client", return_value=stale_context),
        ):
            asc.collect_asc()
        stale.refresh_from_db()
        self.assertEqual(stale.phase, ReleaseTrain.Phase.PLANNED)

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_app_version_state_wins_and_ready_for_distribution_releases(self, _encode):
        self._snapshot(version_string="2.1.9")
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.9",
        )
        context, _ = self._client(
            versions_payload(
                "READY_FOR_DISTRIBUTION",
                version_string="2.1.9",
                state_field="appVersionState",
                fallback_state="WAITING_FOR_REVIEW",
            )
        )
        with patch("apps.steward.collectors.asc.httpx.Client", return_value=context):
            result = asc.collect_asc()

        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.RELEASED)
        self.assertEqual(result["train_advances"], 1)
        self.assertEqual(AscVersionSnapshot.objects.get().app_state, "READY_FOR_DISTRIBUTION")

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_no_rollout_companion_event_or_subject_satisfaction(self, _encode):
        expectation = Expectation.objects.create(
            kind=Expectation.Kind.DEADLINE,
            due_at=timezone.now(),
            grace_s=6 * 60 * 60,
            evidence_source=EvidenceSource.ASC_VERSION_STATE,
            subject="nbhd-ios-2.1.5-rollout",
            on_miss=Expectation.OnMiss.DIGEST,
        )
        context, _ = self._client(
            versions_payload(
                "READY_FOR_SALE",
                version_string="2.1.5",
                phased_state="COMPLETE",
                day_number=7,
            )
        )
        with patch("apps.steward.collectors.asc.httpx.Client", return_value=context):
            asc.collect_asc()

        expectation.refresh_from_db()
        self.assertEqual(expectation.state, Expectation.State.ARMED)
        self.assertFalse(EvidenceEvent.objects.filter(subject="nbhd-ios-2.1.5-rollout").exists())

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_pagination_follows_three_pages_filters_ios_and_records_truncation(self, _encode):
        first = versions_payload(
            "WAITING_FOR_REVIEW",
            version_id="v1",
            next_link="https://asc.example/page-2",
        )
        second = versions_payload(
            "IN_REVIEW",
            version_id="v2",
            version_string="2.1.7",
            next_link="https://asc.example/page-3",
        )
        third = versions_payload(
            "READY_FOR_SALE",
            version_id="v3",
            version_string="2.1.8",
            next_link="https://asc.example/page-4",
        )
        context, client = self._client(
            first,
            linked_pages={
                "https://asc.example/page-2": second,
                "https://asc.example/page-3": third,
            },
        )
        with patch("apps.steward.collectors.asc.httpx.Client", return_value=context):
            result = asc.collect_asc()

        self.assertEqual(result["versions"], 3)
        version_call = next(
            call for call in client.get.call_args_list if call.args[0] == "/v1/apps/app-1/appStoreVersions"
        )
        self.assertEqual(version_call.kwargs["params"]["filter[platform]"], "IOS")
        self.assertFalse(any(call.args[0] == "https://asc.example/page-4" for call in client.get.call_args_list))
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.ASC)
        self.assertEqual(status.last_error_class, "truncated")
        self.assertIn("3 pages", status.detail)

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_complete_poll_tombstones_and_reappearance_continues_revision(self, _encode):
        self._snapshot(
            version_id="disappeared",
            version_string="2.0.0",
            state="WAITING_FOR_REVIEW",
            build_number="42",
            processing_state="VALID",
        )
        first = versions_payload(
            "WAITING_FOR_REVIEW",
            version_id="v1",
            next_link="https://asc.example/page-2",
        )
        second = versions_payload(
            "IN_REVIEW",
            version_id="v2",
            version_string="2.1.7",
            next_link="https://asc.example/page-3",
        )
        third = versions_payload(
            "READY_FOR_SALE",
            version_id="v3",
            version_string="2.1.8",
            next_link="https://asc.example/page-4",
        )
        truncated_context, _ = self._client(
            first,
            linked_pages={
                "https://asc.example/page-2": second,
                "https://asc.example/page-3": third,
            },
        )
        with patch(
            "apps.steward.collectors.asc.httpx.Client",
            return_value=truncated_context,
        ):
            asc.collect_asc()
        self.assertTrue(AscVersionSnapshot.objects.get(version_id="disappeared").active)

        complete_context, _ = self._client(versions_payload("WAITING_FOR_REVIEW", version_id="v1"))
        with patch(
            "apps.steward.collectors.asc.httpx.Client",
            return_value=complete_context,
        ):
            asc.collect_asc()
        tombstone = AscVersionSnapshot.objects.get(version_id="disappeared")
        self.assertFalse(tombstone.active)
        self.assertEqual(
            set(
                AscVersionSnapshot.objects.filter(active=True).values_list(
                    "version_id",
                    flat=True,
                )
            ),
            {"v1"},
        )

        reappeared_context, _ = self._client(
            versions_payload(
                "WAITING_FOR_REVIEW",
                version_id="disappeared",
                version_string="2.0.0",
            )
        )
        with patch(
            "apps.steward.collectors.asc.httpx.Client",
            return_value=reappeared_context,
        ):
            result = asc.collect_asc()

        reappeared = AscVersionSnapshot.objects.get(version_id="disappeared")
        self.assertTrue(reappeared.active)
        self.assertEqual(reappeared.revision, 1)
        self.assertEqual(result["evidence"], 1)
        self.assertEqual(
            EvidenceEvent.objects.filter(
                source=EvidenceSource.ASC_VERSION_STATE,
                subject="nbhd-ios-2.0.0",
            ).count(),
            1,
        )

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_fingerprint_collision_leaves_snapshot_advance_retryable(self, _encode):
        snapshot = self._snapshot()
        raw_fingerprint = f"asc-state:{asc._fingerprint_hash(snapshot.version_id, 1)}"
        EvidenceEvent.objects.create(
            source=EvidenceSource.ASC_VERSION_STATE,
            subject="nbhd-ios-collision",
            occurred_at=timezone.now() - timedelta(minutes=1),
            payload={"state": "COLLISION"},
            fingerprint=stored_evidence_fingerprint(
                EvidenceSource.ASC_VERSION_STATE,
                raw_fingerprint,
            ),
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        context, _ = self._client(versions_payload("WAITING_FOR_REVIEW"))

        with (
            patch("apps.steward.collectors.asc.httpx.Client", return_value=context),
            patch.object(asc.logger, "error") as log_error,
        ):
            result = asc.collect_asc()

        snapshot.refresh_from_db()
        self.assertEqual(result["evidence"], 0)
        self.assertEqual(snapshot.app_state, "PREPARE_FOR_SUBMISSION")
        self.assertEqual(snapshot.revision, 0)
        self.assertTrue(snapshot.active)
        self.assertIn("snapshot advance skipped", log_error.call_args.args[0])

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_persistence_rolls_back_evidence_snapshot_and_pruning_together(self, _encode):
        self._snapshot(version_id="existing", version_string="2.0.0")
        context, _ = self._client(versions_payload("WAITING_FOR_REVIEW", version_id="new"))
        with (
            patch("apps.steward.collectors.asc.httpx.Client", return_value=context),
            patch(
                "apps.steward.collectors.asc._recover_train_advances",
                side_effect=RuntimeError("crash after evidence"),
            ),
            self.assertRaises(RuntimeError),
        ):
            asc.collect_asc()

        self.assertFalse(EvidenceEvent.objects.exists())
        self.assertEqual(
            set(AscVersionSnapshot.objects.values_list("version_id", flat=True)),
            {"existing"},
        )

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_recovery_sweep_advances_from_previously_persisted_evidence(self, _encode):
        self._snapshot(state="WAITING_FOR_REVIEW", build_number="42", processing_state="VALID")
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        )
        occurred_at = timezone.now()
        EvidenceEvent.objects.create(
            source=EvidenceSource.ASC_VERSION_STATE,
            subject="nbhd-ios-2.1.6",
            occurred_at=occurred_at,
            payload={
                "versionString": "2.1.6",
                "state": "WAITING_FOR_REVIEW",
                "buildNumber": "42",
                "buildProcessingState": "VALID",
                "phasedState": "",
                "dayNumber": None,
            },
            fingerprint="asc-recovery-probe",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        context, _ = self._client(versions_payload("WAITING_FOR_REVIEW"))

        with patch("apps.steward.collectors.asc.httpx.Client", return_value=context):
            result = asc.collect_asc()

        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.SUBMITTED)
        self.assertEqual(result["evidence"], 0)
        self.assertEqual(result["train_advances"], 1)

    def test_recovery_sweep_caps_trains_and_uses_two_set_based_queries(self):
        for index in range(21):
            open_train(
                product=TrackedItem.Product.NBHD_IOS,
                version_string=f"bounded-{index}",
            )

        with self.assertNumQueries(2):
            advances = asc._recover_train_advances()

        self.assertEqual(advances, 0)

    @override_settings(**ASC_SETTINGS)
    @patch("jwt.encode", return_value="signed-test-jwt")
    def test_collection_failure_updates_health_before_reraising(self, _encode):
        context, _ = self._client({"data": "invalid"})
        with (
            patch("apps.steward.collectors.asc.httpx.Client", return_value=context),
            self.assertRaises(ValueError),
        ):
            asc.collect_asc()

        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.ASC)
        self.assertEqual(status.last_error_class, "ValueError")
        self.assertEqual(status.consecutive_failures, 1)
        self.assertLessEqual(len(status.detail), 200)

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
