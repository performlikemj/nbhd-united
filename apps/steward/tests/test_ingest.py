import hashlib
import hmac
import json
import time

from django.test import TestCase, override_settings

from apps.steward.models import EvidenceEvent, EvidenceSource


@override_settings(STEWARD_INGEST_SECRET="obvious-test-steward-secret")
class StewardIngestTests(TestCase):
    def _post(self, path, body, *, timestamp=None, signature=None):
        raw = json.dumps(body, separators=(",", ":")).encode()
        timestamp = str(timestamp if timestamp is not None else int(time.time()))
        if signature is None:
            signature = hmac.new(
                b"obvious-test-steward-secret",
                timestamp.encode() + b"." + raw,
                hashlib.sha256,
            ).hexdigest()
        return self.client.post(
            path,
            data=raw,
            content_type="application/json",
            headers={
                "X-Steward-Timestamp": timestamp,
                "X-Steward-Signature": signature,
            },
        )

    def test_valid_generic_hmac_is_accepted_and_provenance_is_forced(self):
        response = self._post(
            "/api/steward/evidence/",
            {
                "source": "ci_run",
                "subject": "nbhd-united-main-ci",
                "fingerprint": "commit-deadbeef",
            },
        )

        self.assertEqual(response.status_code, 201)
        event = EvidenceEvent.objects.get()
        self.assertEqual(event.source, EvidenceSource.CI_RUN)
        self.assertEqual(event.provenance, EvidenceEvent.Provenance.COLLECTOR)
        self.assertEqual(event.trust, EvidenceEvent.Trust.AUTHENTICATED_API)

    def test_valid_heartbeat_hmac_is_accepted(self):
        response = self._post(
            "/api/steward/heartbeat/",
            {"subject": "personal-openclaw-gateway"},
        )

        self.assertEqual(response.status_code, 201)
        event = EvidenceEvent.objects.get()
        self.assertEqual(event.source, EvidenceSource.GATEWAY_HEARTBEAT)
        self.assertEqual(event.trust, EvidenceEvent.Trust.HOST_LOG)

    def test_replayed_signed_request_is_deduplicated_without_client_fingerprint(self):
        timestamp = int(time.time())
        body = {
            "source": "ci_run",
            "subject": "nbhd-united-main-ci",
            "payload": {"conclusion": "success"},
        }

        first = self._post(
            "/api/steward/evidence/",
            body,
            timestamp=timestamp,
        )
        second = self._post(
            "/api/steward/evidence/",
            body,
            timestamp=timestamp,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(EvidenceEvent.objects.count(), 1)

    def test_bad_signature_is_rejected(self):
        response = self._post(
            "/api/steward/evidence/",
            {"source": "ci_run", "subject": "nbhd-united-main-ci"},
            signature="0" * 64,
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(EvidenceEvent.objects.exists())

    def test_stale_timestamp_is_rejected(self):
        response = self._post(
            "/api/steward/evidence/",
            {"source": "ci_run", "subject": "nbhd-united-main-ci"},
            timestamp=int(time.time()) - 301,
        )
        self.assertEqual(response.status_code, 401)

    def test_oversize_payload_is_rejected(self):
        response = self._post(
            "/api/steward/evidence/",
            {
                "source": "ci_run",
                "subject": "nbhd-united-main-ci",
                "payload": {"detail": "x" * 4097},
            },
        )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(EvidenceEvent.objects.exists())

    def test_invalid_source_is_rejected(self):
        response = self._post(
            "/api/steward/evidence/",
            {"source": "free_text", "subject": "nbhd-united-main-ci"},
        )
        self.assertEqual(response.status_code, 400)

    def test_internal_sources_are_rejected_over_http(self):
        for source in ("eval_run", "eval_slo", "mj_ack"):
            with self.subTest(source=source):
                response = self._post(
                    "/api/steward/evidence/",
                    {
                        "source": source,
                        "subject": f"{source}:forged",
                        "fingerprint": f"forged-{source}",
                    },
                )
                self.assertEqual(response.status_code, 403)
                self.assertIn("internal-only", response.json()["error"])
        self.assertFalse(EvidenceEvent.objects.exists())

    def test_cross_subject_fingerprint_collision_is_not_idempotent_success(self):
        fingerprint = "caller-chosen-shared-fingerprint"
        first = self._post(
            "/api/steward/evidence/",
            {
                "source": "ci_run",
                "subject": "nbhd-united-main-ci",
                "fingerprint": fingerprint,
            },
        )
        with self.assertLogs("apps.steward.services", level="ERROR"):
            collision = self._post(
                "/api/steward/evidence/",
                {
                    "source": "asc_version_state",
                    "subject": "nbhd-ios-version",
                    "fingerprint": fingerprint,
                },
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(collision.status_code, 409)
        self.assertEqual(collision.json()["status"], "collision")
        self.assertFalse(collision.json()["created"])
        self.assertEqual(EvidenceEvent.objects.count(), 1)

    @override_settings(STEWARD_INGEST_SECRET="")
    def test_unconfigured_secret_fails_closed(self):
        response = self._post(
            "/api/steward/evidence/",
            {"source": "ci_run", "subject": "nbhd-united-main-ci"},
        )
        self.assertEqual(response.status_code, 503)

    def test_provenance_cannot_be_spoofed(self):
        response = self._post(
            "/api/steward/evidence/",
            {
                "source": "ci_run",
                "subject": "nbhd-united-main-ci",
                "provenance": "agent_proposed",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("server-controlled", response.json()["error"])
        self.assertFalse(EvidenceEvent.objects.exists())
