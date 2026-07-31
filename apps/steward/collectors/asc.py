from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.steward.collectors.status import collector_failed, collector_succeeded
from apps.steward.models import (
    AscVersionSnapshot,
    CollectorStatus,
    EvidenceEvent,
    EvidenceSource,
    ReleaseTrain,
    TrackedItem,
)
from apps.steward.services import EvidenceIngestInput, ingest_evidence_batch
from apps.steward.trains import PHASE_ORDER, advance_train

logger = logging.getLogger(__name__)

ASC_API_BASE_URL = "https://api.appstoreconnect.apple.com"
ASC_BUNDLE_ID = "org.neighborhoodunited.app"
ASC_TIMEOUT_SECONDS = 15.0
ASC_JWT_TTL_SECONDS = 14 * 60
ASC_JWT_REFRESH_SKEW_SECONDS = 60
ASC_MAX_PAGES = 3
_PHASED_LIVE_STATES = frozenset(
    {
        "PENDING_DEVELOPER_RELEASE",
        "PROCESSING_FOR_APP_STORE",
        "PENDING_APPLE_RELEASE",
        "READY_FOR_SALE",
        "READY_FOR_DISTRIBUTION",
    }
)

_jwt_cache: dict[str, Any] = {
    "token": None,
    "expires_at": 0,
    "key_id": None,
    "issuer_id": None,
}
_app_id_cache: str | None = None


def _fingerprint_hash(*parts: object) -> str:
    material = json.dumps(parts, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _credentials() -> tuple[str, str, str] | None:
    key_id = str(getattr(settings, "STEWARD_ASC_KEY_ID", "") or "").strip()
    issuer_id = str(getattr(settings, "STEWARD_ASC_ISSUER_ID", "") or "").strip()
    private_key = str(getattr(settings, "STEWARD_ASC_PRIVATE_KEY", "") or "")
    if not key_id or not issuer_id or not private_key.strip():
        return None
    return key_id, issuer_id, private_key.replace("\\n", "\n")


def _asc_jwt(*, key_id: str, issuer_id: str, private_key: str, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    cached = _jwt_cache.get("token")
    if (
        cached
        and _jwt_cache.get("key_id") == key_id
        and _jwt_cache.get("issuer_id") == issuer_id
        and issued_at < int(_jwt_cache.get("expires_at") or 0) - ASC_JWT_REFRESH_SKEW_SECONDS
    ):
        return str(cached)

    import jwt as pyjwt

    expires_at = issued_at + ASC_JWT_TTL_SECONDS
    token = pyjwt.encode(
        {
            "iss": issuer_id,
            "iat": issued_at,
            "exp": expires_at,
            "aud": "appstoreconnect-v1",
        },
        private_key,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": key_id, "typ": "JWT"},
    )
    _jwt_cache.update(
        {
            "token": token,
            "expires_at": expires_at,
            "key_id": key_id,
            "issuer_id": issuer_id,
        }
    )
    return token


def _response_json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("App Store Connect response has an invalid shape.")
    return payload


def _resolve_app_id(client: httpx.Client) -> str:
    global _app_id_cache
    if _app_id_cache:
        return _app_id_cache
    payload = _response_json(
        client.get(
            "/v1/apps",
            params={
                "filter[bundleId]": ASC_BUNDLE_ID,
                "limit": 1,
            },
        )
    )
    apps = payload.get("data")
    if not isinstance(apps, list) or len(apps) != 1 or not isinstance(apps[0], dict):
        raise ValueError("App Store Connect app lookup did not return exactly one app.")
    app_id = apps[0].get("id")
    if not isinstance(app_id, str) or not app_id:
        raise ValueError("App Store Connect app id is missing.")
    _app_id_cache = app_id
    return app_id


def _relationship_id(resource: dict[str, Any], relationship: str) -> str | None:
    relationships = resource.get("relationships")
    if not isinstance(relationships, dict):
        return None
    relation = relationships.get(relationship)
    if not isinstance(relation, dict):
        return None
    data = relation.get("data")
    if not isinstance(data, dict):
        return None
    resource_id = data.get("id")
    return resource_id if isinstance(resource_id, str) and resource_id else None


def _included_by_type(payload: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    included = payload.get("included")
    if not isinstance(included, list):
        return {}
    return {
        item["id"]: item
        for item in included
        if isinstance(item, dict) and item.get("type") == resource_type and isinstance(item.get("id"), str)
    }


def _version_pages(client: httpx.Client, app_id: str) -> tuple[dict[str, Any], bool]:
    path: str = f"/v1/apps/{app_id}/appStoreVersions"
    params: dict[str, Any] | None = {
        "filter[platform]": "IOS",
        "limit": 200,
        "include": "build,appStoreVersionPhasedRelease",
        "fields[appStoreVersions]": ("appVersionState,appStoreState,versionString,build,appStoreVersionPhasedRelease"),
        "fields[builds]": "version,processingState",
        "fields[appStoreVersionPhasedReleases]": "phasedReleaseState,currentDayNumber",
    }
    versions: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    truncated = False
    for page_number in range(1, ASC_MAX_PAGES + 1):
        payload = _response_json(client.get(path, params=params))
        page_versions = payload.get("data")
        if not isinstance(page_versions, list):
            raise ValueError("App Store Connect versions response has an invalid shape.")
        versions.extend(item for item in page_versions if isinstance(item, dict))
        page_included = payload.get("included")
        if isinstance(page_included, list):
            included.extend(item for item in page_included if isinstance(item, dict))
        links = payload.get("links")
        next_link = links.get("next") if isinstance(links, dict) else None
        if not isinstance(next_link, str) or not next_link:
            break
        if page_number == ASC_MAX_PAGES:
            truncated = True
            break
        path = next_link
        params = None
    return {"data": versions, "included": included}, truncated


def _canonical_string(value: object, *, allow_int: bool = False) -> str:
    if isinstance(value, str):
        return value
    if allow_int and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _version_changes(
    payload: dict[str, Any],
    *,
    collected_at: datetime,
) -> tuple[list[EvidenceIngestInput], list[AscVersionSnapshot]]:
    versions = payload.get("data")
    if not isinstance(versions, list):
        raise ValueError("App Store Connect versions response has an invalid shape.")
    builds = _included_by_type(payload, "builds")
    phased_releases = _included_by_type(payload, "appStoreVersionPhasedReleases")
    snapshots_by_id: dict[str, AscVersionSnapshot] = {}

    for version in versions:
        if not isinstance(version, dict):
            continue
        version_id = version.get("id")
        attributes = version.get("attributes")
        if not isinstance(version_id, str) or not version_id or not isinstance(attributes, dict):
            continue
        state = attributes.get("appVersionState")
        if not isinstance(state, str):
            state = attributes.get("appStoreState")
        version_string = attributes.get("versionString")
        if not isinstance(state, str) or not isinstance(version_string, str):
            continue

        build = builds.get(_relationship_id(version, "build") or "", {})
        build_attributes = build.get("attributes") if isinstance(build.get("attributes"), dict) else {}
        build_number = _canonical_string(build_attributes.get("version"), allow_int=True)
        processing_state = _canonical_string(build_attributes.get("processingState"))

        phased = phased_releases.get(
            _relationship_id(version, "appStoreVersionPhasedRelease") or "",
            {},
        )
        phased_attributes = phased.get("attributes") if isinstance(phased.get("attributes"), dict) else {}
        phased_state = ""
        phased_day = None
        if state in _PHASED_LIVE_STATES:
            phased_state = _canonical_string(phased_attributes.get("phasedReleaseState"))
            day_number = phased_attributes.get("currentDayNumber")
            if isinstance(day_number, int) and not isinstance(day_number, bool) and day_number >= 0:
                phased_day = day_number

        snapshots_by_id[version_id] = AscVersionSnapshot(
            version_id=version_id,
            version_string=version_string,
            app_state=state,
            build_number=build_number,
            build_processing_state=processing_state,
            phased_state=phased_state,
            phased_day=phased_day,
            updated_at=collected_at,
        )

    existing = {
        snapshot.version_id: snapshot
        for snapshot in AscVersionSnapshot.objects.select_for_update().filter(version_id__in=snapshots_by_id)
    }
    inputs: list[EvidenceIngestInput] = []
    for version_id, snapshot in snapshots_by_id.items():
        previous = existing.get(version_id)
        state_tuple = snapshot.state_tuple()
        if previous is not None and previous.state_tuple() == state_tuple:
            snapshot.revision = previous.revision
            continue
        snapshot.revision = previous.revision + 1 if previous is not None else 0
        payload = {
            "versionString": snapshot.version_string,
            "state": snapshot.app_state,
            "buildNumber": snapshot.build_number,
            "buildProcessingState": snapshot.build_processing_state,
            "phasedState": snapshot.phased_state,
            "dayNumber": snapshot.phased_day,
        }
        inputs.append(
            EvidenceIngestInput(
                source=EvidenceSource.ASC_VERSION_STATE,
                subject=f"nbhd-ios-{snapshot.version_string}",
                occurred_at=collected_at,
                payload=payload,
                fingerprint=f"asc-state:{_fingerprint_hash(version_id, snapshot.revision)}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )
    return inputs, list(snapshots_by_id.values())


def _upsert_snapshots(snapshots: list[AscVersionSnapshot]) -> None:
    if not snapshots:
        return
    AscVersionSnapshot.objects.bulk_create(
        snapshots,
        update_conflicts=True,
        update_fields=[
            "version_string",
            "app_state",
            "build_number",
            "build_processing_state",
            "phased_state",
            "phased_day",
            "revision",
            "updated_at",
        ],
        unique_fields=["version_id"],
    )


ASC_TRAIN_TARGETS = {
    "WAITING_FOR_REVIEW": ReleaseTrain.Phase.SUBMITTED,
    "IN_REVIEW": ReleaseTrain.Phase.IN_REVIEW,
    "READY_FOR_SALE": ReleaseTrain.Phase.RELEASED,
    "READY_FOR_DISTRIBUTION": ReleaseTrain.Phase.RELEASED,
}


def _recover_train_advances() -> int:
    advances = 0
    trains = ReleaseTrain.objects.filter(
        product=TrackedItem.Product.NBHD_IOS,
    ).exclude(phase__in=[ReleaseTrain.Phase.RELEASED, ReleaseTrain.Phase.ROLLED_BACK])
    for train in trains:
        events = EvidenceEvent.objects.filter(
            source=EvidenceSource.ASC_VERSION_STATE,
            subject=f"nbhd-ios-{train.version_string}",
            occurred_at__gte=train.phase_changed_at,
        ).order_by("-occurred_at", "-id")
        candidates = [(ASC_TRAIN_TARGETS.get(event.payload.get("state")), event) for event in events]
        candidates = [
            (target, event)
            for target, event in candidates
            if target is not None and PHASE_ORDER.index(target) > PHASE_ORDER.index(train.phase)
        ]
        if not candidates:
            continue
        target_phase, evidence = max(
            candidates,
            key=lambda candidate: PHASE_ORDER.index(candidate[0]),
        )
        advance_train(
            train,
            target_phase,
            evidence=evidence,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        advances += 1
    return advances


@transaction.atomic
def _persist_collection(
    payload: dict[str, Any],
    *,
    collected_at: datetime,
    truncated: bool,
    baseline_mode: bool,
) -> tuple[list[AscVersionSnapshot], int, int]:
    inputs, snapshots = _version_changes(
        payload,
        collected_at=collected_at,
    )
    if baseline_mode:
        inputs = []
    results = ingest_evidence_batch(inputs, now=collected_at)
    _upsert_snapshots(snapshots)
    if not truncated:
        returned_ids = [snapshot.version_id for snapshot in snapshots]
        AscVersionSnapshot.objects.exclude(version_id__in=returned_ids).delete()
    advances = _recover_train_advances()
    return snapshots, sum(result.created for result in results), advances


def collect_asc() -> dict[str, int]:
    """Collect paginated App Store version/build/phased state without user data."""
    collected_at = timezone.now()
    baseline_mode = not AscVersionSnapshot.objects.exists()
    credentials = _credentials()
    if credentials is None:
        logger.info("Steward ASC collector disabled: App Store Connect credentials are incomplete")
        collector_failed(
            CollectorStatus.Collector.ASC,
            attempted_at=collected_at,
            error_class="not_configured",
            detail="App Store Connect credentials incomplete",
        )
        return {"versions": 0, "evidence": 0, "train_advances": 0}
    key_id, issuer_id, private_key = credentials

    try:
        token = _asc_jwt(
            key_id=key_id,
            issuer_id=issuer_id,
            private_key=private_key,
        )
        with httpx.Client(
            base_url=ASC_API_BASE_URL,
            timeout=ASC_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            app_id = _resolve_app_id(client)
            _response_json(
                client.get(
                    "/v1/builds",
                    params={
                        "filter[app]": app_id,
                        "sort": "-uploadedDate",
                        "limit": 1,
                        "fields[builds]": "version,processingState",
                    },
                )
            )
            versions_payload, truncated = _version_pages(client, app_id)

        snapshots, evidence_count, advances = _persist_collection(
            versions_payload,
            collected_at=collected_at,
            truncated=truncated,
            baseline_mode=baseline_mode,
        )
    except Exception as exc:
        collector_failed(
            CollectorStatus.Collector.ASC,
            attempted_at=collected_at,
            error_class=type(exc).__name__,
            detail="App Store Connect collection failed",
        )
        raise

    if truncated:
        collector_failed(
            CollectorStatus.Collector.ASC,
            attempted_at=collected_at,
            error_class="truncated",
            detail=";".join(
                [
                    f"pagination exceeded {ASC_MAX_PAGES} pages",
                    *(["baseline_established"] if baseline_mode else []),
                ]
            ),
        )
    else:
        collector_succeeded(
            CollectorStatus.Collector.ASC,
            attempted_at=collected_at,
            detail=";".join(
                [
                    f"versions={len(snapshots)}",
                    *(["baseline_established"] if baseline_mode else []),
                ]
            ),
        )
    return {
        "versions": len(snapshots),
        "evidence": evidence_count,
        "train_advances": advances,
    }
