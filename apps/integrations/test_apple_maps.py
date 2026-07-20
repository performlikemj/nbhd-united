"""Apple Maps signing, caching, normalization, and runtime endpoint tests."""

from __future__ import annotations

import json
import time
from unittest.mock import call, patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .apple_maps import (
    APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY,
    APPLE_MAPS_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS,
    APPLE_MAPS_AUTH_JWT_TTL_SECONDS,
    PlacesSearchEnvelope,
    _apple_maps_auth_jwt,
    _canonical_search_cache_key,
    _exchange_access_token,
    _get_access_token,
    search_places,
)


def _response(status_code: int, payload=None, *, headers=None) -> httpx.Response:
    request = httpx.Request("GET", "https://maps-api.apple.com/v1/search")
    if payload is None:
        return httpx.Response(status_code, request=request, headers=headers)
    return httpx.Response(status_code, request=request, json=payload, headers=headers)


def _apple_place(*, place_id: str = "place-1", name: str = "Kiyomizu-dera") -> dict:
    return {
        "id": place_id,
        "name": name,
        "coordinate": {"latitude": 34.994856, "longitude": 135.785046},
        "formattedAddressLines": ["1-294 Kiyomizu", "Kyoto", "Japan"],
        "country": "Japan",
        "countryCode": "JP",
        "poiCategory": "ReligiousSite",
        "rawSecret": "must-not-survive-normalization",
    }


@override_settings(NBHD_APPLE_MAPS_KEY_ID="KEY1234567", NBHD_APPLE_MAPS_TEAM_ID="TEAM123456")
class AppleMapsJwtTest(TestCase):
    def test_auth_jwt_is_es256_bounded_and_has_only_documented_claims(self):
        private_key = ec.generate_private_key(ec.SECP256R1())
        now = int(time.time())

        token = _apple_maps_auth_jwt(private_key=private_key, now=now)
        decoded = jwt.decode(
            token,
            private_key.public_key(),
            algorithms=["ES256"],
            options={"verify_iat": False},
        )

        self.assertEqual(
            jwt.get_unverified_header(token),
            {"alg": "ES256", "kid": "KEY1234567", "typ": "JWT"},
        )
        self.assertEqual(set(decoded), {"iss", "iat", "exp", "scope"})
        self.assertEqual(decoded["iss"], "TEAM123456")
        self.assertEqual(decoded["iat"], now)
        self.assertEqual(decoded["exp"], now + APPLE_MAPS_AUTH_JWT_TTL_SECONDS)
        self.assertLessEqual(decoded["exp"] - decoded["iat"], 20 * 60)
        self.assertEqual(decoded["scope"], "server_api")
        self.assertNotIn("sub", decoded)
        self.assertNotIn("maps_id", decoded)


@override_settings(
    AZURE_KV_SECRET_APPLE_MAPS_AUTHKEY="apple-maps-server-authkey",
    NBHD_APPLE_MAPS_KEY_ID="KEY1234567",
    NBHD_APPLE_MAPS_TEAM_ID="TEAM123456",
)
class AppleMapsAccessTokenTest(TestCase):
    @patch("apps.integrations.apple_maps._exchange_access_token")
    @patch("apps.integrations.apple_maps._safe_cache_delete")
    @patch("apps.integrations.apple_maps._safe_cache_add", return_value=True)
    @patch("apps.integrations.apple_maps._safe_cache_get", side_effect=[None, "peer-token"])
    def test_stampede_lock_rechecks_before_key_vault_exchange(
        self,
        cache_get,
        cache_add,
        cache_delete,
        exchange,
    ):
        token = _get_access_token()

        self.assertEqual(token, "peer-token")
        self.assertEqual(cache_get.call_count, 2)
        cache_add.assert_called_once()
        cache_delete.assert_called_once()
        exchange.assert_not_called()

    @patch("apps.integrations.apple_maps._safe_cache_set")
    @patch("apps.integrations.apple_maps.httpx.get")
    @patch("apps.integrations.apple_maps._apple_maps_auth_jwt", return_value="signed-auth-jwt")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret", return_value="private-p8")
    def test_access_token_ttl_honors_apple_expiry_minus_skew(
        self,
        read_secret,
        sign_jwt,
        http_get,
        cache_set,
    ):
        http_get.return_value = _response(
            200,
            {"accessToken": "apple-access-token", "expiresInSeconds": 777},
        )

        token = _exchange_access_token()

        self.assertEqual(token, "apple-access-token")
        read_secret.assert_called_once_with("apple-maps-server-authkey")
        sign_jwt.assert_called_once_with(private_key="private-p8")
        cache_set.assert_called_once_with(
            APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY,
            "apple-access-token",
            timeout=777 - APPLE_MAPS_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS,
        )
        auth_header = http_get.call_args.kwargs["headers"]["Authorization"]
        self.assertEqual(auth_header, "Bearer signed-auth-jwt")


class AppleMapsSearchTest(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    @patch("apps.integrations.apple_maps._search_request")
    @patch("apps.integrations.apple_maps._get_access_token", side_effect=["old-token", "new-token"])
    @patch("apps.integrations.apple_maps._safe_cache_delete")
    def test_search_401_evicts_reexchanges_and_retries_once(self, cache_delete, get_token, search_request):
        search_request.side_effect = [
            _response(401, {"error": "expired"}),
            _response(200, {"results": [_apple_place()]}),
        ]

        result = search_places(tenant_id="tenant-a", query="temple", limit=6)

        self.assertTrue(result["verified"])
        self.assertEqual(result["results"][0]["id"], "place-1")
        self.assertEqual(get_token.call_count, 2)
        self.assertEqual(get_token.call_args_list, [call(), call(force_refresh=True)])
        self.assertEqual(
            search_request.call_args_list,
            [
                call(access_token="old-token", params={"q": "temple"}),
                call(access_token="new-token", params={"q": "temple"}),
            ],
        )
        cache_delete.assert_called_once_with(APPLE_MAPS_ACCESS_TOKEN_CACHE_KEY)

    @patch("apps.integrations.apple_maps._search_request", side_effect=[_response(401), _response(401)])
    @patch("apps.integrations.apple_maps._get_access_token", side_effect=["old-token", "new-token"])
    def test_second_401_is_stable_auth_unavailable(self, _get_token, search_request):
        result = search_places(tenant_id="tenant-a", query="temple")

        self.assertEqual(search_request.call_count, 2)
        self.assertEqual(
            result,
            {
                "verified": False,
                "fresh": False,
                "source": "apple_maps",
                "results": [],
                "reason": "auth_unavailable",
            },
        )
        self.assertEqual(result.http_status, 503)

    @patch("apps.integrations.apple_maps._get_access_token", return_value="access-token")
    def test_429_timeout_5xx_and_bad_json_are_stable_degraded(self, _get_token):
        cases = (
            (_response(429, {"private": "raw body"}, headers={"Retry-After": "120"}), "rate_limited", 429),
            (httpx.ReadTimeout("slow"), "upstream_unavailable", 503),
            (_response(503, {"private": "raw body"}), "upstream_unavailable", 503),
            (
                httpx.Response(
                    200,
                    request=httpx.Request("GET", "https://maps-api.apple.com/v1/search"),
                    content=b"not-json",
                ),
                "upstream_unavailable",
                503,
            ),
        )
        for upstream, reason, expected_status in cases:
            with self.subTest(reason=reason, upstream=type(upstream).__name__):
                cache.clear()
                patch_kwargs = (
                    {"side_effect": upstream} if isinstance(upstream, Exception) else {"return_value": upstream}
                )
                with patch("apps.integrations.apple_maps.httpx.get", **patch_kwargs):
                    result = search_places(tenant_id="tenant-a", query="temple")
                self.assertFalse(result["verified"])
                self.assertEqual(result["results"], [])
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result.http_status, expected_status)
                self.assertNotIn("private", json.dumps(result))
                self.assertEqual(result.retry_after, "120" if expected_status == 429 else None)

    @patch("apps.integrations.apple_maps._search_request")
    @patch("apps.integrations.apple_maps._get_access_token", return_value="access-token")
    def test_empty_success_is_not_failure(self, _get_token, search_request):
        search_request.return_value = _response(200, {"results": []})

        result = search_places(tenant_id="tenant-a", query="nothing here")

        self.assertEqual(
            result,
            {"verified": True, "fresh": True, "source": "apple_maps", "results": []},
        )
        self.assertNotIn("reason", result)

    @patch("apps.integrations.apple_maps._search_request")
    @patch("apps.integrations.apple_maps._get_access_token", return_value="access-token")
    def test_malformed_or_non_finite_places_do_not_escape_normalization(self, _get_token, search_request):
        malformed = _apple_place(place_id="bad")
        malformed["coordinate"]["latitude"] = "nan"
        search_request.return_value = _response(200, {"results": [malformed]})

        result = search_places(tenant_id="tenant-a", query="bad place")

        self.assertEqual(result["results"], [])
        self.assertTrue(result["verified"])

    def test_search_cache_key_varies_by_query_region_language_country_and_categories(self):
        base = {
            "tenant_id": "tenant-a",
            "query": "coffee",
            "latitude": 35.0,
            "longitude": 135.0,
            "language": "en-US",
            "countries": ["JP"],
            "categories": ["Cafe"],
        }
        keys = {_canonical_search_cache_key(**base)}
        for field, value in (
            ("query", "tea"),
            ("latitude", 36.0),
            ("longitude", 136.0),
            ("language", "ja-JP"),
            ("countries", ["US"]),
            ("categories", ["Restaurant"]),
            ("tenant_id", "tenant-b"),
        ):
            changed = {**base, field: value}
            keys.add(_canonical_search_cache_key(**changed))
        self.assertEqual(len(keys), 8)
        self.assertTrue(all("coffee" not in key and "35.0" not in key for key in keys))

    @patch("apps.integrations.apple_maps._get_access_token")
    def test_fresh_cache_skips_apple_and_returns_normalized_stale_results(self, get_token):
        key = _canonical_search_cache_key(
            tenant_id="tenant-a",
            query="coffee",
            latitude=35.0,
            longitude=135.0,
            language="en-US",
            countries=["JP"],
            categories=["Cafe"],
        )
        cached_results = [
            {
                "id": "cached-1",
                "name": "Cafe",
                "latitude": 35.0,
                "longitude": 135.0,
                "formatted_address_lines": [],
                "country": "Japan",
                "country_code": "JP",
                "poi_category": "Cafe",
            }
        ]
        cache.set(key, cached_results, timeout=60)

        result = search_places(
            tenant_id="tenant-a",
            query="coffee",
            latitude=35.0,
            longitude=135.0,
            language="en-US",
            countries=["JP"],
            categories=["Cafe"],
        )

        get_token.assert_not_called()
        self.assertEqual(result["source"], "apple_maps_cache")
        self.assertFalse(result["fresh"])
        self.assertEqual(result["results"], cached_results)

    @patch("apps.integrations.apple_maps._search_request")
    @patch("apps.integrations.apple_maps._get_access_token", return_value="super-secret-token")
    def test_success_is_normalized_and_token_free(self, _get_token, search_request):
        search_request.return_value = _response(200, {"results": [_apple_place()]})

        result = search_places(tenant_id="tenant-a", query="temple")

        self.assertEqual(
            result["results"],
            [
                {
                    "id": "place-1",
                    "name": "Kiyomizu-dera",
                    "latitude": 34.994856,
                    "longitude": 135.785046,
                    "formatted_address_lines": ["1-294 Kiyomizu", "Kyoto", "Japan"],
                    "country": "Japan",
                    "country_code": "JP",
                    "poi_category": "ReligiousSite",
                }
            ],
        )
        serialized = json.dumps(result)
        self.assertNotIn("super-secret-token", serialized)
        self.assertNotIn("rawSecret", serialized)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimePlacesSearchViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Places", telegram_chat_id=943100)
        seed_internal_key(self.tenant)
        self.other_tenant = create_tenant(display_name="Other Places", telegram_chat_id=943101)

    def _url(self, query: str = "") -> str:
        suffix = f"?{query}" if query else ""
        return f"/api/v1/integrations/runtime/{self.tenant.id}/places/search/{suffix}"

    def _headers(self, *, tenant_id: str | None = None, key: str = "shared-key") -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def test_missing_key_is_401(self):
        response = self.client.get(self._url("q=cafe"))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "internal_auth_failed"})

    def test_tenant_mismatch_is_401(self):
        response = self.client.get(
            self._url("q=cafe"),
            **self._headers(tenant_id=str(self.other_tenant.id)),
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "internal_auth_failed"})

    def test_blank_q_bad_coords_and_oversized_lists_are_400(self):
        cases = (
            "q=%20",
            "q=cafe&lat=35",
            "q=cafe&lat=91&lon=135",
            "q=cafe&lat=x&lon=135",
            "q=cafe&country=JP,US,CA,GB,FR,DE",
            "q=cafe&categories=" + ",".join(f"Category{i}" for i in range(11)),
        )
        for query in cases:
            with self.subTest(query=query):
                response = self.client.get(self._url(query), **self._headers())
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_request")

    @patch("apps.integrations.runtime_views.search_places")
    def test_success_passes_bounded_params_and_returns_normalized_envelope(self, search):
        search.return_value = PlacesSearchEnvelope(
            {
                "verified": True,
                "fresh": True,
                "source": "apple_maps",
                "results": [
                    {
                        "id": "place-1",
                        "name": "Cafe",
                        "latitude": 35.0,
                        "longitude": 135.0,
                        "formatted_address_lines": ["Kyoto"],
                        "country": "Japan",
                        "country_code": "JP",
                        "poi_category": "Cafe",
                    }
                ],
            }
        )

        response = self.client.get(
            self._url("q=cafe&lat=35&lon=135&lang=ja-JP&country=jp&categories=Cafe&limit=6"),
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), dict(search.return_value))
        search.assert_called_once_with(
            tenant_id=str(self.tenant.id),
            query="cafe",
            latitude=35.0,
            longitude=135.0,
            language="ja-JP",
            countries=["JP"],
            categories=["Cafe"],
            limit=6,
        )
        serialized = response.content.decode()
        for forbidden in ("accessToken", "Authorization", "Bearer", "private-p8", "signed-auth-jwt"):
            self.assertNotIn(forbidden, serialized)

    @patch("apps.integrations.runtime_views.search_places")
    def test_degraded_status_and_retry_after_are_preserved(self, search):
        search.return_value = PlacesSearchEnvelope(
            {
                "verified": False,
                "fresh": False,
                "source": "apple_maps",
                "results": [],
                "reason": "rate_limited",
            },
            http_status=429,
            retry_after="120",
        )

        response = self.client.get(self._url("q=cafe"), **self._headers())

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "120")
        self.assertEqual(response.json(), dict(search.return_value))
