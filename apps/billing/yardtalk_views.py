"""YardTalk licensing API — license validation (unauthenticated, device-seat
tracked) and subscription entitlement (PAT-scoped)."""

from __future__ import annotations

import hashlib

from django.core import signing
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.tenants.permissions import HasYardTalkReadScope
from apps.tenants.throttling import _PATScopedThrottle

from .models import YardTalkLicense
from .yardtalk_licensing import (
    DEVICE_SEAT_LIMIT,
    LICENSE_RECEIPT_SALT,
    canonical_license_key,
    is_yardtalk_entitled,
    normalize_license_key,
)

_MAX_DEVICE_ID_LEN = 64


def _client_ip(request) -> str:
    """First hop of the Container Apps proxy header, else REMOTE_ADDR."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


class YardTalkValidateIpThrottle(SimpleRateThrottle):
    """Per-client-IP cap on the unauthenticated validate endpoint."""

    scope = "yardtalk_validate_ip"
    rate = "10/minute"

    def get_cache_key(self, request, view):
        ident = _client_ip(request)
        return self.cache_format % {"scope": self.scope, "ident": ident or "anon"}


class YardTalkValidateKeyThrottle(SimpleRateThrottle):
    """Per-license-key cap on the validate endpoint — caps guessing against ONE
    key even across rotating IPs. The key is hashed so raw keys never land in
    cache keys."""

    scope = "yardtalk_validate_key"
    rate = "5/minute"

    def get_cache_key(self, request, view):
        try:
            raw = request.data.get("license_key") or ""
        except Exception:  # noqa: BLE001 — malformed body: skip throttle, view will 400 it
            return None
        normalized = normalize_license_key(raw)
        if not normalized:
            return None
        ident = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
        return self.cache_format % {"scope": self.scope, "ident": ident}


class YardTalkEntitlementThrottle(_PATScopedThrottle):
    """Per-PAT cap on the entitlement check."""

    scope = "pat_yardtalk_entitlement"
    rate = "30/minute"


class LicenseValidateView(APIView):
    """POST /api/v1/yardtalk/licenses/validate/ — validate a license key and
    track a device seat. Unauthenticated; throttled per IP and per key."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [YardTalkValidateIpThrottle, YardTalkValidateKeyThrottle]

    def post(self, request):
        license_key = request.data.get("license_key")
        device_id = request.data.get("device_id")

        if not isinstance(license_key, str) or not license_key.strip():
            return Response({"detail": "license_key is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(device_id, str):
            return Response({"detail": "device_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        device_id = device_id.strip()
        if not device_id:
            return Response({"detail": "device_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        if len(device_id) > _MAX_DEVICE_ID_LEN:
            return Response({"detail": "device_id too long."}, status=status.HTTP_400_BAD_REQUEST)

        canonical = canonical_license_key(license_key)
        license_obj = YardTalkLicense.objects.filter(key=canonical).first()
        if license_obj is None:
            return Response({"valid": False, "reason": "unknown_key"})
        if license_obj.revoked_at is not None:
            return Response({"valid": False, "reason": "revoked"})

        # Seat check + append are serialized on the row so two devices racing the
        # last free seat can't both consume it.
        seat_limited = False
        with transaction.atomic():
            locked = YardTalkLicense.objects.select_for_update().get(id=license_obj.id)
            device_ids = list(locked.device_ids or [])
            if device_id not in device_ids:
                if len(device_ids) >= DEVICE_SEAT_LIMIT:
                    seat_limited = True
                else:
                    device_ids.append(device_id)
                    locked.device_ids = device_ids
                    locked.save(update_fields=["device_ids", "updated_at"])

        if seat_limited:
            return Response({"valid": False, "reason": "seat_limit"})

        receipt = signing.dumps(
            {"lic": str(license_obj.id), "dev": device_id, "iat": timezone.now().isoformat()},
            salt=LICENSE_RECEIPT_SALT,
        )
        return Response(
            {
                "valid": True,
                "receipt": receipt,
                "seats_remaining": DEVICE_SEAT_LIMIT - len(device_ids),
            }
        )


class EntitlementView(APIView):
    """GET /api/v1/yardtalk/entitlement/ — does this account's subscription
    entitle free YardTalk? PAT-scoped (yardtalk:read); JWT console users bypass
    the scope check by design."""

    permission_classes = [HasYardTalkReadScope]
    throttle_classes = [YardTalkEntitlementThrottle]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        entitled = is_yardtalk_entitled(tenant)
        return Response(
            {
                "entitled": entitled,
                "source": "subscription" if entitled else "none",
                "recheck_after_days": 14,
            }
        )
