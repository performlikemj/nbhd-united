"""Fail-closed guard tests for share-backed cron enumeration."""

from __future__ import annotations

import json
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from apps.cron.share_observer import (
    ShareContentTooSmall,
    ShareFileMissing,
    ShareFileZeroByte,
    ShareJobsEmpty,
    ShareJSONInvalid,
    ShareSchemaInvalid,
    ShareSnapshotShrank,
    ShareStateIncoherent,
    _metadata_cache_key,
    observe_share,
)

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "cron-share-observer-tests",
    }
}


def _job(index: int, *, name: str = "_fuel:welcome") -> dict:
    return {
        "id": f"job-{index:03d}",
        "name": name,
        "enabled": True,
        "createdAtMs": 1_700_000_000_000 + index,
        "schedule": {"kind": "cron", "expr": "25 23 25 4 *", "tz": "Asia/Tokyo"},
    }


def _jobs_content(count: int) -> str:
    return json.dumps({"version": 1, "jobs": [_job(index) for index in range(count)]})


def _state_content(count: int, *, start: int = 0) -> str:
    return json.dumps(
        {
            "version": 1,
            "jobs": {
                f"job-{index:03d}": {
                    "updatedAtMs": 1_700_000_100_000 + index,
                    "state": {},
                }
                for index in range(start, start + count)
            },
        }
    )


@override_settings(CACHES=TEST_CACHES)
class ShareObserverGuardTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.tenant_id = uuid4()

    def _observe(self, jobs: str | None, state: str | None):
        with patch(
            "apps.orchestrator.azure_client.download_workspace_file",
            side_effect=[jobs, state],
        ) as mock_download:
            result = observe_share(self.tenant_id)
        self.assertEqual(
            mock_download.call_args_list,
            [
                ((self.tenant_id, "cron/jobs.json"),),
                ((self.tenant_id, "cron/jobs-state.json"),),
            ],
        )
        return result

    def test_valid_observation_returns_complete_jobs_and_stores_metadata_only(self):
        observation = self._observe(_jobs_content(3), _state_content(3))

        self.assertEqual(observation.count, 3)
        self.assertEqual([job["id"] for job in observation.jobs], ["job-000", "job-001", "job-002"])
        metadata = cache.get(_metadata_cache_key(self.tenant_id))
        self.assertEqual(set(metadata), {"count", "hash", "mtime", "observed_at"})
        self.assertEqual(metadata["count"], 3)
        self.assertEqual(len(metadata["hash"]), 64)
        self.assertNotIn("jobs", metadata)

    def test_missing_file_raises_distinct_guard(self):
        with self.assertRaises(ShareFileMissing):
            self._observe(None, _state_content(1))

    def test_zero_byte_file_raises_distinct_guard(self):
        with self.assertRaises(ShareFileZeroByte):
            self._observe("", _state_content(1))

    def test_unparseable_json_raises_distinct_guard(self):
        with self.assertRaises(ShareJSONInvalid):
            self._observe("{not-json", _state_content(1))

    def test_invalid_schema_raises_distinct_guard(self):
        malformed = json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "job-1",
                        "name": "_fuel:welcome",
                        "schedule": {"expr": "25 23 25 4 *"},
                    }
                ],
            }
        )
        with self.assertRaises(ShareSchemaInvalid):
            self._observe(malformed, _state_content(1))

    def test_empty_jobs_raises_distinct_guard(self):
        padded_empty = json.dumps({"version": 1, "jobs": []}, indent=80)
        with self.assertRaises(ShareJobsEmpty):
            self._observe(padded_empty, json.dumps({"version": 1, "jobs": {}}))

    def test_implausibly_small_valid_content_raises_distinct_guard(self):
        tiny_valid = json.dumps(
            [
                {
                    "id": "x",
                    "name": "n",
                    "schedule": {"kind": "cron"},
                }
            ],
            separators=(",", ":"),
        )
        tiny_state = json.dumps({"version": 1, "jobs": {"x": {}}})
        self.assertLess(len(tiny_valid.encode()), 128)
        with self.assertRaises(ShareContentTooSmall):
            self._observe(tiny_valid, tiny_state)

    def test_jobs_state_with_vastly_fewer_ids_raises_distinct_guard(self):
        with self.assertRaises(ShareStateIncoherent):
            self._observe(_jobs_content(10), _state_content(2))

    def test_jobs_state_with_vastly_more_ids_raises_distinct_guard(self):
        with self.assertRaises(ShareStateIncoherent):
            self._observe(_jobs_content(2), _state_content(10))

    def test_shrinking_snapshot_raises_and_preserves_prior_metadata(self):
        cache.set(
            _metadata_cache_key(self.tenant_id),
            {
                "count": 10,
                "hash": "previous",
                "mtime": "2026-07-31T00:00:00+00:00",
                "observed_at": "2026-07-31T00:00:00+00:00",
            },
            timeout=None,
        )

        with self.assertRaises(ShareSnapshotShrank):
            self._observe(_jobs_content(4), _state_content(4))

        self.assertEqual(cache.get(_metadata_cache_key(self.tenant_id))["hash"], "previous")
