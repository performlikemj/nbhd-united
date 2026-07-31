"""Read-only, fail-closed observation of a tenant's complete cron share.

The gateway's ``cron.list`` response is capped, so operator cleanup commands
must enumerate from ``cron/jobs.json`` instead.  This module never writes to
the share and never persists job payloads.  It stores only the last validated
observation's count/digest/timestamps in Django's existing shared cache.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from django.core.cache import cache
from django.utils import timezone

SHRINK_FLOOR = 0.5
STATE_OVERLAP_FLOOR = 0.5
MIN_JOBS_FILE_BYTES = 128


class ShareObservationError(Exception):
    """Base class for any condition that makes a share read untrustworthy."""


class ObservationInvalid(ShareObservationError):
    """The downloaded observation cannot be trusted as valid input."""


class ObservationSuspicious(ShareObservationError):
    """The input is valid in isolation but unsafe relative to prior state."""


class ShareFileMissing(ObservationInvalid):
    """A required cron share file does not exist."""


class ShareFileZeroByte(ObservationInvalid):
    """A required cron share file exists but is zero bytes."""


class ShareJSONInvalid(ObservationInvalid):
    """A required cron share file is not parseable JSON."""


class ShareSchemaInvalid(ObservationInvalid):
    """A parsed cron share file does not match the required schema."""


class ShareJobsEmpty(ObservationInvalid):
    """The jobs file contains no scheduled jobs."""


class ShareContentTooSmall(ObservationInvalid):
    """The jobs file is valid-looking but implausibly small."""


class ShareStateIncoherent(ObservationInvalid):
    """The jobs and jobs-state files refer to substantially different IDs."""


class ShareSnapshotShrank(ObservationSuspicious):
    """A validated jobs snapshot shrank below the allowed floor."""


class ShareMetadataInvalid(ObservationInvalid):
    """Stored prior-observation metadata is malformed."""


@dataclass(frozen=True)
class ShareObservation:
    """One validated, complete read of a tenant's cron share."""

    tenant_id: str
    jobs: tuple[dict[str, Any], ...]
    jobs_state: dict[str, dict[str, Any]]
    count: int
    digest: str
    mtime: datetime
    observed_at: datetime


def _metadata_cache_key(tenant_id: object) -> str:
    return f"cron:share-observation:v1:{tenant_id}"


def _download_required(tenant_id: object, path: str) -> str:
    # Keep this import local: tests and callers patch the canonical helper, and
    # the helper owns Azure credential retrieval without exposing storage keys.
    from apps.orchestrator.azure_client import download_workspace_file

    content = download_workspace_file(tenant_id, path)
    if content is None:
        raise ShareFileMissing(f"missing {path}")
    if not isinstance(content, str):
        raise ShareSchemaInvalid(f"{path} download returned non-text content")
    if len(content.encode("utf-8")) == 0:
        raise ShareFileZeroByte(f"zero-byte {path}")
    return content


def _parse_json(content: str, path: str) -> Any:
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ShareJSONInvalid(f"unparseable {path}: {exc}") from exc


def _validate_jobs(parsed: Any) -> tuple[dict[str, Any], ...]:
    # OpenClaw accepts both its versioned object and the legacy top-level list.
    if isinstance(parsed, list):
        raw_jobs = parsed
    elif isinstance(parsed, dict) and parsed.get("version") == 1 and isinstance(parsed.get("jobs"), list):
        raw_jobs = parsed["jobs"]
    else:
        raise ShareSchemaInvalid("cron/jobs.json must be a job list or {version: 1, jobs: [...]}")

    if not raw_jobs:
        raise ShareJobsEmpty("cron/jobs.json contains zero jobs")

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, job in enumerate(raw_jobs):
        if not isinstance(job, dict):
            raise ShareSchemaInvalid(f"cron/jobs.json job {index} is not an object")
        job_id = job.get("id")
        name = job.get("name")
        schedule = job.get("schedule")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ShareSchemaInvalid(f"cron/jobs.json job {index} is missing id")
        if job_id in seen_ids:
            raise ShareSchemaInvalid(f"cron/jobs.json contains duplicate id {job_id!r}")
        if not isinstance(name, str) or not name:
            raise ShareSchemaInvalid(f"cron/jobs.json job {index} is missing name")
        if not isinstance(schedule, dict) or schedule.get("kind") not in {"at", "cron", "every"}:
            raise ShareSchemaInvalid(f"cron/jobs.json job {index} has invalid schedule.kind")
        seen_ids.add(job_id)
        validated.append(job)
    return tuple(validated)


def _validate_jobs_state(parsed: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(parsed, dict) or parsed.get("version") != 1 or not isinstance(parsed.get("jobs"), dict):
        raise ShareSchemaInvalid("cron/jobs-state.json must be {version: 1, jobs: {id: state}}")

    validated: dict[str, dict[str, Any]] = {}
    for job_id, state in parsed["jobs"].items():
        if not isinstance(job_id, str) or not job_id or not isinstance(state, dict):
            raise ShareSchemaInvalid("cron/jobs-state.json contains an invalid job state entry")
        validated[job_id] = state
    return validated


def _assert_state_coherent(jobs: tuple[dict[str, Any], ...], jobs_state: dict[str, dict[str, Any]]) -> None:
    job_ids = {job["id"] for job in jobs}
    state_ids = set(jobs_state)
    if not state_ids:
        raise ShareStateIncoherent(
            f"cron/jobs-state.json references 0 ids while cron/jobs.json references {len(job_ids)}"
        )

    overlap = job_ids & state_ids
    job_overlap = len(overlap) / len(job_ids)
    state_overlap = len(overlap) / len(state_ids)
    if job_overlap < STATE_OVERLAP_FLOOR or state_overlap < STATE_OVERLAP_FLOOR:
        raise ShareStateIncoherent(
            f"cron jobs/state id sets are incoherent: jobs={len(job_ids)} state={len(state_ids)} overlap={len(overlap)}"
        )


def _snapshot_mtime(
    jobs: tuple[dict[str, Any], ...],
    jobs_state: dict[str, dict[str, Any]],
    *,
    fallback: datetime,
) -> datetime:
    """Return the newest scheduler-persisted timestamp available in the files.

    ``download_workspace_file`` deliberately owns Azure credentials and returns
    content only.  The split-state format carries ``updatedAtMs`` per job, which
    is the scheduler's logical file mtime; legacy jobs also carry
    ``createdAtMs``.  Use the observation time only when neither is present.
    """

    timestamps: list[float] = []
    for state_entry in jobs_state.values():
        value = state_entry.get("updatedAtMs")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            timestamps.append(float(value))
    for job in jobs:
        value = job.get("createdAtMs")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            timestamps.append(float(value))
    if not timestamps:
        return fallback
    try:
        return datetime.fromtimestamp(max(timestamps) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return fallback


def _load_previous_count(tenant_id: object) -> int | None:
    previous = cache.get(_metadata_cache_key(tenant_id))
    if previous is None:
        return None
    if not isinstance(previous, dict):
        raise ShareMetadataInvalid("prior share-observation metadata is not an object")
    count = previous.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise ShareMetadataInvalid("prior share-observation metadata has an invalid count")
    return count


def observe_share(tenant: object) -> ShareObservation:
    """Read and validate a tenant's complete cron snapshot from its file share.

    All validation completes before the new observation metadata is stored.
    Any guard failure raises a distinct ``ShareObservationError`` subclass.
    """

    tenant_id = getattr(tenant, "id", tenant)
    jobs_content = _download_required(tenant_id, "cron/jobs.json")
    state_content = _download_required(tenant_id, "cron/jobs-state.json")
    jobs = _validate_jobs(_parse_json(jobs_content, "cron/jobs.json"))
    jobs_state = _validate_jobs_state(_parse_json(state_content, "cron/jobs-state.json"))

    if len(jobs_content.encode("utf-8")) < MIN_JOBS_FILE_BYTES:
        raise ShareContentTooSmall(f"cron/jobs.json is implausibly small: {len(jobs_content.encode('utf-8'))} bytes")

    _assert_state_coherent(jobs, jobs_state)
    previous_count = _load_previous_count(tenant_id)
    if previous_count is not None and len(jobs) < previous_count * SHRINK_FLOOR:
        raise ShareSnapshotShrank(f"cron/jobs.json snapshot shrank {previous_count} -> {len(jobs)}")

    observed_at = timezone.now()
    digest = hashlib.sha256(jobs_content.encode("utf-8")).hexdigest()
    mtime = _snapshot_mtime(jobs, jobs_state, fallback=observed_at)
    observation = ShareObservation(
        tenant_id=str(tenant_id),
        jobs=jobs,
        jobs_state=jobs_state,
        count=len(jobs),
        digest=digest,
        mtime=mtime,
        observed_at=observed_at,
    )
    cache.set(
        _metadata_cache_key(tenant_id),
        {
            "count": observation.count,
            "hash": observation.digest,
            "mtime": observation.mtime.isoformat(),
            "observed_at": observation.observed_at.isoformat(),
        },
        timeout=None,
    )
    return observation
