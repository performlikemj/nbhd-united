"""Server-side Apple Maps Search client with credential-safe normalization."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

APPLE_MAPS_TOKEN_URL = "https://maps-api.apple.com/v1/token"
APPLE_MAPS_SEARCH_URL = "https://maps-api.apple.com/v1/search"
APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY = "apple_maps:access_token:v1"
APPLE_MAPS_ACCESS_TOKEN_LOCK_KEY = "apple_maps:access_token_lock:v1"
APPLE_MAPS_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS = 90
APPLE_MAPS_ACCESS_TOKEN_LOCK_SECONDS = 10
APPLE_MAPS_AUTH_JWT_TTL_SECONDS = 20 * 60
APPLE_MAPS_HTTP_TIMEOUT_SECONDS = 10.0

# UNVERIFIED: confirm these result-cache lifetimes against Apple's current Maps
# Server API terms before production rollout.
APPLE_MAPS_SEARCH_CACHE_TTL_SECONDS = 10 * 60
APPLE_MAPS_EMPTY_SEARCH_CACHE_TTL_SECONDS = 60


class PlacesSearchEnvelope(dict):
    """Public response body plus transport-only metadata kept out of JSON."""

    http_status: int
    retry_after: str | None

    def __init__(self, *args, http_status: int = 200, retry_after: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_status = http_status
        self.retry_after = retry_after


class _AppleMapsFailure(Exception):
    reason = "upstream_unavailable"
    http_status = 503

    def __init__(self, *, retry_after: str | None = None):
        super().__init__(self.reason)
        self.retry_after = retry_after


class _AppleMapsAuthUnavailable(_AppleMapsFailure):
    reason = "auth_unavailable"


class _AppleMapsRateLimited(_AppleMapsFailure):
    reason = "rate_limited"
    http_status = 429


class _AppleMapsUpstreamUnavailable(_AppleMapsFailure):
    pass


def _safe_cache_get(key: str):
    try:
        return cache.get(key)
    except Exception:  # noqa: BLE001 - cache outages must not break the request
        logger.warning("Apple Maps cache read failed")
        return None


def _safe_cache_set(key: str, value, *, timeout: int) -> None:
    try:
        cache.set(key, value, timeout=timeout)
    except Exception:  # noqa: BLE001 - cache outages must not break the request
        logger.warning("Apple Maps cache write failed")


def _safe_cache_add(key: str, value, *, timeout: int) -> bool | None:
    try:
        return cache.add(key, value, timeout=timeout)
    except Exception:  # noqa: BLE001 - a missing lock degrades to direct exchange
        logger.warning("Apple Maps cache lock failed")
        return None


def _safe_cache_delete(key: str) -> None:
    try:
        cache.delete(key)
    except Exception:  # noqa: BLE001 - eviction remains best effort
        logger.warning("Apple Maps cache delete failed")


def _apple_maps_auth_jwt(*, private_key: Any, now: int | None = None) -> str:
    """Sign one short-lived auth JWT; neither key nor JWT is retained."""
    import jwt as pyjwt

    issued_at = int(time.time()) if now is None else int(now)
    key_id = str(getattr(settings, "NBHD_APPLE_MAPS_KEY_ID", "") or "").strip()
    team_id = str(getattr(settings, "NBHD_APPLE_MAPS_TEAM_ID", "") or "").strip()
    if not key_id or not team_id:
        raise _AppleMapsAuthUnavailable()

    signing_key = private_key.replace("\\n", "\n") if isinstance(private_key, str) else private_key
    return pyjwt.encode(
        {
            "iss": team_id,
            "iat": issued_at,
            "exp": issued_at + APPLE_MAPS_AUTH_JWT_TTL_SECONDS,
            "scope": "server_api",
        },
        signing_key,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": key_id, "typ": "JWT"},
    )


def _retry_after(response: httpx.Response) -> str | None:
    value = (response.headers.get("Retry-After") or "").strip()
    return value or None


def _exchange_access_token() -> str:
    """Read the .p8 from Key Vault, exchange it, and cache only the access token."""
    from apps.orchestrator.azure_client import read_key_vault_secret

    secret_name = str(getattr(settings, "AZURE_KV_SECRET_APPLE_MAPS_AUTHKEY", "") or "").strip()
    if not secret_name:
        raise _AppleMapsAuthUnavailable()

    private_key = read_key_vault_secret(secret_name)
    if not private_key:
        raise _AppleMapsAuthUnavailable()
    auth_jwt = _apple_maps_auth_jwt(private_key=private_key)

    try:
        response = httpx.get(
            APPLE_MAPS_TOKEN_URL,
            headers={"Authorization": f"Bearer {auth_jwt}"},
            timeout=APPLE_MAPS_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise _AppleMapsUpstreamUnavailable() from exc

    if response.status_code == 429:
        raise _AppleMapsRateLimited(retry_after=_retry_after(response))
    if response.status_code == 401:
        raise _AppleMapsAuthUnavailable()
    if response.status_code >= 500:
        raise _AppleMapsUpstreamUnavailable()
    if not response.is_success:
        raise _AppleMapsAuthUnavailable()

    try:
        payload = response.json()
        access_token = payload["accessToken"]
        expires_in = int(payload["expiresInSeconds"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _AppleMapsUpstreamUnavailable() from exc

    if not isinstance(access_token, str) or not access_token.strip():
        raise _AppleMapsUpstreamUnavailable()
    ttl = expires_in - APPLE_MAPS_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS
    if ttl <= 0:
        raise _AppleMapsUpstreamUnavailable()

    _safe_cache_set(APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY, access_token.strip(), timeout=ttl)
    return access_token.strip()


def _get_access_token(*, force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = _safe_cache_get(APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY)
        if isinstance(cached, str) and cached:
            return cached

    acquired = _safe_cache_add(
        APPLE_MAPS_ACCESS_TOKEN_LOCK_KEY,
        "1",
        timeout=APPLE_MAPS_ACCESS_TOKEN_LOCK_SECONDS,
    )
    try:
        # Re-check after lock acquisition; another worker may have filled it.
        if not force_refresh:
            cached = _safe_cache_get(APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY)
            if isinstance(cached, str) and cached:
                return cached

        if acquired is False and not force_refresh:
            # Briefly allow the lock holder to finish, then fail soft by doing
            # our own exchange if the shared cache is still empty.
            for _ in range(4):
                time.sleep(0.05)
                cached = _safe_cache_get(APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY)
                if isinstance(cached, str) and cached:
                    return cached
        return _exchange_access_token()
    finally:
        if acquired is True:
            _safe_cache_delete(APPLE_MAPS_ACCESS_TOKEN_LOCK_KEY)


def _canonical_search_cache_key(
    *,
    tenant_id: str,
    query: str,
    latitude: float | None,
    longitude: float | None,
    language: str,
    countries: list[str],
    categories: list[str],
) -> str:
    canonical = "|".join(
        (
            query.strip().casefold(),
            "" if latitude is None else f"{latitude:.5f}",
            "" if longitude is None else f"{longitude:.5f}",
            language.casefold(),
            ",".join(sorted(country.upper() for country in countries)),
            ",".join(sorted(category.casefold() for category in categories)),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    tenant_digest = hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()[:24]
    return f"apple_maps:search:v1:{tenant_digest}:{digest}"


def _normalized_place(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    coordinate = raw.get("coordinate")
    if not isinstance(coordinate, dict):
        return None
    try:
        if isinstance(coordinate.get("latitude"), bool) or isinstance(coordinate.get("longitude"), bool):
            return None
        latitude = float(coordinate["latitude"])
        longitude = float(coordinate["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    address_lines = raw.get("formattedAddressLines")
    if not isinstance(address_lines, list):
        address_lines = []

    return {
        "id": str(raw.get("id") or ""),
        "name": name.strip(),
        "latitude": latitude,
        "longitude": longitude,
        "formatted_address_lines": [line for line in address_lines if isinstance(line, str)],
        "country": str(raw.get("country") or ""),
        "country_code": str(raw.get("countryCode") or ""),
        "poi_category": str(raw.get("poiCategory") or ""),
    }


def _success_envelope(results: list[dict[str, Any]], *, fresh: bool, source: str) -> PlacesSearchEnvelope:
    return PlacesSearchEnvelope(
        {
            "verified": True,
            "fresh": fresh,
            "source": source,
            "results": results,
        }
    )


def _degraded_envelope(failure: _AppleMapsFailure) -> PlacesSearchEnvelope:
    return PlacesSearchEnvelope(
        {
            "verified": False,
            "fresh": False,
            "source": "apple_maps",
            "results": [],
            "reason": failure.reason,
        },
        http_status=failure.http_status,
        retry_after=failure.retry_after,
    )


def search_places(
    *,
    tenant_id: str,
    query: str,
    latitude: float | None = None,
    longitude: float | None = None,
    language: str = "",
    countries: list[str] | None = None,
    categories: list[str] | None = None,
    limit: int = 10,
) -> PlacesSearchEnvelope:
    """Search Apple, returning only normalized place data and stable metadata."""
    countries = countries or []
    categories = categories or []
    cache_key = _canonical_search_cache_key(
        tenant_id=tenant_id,
        query=query,
        latitude=latitude,
        longitude=longitude,
        language=language,
        countries=countries,
        categories=categories,
    )
    cached = _safe_cache_get(cache_key)
    if isinstance(cached, list):
        return _success_envelope(cached[:limit], fresh=False, source="apple_maps_cache")

    params: dict[str, str] = {"q": query}
    if latitude is not None and longitude is not None:
        params["userLocation"] = f"{latitude},{longitude}"
    if language:
        params["lang"] = language
    if countries:
        params["limitToCountries"] = ",".join(countries)
    if categories:
        params["includePoiCategories"] = ",".join(categories)

    try:
        access_token = _get_access_token()
        response = _search_request(access_token=access_token, params=params)
        if response.status_code == 401:
            _safe_cache_delete(APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY)
            access_token = _get_access_token(force_refresh=True)
            response = _search_request(access_token=access_token, params=params)
            if response.status_code == 401:
                raise _AppleMapsAuthUnavailable()
        if response.status_code == 429:
            raise _AppleMapsRateLimited(retry_after=_retry_after(response))
        if response.status_code >= 500:
            raise _AppleMapsUpstreamUnavailable()
        if not response.is_success:
            raise _AppleMapsUpstreamUnavailable()

        try:
            payload = response.json()
            raw_results = payload["results"]
            if not isinstance(raw_results, list):
                raise TypeError("results must be a list")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise _AppleMapsUpstreamUnavailable() from exc

        normalized = [place for raw in raw_results if (place := _normalized_place(raw)) is not None]
        normalized = normalized[:limit]
        ttl = APPLE_MAPS_SEARCH_CACHE_TTL_SECONDS if normalized else APPLE_MAPS_EMPTY_SEARCH_CACHE_TTL_SECONDS
        _safe_cache_set(cache_key, normalized, timeout=ttl)
        return _success_envelope(normalized, fresh=True, source="apple_maps")
    except _AppleMapsFailure as failure:
        return _degraded_envelope(failure)


def _search_request(*, access_token: str, params: dict[str, str]) -> httpx.Response:
    try:
        return httpx.get(
            APPLE_MAPS_SEARCH_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=APPLE_MAPS_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise _AppleMapsUpstreamUnavailable() from exc
