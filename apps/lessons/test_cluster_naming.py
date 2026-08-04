"""Tests for the async LLM cluster-naming pass (apps.lessons.cluster_naming).

The LLM HTTP call (``_cluster_naming_request``) is mocked in every test — no
network. A lightweight fake ``RedactionSession`` stands in for the heavy PII
model so the redact → rehydrate → scrub round-trip is exercised deterministically
without loading DeBERTa. Covers: cache miss (≤1 call, name written + cached),
cache hit (no call), per-run cap (excess clusters keep deterministic labels),
LLM failure fallback, and the PII round-trip (input redacted, output rehydrated,
unmapped placeholder scrubbed).
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.services import create_tenant

from . import cluster_naming
from .cluster_naming import _member_hash, name_clusters_for_tenant
from .models import Lesson


class _FakeSession:
    """Deterministic stand-in for RedactionSession — replaces a known name only.

    Mirrors the real contract MY code relies on: ``.redact(text)`` returns the
    redacted string and accumulates fresh mints on ``.entity_map`` for the
    caller to union when rehydrating the response.
    """

    def __init__(self, *args, **kwargs):
        self.entity_map: dict[str, str] = {}

    def redact(self, text: str) -> str:
        if "Sarah" in text:
            self.entity_map["[PERSON_1]"] = "Sarah"
            return text.replace("Sarah", "[PERSON_1]")
        return text


@override_settings(CLUSTER_LABEL_LLM_ENABLED=True)
@patch("apps.lessons.cluster_naming.RedactionSession", _FakeSession)
class ClusterNamingTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Naming Tenant", telegram_chat_id=440001)

    def _mk(self, cluster_id: int, text: str, tags: list[str], label: str = "Deterministic") -> Lesson:
        return Lesson.objects.create(
            tenant=self.tenant,
            text=text,
            context="",
            source_type="journal",
            source_ref="",
            tags=tags,
            status="approved",
            cluster_id=cluster_id,
            cluster_label=label,
        )

    @patch("apps.lessons.cluster_naming._cluster_naming_request", return_value="Strength Training")
    def test_cache_miss_calls_llm_writes_and_caches(self, mock_req):
        l1 = self._mk(1, "Squats and deadlifts progressed", ["fitness", "strength"])
        l2 = self._mk(1, "Bench press form improved", ["fitness", "strength"])

        result = name_clusters_for_tenant(str(self.tenant.id))

        mock_req.assert_called_once()
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["named"], 1)
        self.assertEqual(result["cache_hits"], 0)

        labels = set(Lesson.objects.filter(tenant=self.tenant, cluster_id=1).values_list("cluster_label", flat=True))
        self.assertEqual(labels, {"Strength Training"})

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.cluster_label_cache, {_member_hash([l1.id, l2.id]): "Strength Training"})

    @patch("apps.lessons.cluster_naming._cluster_naming_request", return_value="Should Not Be Used")
    def test_cache_hit_skips_llm(self, mock_req):
        l1 = self._mk(1, "Squats", ["fitness"])
        l2 = self._mk(1, "Bench", ["fitness"])
        self.tenant.cluster_label_cache = {_member_hash([l1.id, l2.id]): "Cached Name"}
        self.tenant.save(update_fields=["cluster_label_cache"])

        result = name_clusters_for_tenant(str(self.tenant.id))

        mock_req.assert_not_called()
        self.assertEqual(result["cache_hits"], 1)
        self.assertEqual(result["calls"], 0)
        labels = set(Lesson.objects.filter(cluster_id=1).values_list("cluster_label", flat=True))
        self.assertEqual(labels, {"Cached Name"})

    @patch("apps.lessons.cluster_naming._cluster_naming_request", return_value="Sprint Work")
    def test_per_run_cap_leaves_excess_deterministic(self, mock_req):
        # 7 clusters, all cache-miss → cap at MAX_CALLS_PER_RUN (5).
        for cid in range(1, 8):
            self._mk(cid, f"lesson a for {cid}", ["topic"], label=f"DET-{cid}")
            self._mk(cid, f"lesson b for {cid}", ["topic"], label=f"DET-{cid}")

        result = name_clusters_for_tenant(str(self.tenant.id))

        self.assertEqual(result["calls"], cluster_naming.MAX_CALLS_PER_RUN)
        self.assertEqual(result["named"], cluster_naming.MAX_CALLS_PER_RUN)
        self.assertEqual(mock_req.call_count, cluster_naming.MAX_CALLS_PER_RUN)

        # A named cluster no longer carries its deterministic sentinel; a capped
        # one still does (title-case-agnostic check).
        named_clusters = {
            cid for cid in range(1, 8) if not Lesson.objects.filter(cluster_id=cid, cluster_label=f"DET-{cid}").exists()
        }
        self.assertEqual(len(named_clusters), cluster_naming.MAX_CALLS_PER_RUN)
        self.assertEqual(7 - len(named_clusters), 7 - cluster_naming.MAX_CALLS_PER_RUN)

    @patch("apps.lessons.cluster_naming._cluster_naming_request", side_effect=RuntimeError("openrouter 500"))
    def test_llm_failure_keeps_deterministic_and_does_not_cache(self, mock_req):
        self._mk(1, "Squats", ["fitness"], label="Fitness")
        self._mk(1, "Bench", ["fitness"], label="Fitness")

        result = name_clusters_for_tenant(str(self.tenant.id))

        mock_req.assert_called_once()
        self.assertEqual(result["named"], 0)
        self.assertEqual(result["calls"], 1)
        labels = set(Lesson.objects.filter(cluster_id=1).values_list("cluster_label", flat=True))
        self.assertEqual(labels, {"Fitness"})
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.cluster_label_cache, {})

    def test_pii_round_trip_input_redacted_output_rehydrated(self):
        # A name in the snippet must be redacted before egress; if the model
        # echoes the placeholder it must be rehydrated to the real value.
        self._mk(1, "Sarah pushed me through interval runs", ["running"])
        self._mk(1, "Ran intervals with Sarah again", ["running"])

        captured: dict[str, object] = {}

        def _capture(messages, tenant_id=None):
            captured["prompt"] = messages[1]["content"]
            return "[PERSON_1] Running"  # model echoes the redacted placeholder

        with patch("apps.lessons.cluster_naming._cluster_naming_request", side_effect=_capture):
            name_clusters_for_tenant(str(self.tenant.id))

        # Input: the real name never reached the model; the placeholder did.
        self.assertNotIn("Sarah", captured["prompt"])
        self.assertIn("[PERSON_1]", captured["prompt"])
        # Output: rehydrated from the fresh mint, no placeholder stored.
        label = Lesson.objects.filter(cluster_id=1).values_list("cluster_label", flat=True).first()
        self.assertIn("Sarah", label)
        self.assertNotIn("[PERSON_1]", label)

    @patch("apps.lessons.cluster_naming._cluster_naming_request", return_value="[PERSON_9|unresolved] Training")
    def test_unmapped_placeholder_is_scrubbed(self, _mock_req):
        self._mk(1, "gym session", ["fitness"])
        self._mk(1, "another gym session", ["fitness"])

        name_clusters_for_tenant(str(self.tenant.id))

        label = Lesson.objects.filter(cluster_id=1).values_list("cluster_label", flat=True).first()
        self.assertNotIn("[PERSON_9", label)
        self.assertIn("Training", label)

    @override_settings(CLUSTER_LABEL_LLM_ENABLED=False)
    @patch("apps.lessons.cluster_naming._cluster_naming_request", return_value="Nope")
    def test_kill_switch_disables_naming(self, mock_req):
        self._mk(1, "gym", ["fitness"], label="Fitness")
        self._mk(1, "gym2", ["fitness"], label="Fitness")

        result = name_clusters_for_tenant(str(self.tenant.id))

        self.assertEqual(result, {"skipped": "disabled"})
        mock_req.assert_not_called()
        labels = set(Lesson.objects.filter(cluster_id=1).values_list("cluster_label", flat=True))
        self.assertEqual(labels, {"Fitness"})
