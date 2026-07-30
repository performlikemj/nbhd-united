from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.steward.auth import StewardAuthError, validate_steward_hmac
from apps.steward.models import EvidenceEvent, EvidenceSource
from apps.steward.notify import send_urgent
from apps.steward.services import (
    MAX_EVIDENCE_FINGERPRINT_LENGTH,
    generated_fingerprint,
    ingest_evidence,
    validate_payload_size,
)

logger = logging.getLogger(__name__)

_EXTERNAL_EVIDENCE_SOURCES = frozenset(
    {
        EvidenceSource.GATEWAY_HEARTBEAT,
        EvidenceSource.CI_RUN,
        EvidenceSource.ASC_VERSION_STATE,
    }
)


def _auth_error_response(exc: StewardAuthError) -> JsonResponse:
    return JsonResponse({"error": str(exc)}, status=exc.status_code)


def _read_json_object(request: HttpRequest) -> tuple[dict | None, JsonResponse | None]:
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"error": "Request body must be valid JSON."}, status=400)
    if not isinstance(body, dict):
        return None, JsonResponse({"error": "Request body must be a JSON object."}, status=400)
    return body, None


def _validated_subject(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    subject = value.strip()
    if not subject or len(subject) > 128:
        return None
    return subject


def _send_recoveries(result) -> None:
    for expectation in result.recovery_expectations:
        evidence_age_s = max(
            0,
            int((timezone.now() - result.event.occurred_at).total_seconds()),
        )
        try:
            send_urgent(
                subject=f"Steward recovery: {expectation.subject}",
                text=(f"Heartbeat resumed. Miss count: {expectation.miss_count}. Evidence age: {evidence_age_s}s."),
                fingerprint=(f"steward-recovery:{expectation.pk}:{result.event.occurred_at.isoformat()}"),
            )
        except Exception as exc:
            logger.error(
                "Steward recovery notifier raised expectation_id=%s error_class=%s",
                expectation.pk,
                type(exc).__name__,
            )


def _ingest_response(result) -> JsonResponse:
    if result.collision:
        return JsonResponse(
            {
                "status": "collision",
                "created": False,
                "event_id": result.event.pk,
            },
            status=409,
        )
    return JsonResponse(
        {
            "status": "accepted",
            "created": result.created,
            "event_id": result.event.pk,
        },
        status=201 if result.created else 200,
    )


@csrf_exempt
@require_POST
def heartbeat(request: HttpRequest) -> JsonResponse:
    try:
        validate_steward_hmac(request)
    except StewardAuthError as exc:
        return _auth_error_response(exc)

    body, error = _read_json_object(request)
    if error:
        return error
    if "provenance" in body:
        return JsonResponse(
            {"error": "provenance is server-controlled; agent_proposed evidence is forbidden."},
            status=403,
        )
    if set(body) != {"subject"}:
        return JsonResponse(
            {"error": "Heartbeat body must contain only subject."},
            status=400,
        )
    subject = _validated_subject(body.get("subject"))
    if subject is None:
        return JsonResponse({"error": "subject must be a non-empty string of at most 128 characters."}, status=400)

    occurred_at = datetime.fromtimestamp(
        int(request.headers["X-Steward-Timestamp"]),
        UTC,
    )
    received_at = timezone.now()
    result = ingest_evidence(
        source=EvidenceSource.GATEWAY_HEARTBEAT,
        subject=subject,
        occurred_at=occurred_at,
        payload={},
        fingerprint=generated_fingerprint(
            source=EvidenceSource.GATEWAY_HEARTBEAT,
            subject=subject,
            occurred_at=occurred_at,
            payload={},
        ),
        trust=EvidenceEvent.Trust.HOST_LOG,
        provenance=EvidenceEvent.Provenance.COLLECTOR,
        now=received_at,
    )
    _send_recoveries(result)
    return _ingest_response(result)


@csrf_exempt
@require_POST
def evidence(request: HttpRequest) -> JsonResponse:
    try:
        validate_steward_hmac(request)
    except StewardAuthError as exc:
        return _auth_error_response(exc)

    body, error = _read_json_object(request)
    if error:
        return error
    if "provenance" in body:
        return JsonResponse(
            {"error": "provenance is server-controlled; agent_proposed evidence is forbidden."},
            status=403,
        )

    source = body.get("source")
    if source not in EvidenceSource.values:
        return JsonResponse({"error": "source is not a valid Steward evidence source."}, status=400)
    if source not in _EXTERNAL_EVIDENCE_SOURCES:
        return JsonResponse(
            {"error": "source is internal-only and cannot be submitted over HTTP."},
            status=403,
        )
    subject = _validated_subject(body.get("subject"))
    if subject is None:
        return JsonResponse({"error": "subject must be a non-empty string of at most 128 characters."}, status=400)

    occurred_at = datetime.fromtimestamp(
        int(request.headers["X-Steward-Timestamp"]),
        UTC,
    )
    if body.get("occurred_at") is not None:
        if not isinstance(body["occurred_at"], str):
            return JsonResponse({"error": "occurred_at must be an ISO-8601 timestamp."}, status=400)
        parsed = parse_datetime(body["occurred_at"])
        if parsed is None or timezone.is_naive(parsed):
            return JsonResponse({"error": "occurred_at must be a timezone-aware ISO-8601 timestamp."}, status=400)
        occurred_at = parsed.astimezone(UTC)

    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        return JsonResponse({"error": "payload must be a JSON object."}, status=400)
    try:
        validate_payload_size(payload)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=413)

    fingerprint = body.get("fingerprint")
    if fingerprint is None:
        fingerprint = generated_fingerprint(
            source=source,
            subject=subject,
            occurred_at=occurred_at,
            payload=payload,
        )
    if (
        not isinstance(fingerprint, str)
        or not fingerprint.strip()
        or len(f"{source}:{fingerprint.strip()}") > MAX_EVIDENCE_FINGERPRINT_LENGTH
    ):
        return JsonResponse(
            {
                "error": (
                    "fingerprint must be non-empty and produce a source-prefixed "
                    f"value of at most {MAX_EVIDENCE_FINGERPRINT_LENGTH} characters."
                )
            },
            status=400,
        )

    result = ingest_evidence(
        source=source,
        subject=subject,
        occurred_at=occurred_at,
        payload=payload,
        fingerprint=fingerprint.strip(),
        trust=EvidenceEvent.Trust.AUTHENTICATED_API,
        provenance=EvidenceEvent.Provenance.COLLECTOR,
    )
    _send_recoveries(result)
    return _ingest_response(result)
