from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

from apps.steward.models import (
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    ReleaseTrain,
    TrackedItem,
)
from apps.steward.services import (
    EvidenceIngestInput,
    ingest_evidence_batch,
    stored_evidence_fingerprint,
)
from apps.steward.trains import PHASE_ORDER, advance_train

logger = logging.getLogger(__name__)

ASC_API_BASE_URL = "https://api.appstoreconnect.apple.com"
ASC_BUNDLE_ID = "org.neighborhoodunited.app"
ASC_TIMEOUT_SECONDS = 15.0
ASC_JWT_TTL_SECONDS = 14 * 60
ASC_JWT_REFRESH_SKEW_SECONDS = 60
_PHASED_LIVE_STATES = frozenset(
    {
        "PENDING_DEVELOPER_RELEASE",
        "PROCESSING_FOR_APP_STORE",
        "PENDING_APPLE_RELEASE",
        "READY_FOR_SALE",
    }
)

_jwt_cache: dict[str, Any] = {
    "token": None,
    "expires_at": 0,
    "key_id": None,
    "issuer_id": None,
}
_app_id_cache: str | None = None


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


def _version_inputs(payload: dict[str, Any], *, collected_at: datetime) -> list[EvidenceIngestInput]:
    versions = payload.get("data")
    if not isinstance(versions, list):
        raise ValueError("App Store Connect versions response has an invalid shape.")
    builds = _included_by_type(payload, "builds")
    phased_releases = _included_by_type(payload, "appStoreVersionPhasedReleases")
    inputs: list[EvidenceIngestInput] = []

    for version in versions:
        if not isinstance(version, dict):
            continue
        version_id = version.get("id")
        attributes = version.get("attributes")
        if not isinstance(version_id, str) or not isinstance(attributes, dict):
            continue
        state = attributes.get("appStoreState")
        version_string = attributes.get("versionString")
        if not isinstance(state, str) or not isinstance(version_string, str):
            continue

        build = builds.get(_relationship_id(version, "build") or "", {})
        build_attributes = build.get("attributes") if isinstance(build.get("attributes"), dict) else {}
        build_number = build_attributes.get("version")
        processing_state = build_attributes.get("processingState")

        phased = phased_releases.get(
            _relationship_id(version, "appStoreVersionPhasedRelease") or "",
            {},
        )
        phased_attributes = phased.get("attributes") if isinstance(phased.get("attributes"), dict) else {}
        phased_state = phased_attributes.get("phasedReleaseState")
        day_number = phased_attributes.get("currentDayNumber")

        base_payload: dict[str, Any] = {
            "state": state,
            "versionString": version_string,
        }
        if isinstance(build_number, (str, int)):
            base_payload["buildNumber"] = build_number
        if isinstance(processing_state, str):
            base_payload["buildProcessingState"] = processing_state
        if state in _PHASED_LIVE_STATES and isinstance(phased_state, str):
            base_payload["phasedState"] = phased_state
        if state in _PHASED_LIVE_STATES and isinstance(day_number, int):
            base_payload["dayNumber"] = day_number

        subject = f"nbhd-ios-{version_string}"
        inputs.append(
            EvidenceIngestInput(
                source=EvidenceSource.ASC_VERSION_STATE,
                subject=subject,
                occurred_at=collected_at,
                payload=base_payload,
                fingerprint=f"asc:{version_id}:{state}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )

        if state not in _PHASED_LIVE_STATES or not isinstance(phased_state, str):
            continue
        phased_payload = {
            **base_payload,
            "phasedState": phased_state,
        }
        if isinstance(day_number, int):
            phased_payload["dayNumber"] = day_number
        phased_fingerprint = f"asc-phased:{version_id}:{phased_state}:{day_number or 0}"
        inputs.append(
            EvidenceIngestInput(
                source=EvidenceSource.ASC_VERSION_STATE,
                subject=subject,
                occurred_at=collected_at,
                payload=phased_payload,
                fingerprint=phased_fingerprint,
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )
        if (
            phased_state == "COMPLETE"
            and Expectation.objects.filter(
                subject=f"{subject}-rollout",
            )
            .exclude(state=Expectation.State.RETIRED)
            .exists()
        ):
            inputs.append(
                EvidenceIngestInput(
                    source=EvidenceSource.ASC_VERSION_STATE,
                    subject=f"{subject}-rollout",
                    occurred_at=collected_at,
                    payload=phased_payload,
                    fingerprint=f"asc-phased-rollout:{version_id}:{phased_state}:{day_number or 0}",
                    trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                    provenance=EvidenceEvent.Provenance.COLLECTOR,
                )
            )
    return inputs


def _new_inputs(inputs: list[EvidenceIngestInput]) -> list[EvidenceIngestInput]:
    fingerprints = [stored_evidence_fingerprint(item.source, item.fingerprint) for item in inputs]
    existing = set(
        EvidenceEvent.objects.filter(fingerprint__in=fingerprints).values_list(
            "fingerprint",
            flat=True,
        )
    )
    return [item for item in inputs if stored_evidence_fingerprint(item.source, item.fingerprint) not in existing]


def _advance_matching_train(
    *,
    version_string: str,
    target_phase: str,
    evidence: EvidenceEvent,
) -> int:
    advances = 0
    target_index = PHASE_ORDER.index(target_phase)
    trains = ReleaseTrain.objects.filter(
        product=TrackedItem.Product.NBHD_IOS,
        version_string=version_string,
    ).exclude(phase__in=[ReleaseTrain.Phase.RELEASED, ReleaseTrain.Phase.ROLLED_BACK])
    for train in trains:
        if PHASE_ORDER.index(train.phase) >= target_index:
            continue
        advance_train(
            train,
            target_phase,
            evidence=evidence,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        advances += 1
    return advances


def collect_asc() -> dict[str, int]:
    """Collect App Store version/build/phased state without tester or customer data."""
    credentials = _credentials()
    if credentials is None:
        logger.info("Steward ASC collector disabled: App Store Connect credentials are incomplete")
        return {"versions": 0, "evidence": 0, "train_advances": 0}
    key_id, issuer_id, private_key = credentials
    token = _asc_jwt(
        key_id=key_id,
        issuer_id=issuer_id,
        private_key=private_key,
    )
    collected_at = timezone.now()

    with httpx.Client(
        base_url=ASC_API_BASE_URL,
        timeout=ASC_TIMEOUT_SECONDS,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        app_id = _resolve_app_id(client)
        # Latest build polling is deliberately metadata-only. The relationship
        # include below attaches build number and processing state to versions.
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
        versions_payload = _response_json(
            client.get(
                f"/v1/apps/{app_id}/appStoreVersions",
                params={
                    "limit": 200,
                    "include": "build,appStoreVersionPhasedRelease",
                    "fields[appStoreVersions]": ("appStoreState,versionString,build,appStoreVersionPhasedRelease"),
                    "fields[builds]": "version,processingState",
                    "fields[appStoreVersionPhasedReleases]": ("phasedReleaseState,currentDayNumber"),
                },
            )
        )

    inputs = _new_inputs(_version_inputs(versions_payload, collected_at=collected_at))
    results = ingest_evidence_batch(inputs, now=collected_at)
    advances = 0
    targets = {
        "WAITING_FOR_REVIEW": ReleaseTrain.Phase.SUBMITTED,
        "IN_REVIEW": ReleaseTrain.Phase.IN_REVIEW,
        "READY_FOR_SALE": ReleaseTrain.Phase.RELEASED,
    }
    for item, result in zip(inputs, results, strict=True):
        state = item.payload.get("state")
        target = targets.get(state)
        if result.created and target is not None and item.fingerprint.startswith("asc:"):
            advances += _advance_matching_train(
                version_string=str(item.payload["versionString"]),
                target_phase=target,
                evidence=result.event,
            )
    versions = versions_payload.get("data")
    return {
        "versions": len(versions) if isinstance(versions, list) else 0,
        "evidence": sum(result.created for result in results),
        "train_advances": advances,
    }
