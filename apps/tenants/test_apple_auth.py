"""Contract tests for the Sign in with Apple web popup backend."""

from __future__ import annotations

import io
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from .apple_client import (
    APPLE_ISSUER,
    AppleGrant,
    AppleInvalidGrant,
    AppleUnavailable,
    generate_apple_client_secret,
    verify_apple_id_token,
)
from .apple_crypto import decrypt_apple_refresh_token, encrypt_apple_refresh_token
from .apple_models import AppleAuthTransaction, AppleRevocationOutbox, ExternalIdentity
from .models import Tenant
from .serializers import UserSerializer
from .throttling import AppleBeginMinuteThrottle, AppleCompleteMinuteThrottle, AppleLinkMinuteThrottle

User = get_user_model()

SERVICES_ID = "org.hoodunited.web"
TEAM_ID = "TEAMID1234"
KEY_ID = "KEYID12345"
JWT_KID = "apple-rsa-key"
REDIRECT_URI = "https://hoodunited.org"
STATE = "s" * 43
NONCE = "n" * 43
SUBJECT = "apple-subject-123"

_ec_private_key = ec.generate_private_key(ec.SECP256R1())
EC_PRIVATE_PEM = _ec_private_key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_rsa_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_other_rsa_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_rsa_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_rsa_private_key.public_key()))
_rsa_jwk.update({"kid": JWT_KID, "use": "sig", "alg": "RS256"})
SIGNING_KEY = jwt.PyJWK.from_dict(_rsa_jwk)
FERNET_KEY = Fernet.generate_key().decode()

READY_SETTINGS = {
    "APPLE_SIWA_SERVICES_ID": SERVICES_ID,
    "APPLE_SIWA_TEAM_ID": TEAM_ID,
    "APPLE_SIWA_KEY_ID": KEY_ID,
    "APPLE_SIWA_PRIVATE_KEY": EC_PRIVATE_PEM,
    "APPLE_SIWA_REDIRECT_URI": REDIRECT_URI,
    "APPLE_SIWA_TOKEN_ENC_KEYS": [FERNET_KEY],
    "APPLE_SIWA_TRANSACTION_TTL_SECONDS": 600,
    "PREVIEW_ACCESS_KEY": "",
}


class FakeResponse:
    def __init__(self, status_code=200, body=None, *, json_error=None):
        self.status_code = status_code
        self.body = body
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.body


class StaticJwksClient:
    def __init__(self, keys=None, *, refresh_error=None, initial_error=None):
        self.keys = list(keys if keys is not None else [SIGNING_KEY])
        self.refresh_error = refresh_error
        self.initial_error = initial_error
        self.calls: list[bool] = []

    def get_signing_keys(self, refresh=False):
        self.calls.append(refresh)
        if refresh and self.refresh_error is not None:
            raise self.refresh_error
        if not refresh and self.initial_error is not None:
            raise self.initial_error
        return self.keys


class AppleFixtureMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.jwks = StaticJwksClient()
        self.jwks_patch = patch("apps.tenants.apple_client._jwks_client", self.jwks)
        self.jwks_patch.start()
        self.publish_patch = patch("apps.cron.publish.publish_task")
        self.publish = self.publish_patch.start()
        from . import apple_client

        with apple_client._unknown_kids_lock:
            apple_client._unknown_kids.clear()
        self.addCleanup(self.jwks_patch.stop)
        self.addCleanup(self.publish_patch.stop)
        self.addCleanup(cache.clear)

    def mint_transaction(self, *, state=STATE, nonce=NONCE, ttl=600, consumed=False):
        row = AppleAuthTransaction.objects.create(
            state=state,
            nonce_hash=__import__("hashlib").sha256(nonce.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(seconds=ttl),
            consumed_at=timezone.now() if consumed else None,
        )
        return row, nonce

    def id_token(
        self,
        *,
        nonce=NONCE,
        subject=SUBJECT,
        email="apple@example.com",
        email_verified="true",
        private_key=None,
        kid=JWT_KID,
        overrides=None,
        remove=(),
    ):
        claims = {
            "iss": APPLE_ISSUER,
            "aud": SERVICES_ID,
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "sub": subject,
            "nonce": nonce,
            "email": email,
            "email_verified": email_verified,
        }
        claims.update(overrides or {})
        for claim in remove:
            claims.pop(claim, None)
        return jwt.encode(
            claims,
            private_key or _rsa_private_key,
            algorithm="RS256",
            headers={"kid": kid},
        )

    def token_response(
        self,
        *,
        nonce=NONCE,
        subject=SUBJECT,
        email="apple@example.com",
        email_verified="true",
        refresh_token="apple-refresh-token",
        status_code=200,
        token_overrides=None,
        remove_claims=(),
        private_key=None,
        kid=JWT_KID,
    ):
        body = {
            "id_token": self.id_token(
                nonce=nonce,
                subject=subject,
                email=email,
                email_verified=email_verified,
                overrides=token_overrides,
                remove=remove_claims,
                private_key=private_key,
                kid=kid,
            )
        }
        if refresh_token is not None:
            body["refresh_token"] = refresh_token
        return FakeResponse(status_code, body)

    def post_complete(self, row, response, *, state=STATE, code="apple-code"):
        with patch("apps.tenants.apple_client.httpx.post", return_value=response) as mocked:
            result = self.client.post(
                reverse("auth-apple-complete"),
                {"transaction_id": str(row.id), "code": code, "state": state},
                format="json",
            )
        return result, mocked

    def make_user(self, *, email="local@example.com", password="CorrectHorse123!", active=True):
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            is_active=active,
        )
        return user

    def make_identity(
        self,
        user,
        *,
        subject=SUBJECT,
        refresh_token="stored-refresh",
        email="",
        last_login_at=None,
    ):
        return ExternalIdentity.objects.create(
            user=user,
            subject=subject,
            audience=SERVICES_ID,
            email_at_auth=email,
            email_is_relay=email.endswith("@privaterelay.appleid.com"),
            email_verified_at_auth=bool(email),
            refresh_token_encrypted=encrypt_apple_refresh_token(refresh_token),
            refresh_token_updated_at=timezone.now(),
            last_login_at=last_login_at or timezone.now(),
        )


class AppleReadinessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def test_each_required_setting_fails_all_three_endpoints_before_auth_or_db(self):
        missing_values = {
            "APPLE_SIWA_SERVICES_ID": "",
            "APPLE_SIWA_TEAM_ID": "",
            "APPLE_SIWA_KEY_ID": "",
            "APPLE_SIWA_PRIVATE_KEY": "",
            "APPLE_SIWA_REDIRECT_URI": "",
            "APPLE_SIWA_TRANSACTION_TTL_SECONDS": 0,
            "APPLE_SIWA_TOKEN_ENC_KEYS": [],
        }
        urls = (
            reverse("auth-apple-begin"),
            reverse("auth-apple-complete"),
            reverse("auth-apple-link"),
        )
        for setting_name, value in missing_values.items():
            configured = {**READY_SETTINGS, setting_name: value}
            with self.subTest(setting=setting_name), self.settings(**configured):
                for url in urls:
                    response = self.client.post(url, {}, format="json")
                    self.assertEqual(response.status_code, 503, response.content)
                    self.assertEqual(response.data, {"error": "not_configured"})

    @override_settings(**READY_SETTINGS)
    def test_non_p256_key_and_noncanonical_redirect_fail_readiness(self):
        rsa_pem = _rsa_private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        for override in (
            {"APPLE_SIWA_PRIVATE_KEY": rsa_pem},
            {"APPLE_SIWA_REDIRECT_URI": f"{REDIRECT_URI}/"},
            {"APPLE_SIWA_REDIRECT_URI": "http://hoodunited.org"},
            {"APPLE_SIWA_TOKEN_ENC_KEYS": ["not-a-fernet-key"]},
        ):
            with self.subTest(override=next(iter(override))), self.settings(**override):
                response = self.client.post(reverse("auth-apple-begin"), {}, format="json")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.data, {"error": "not_configured"})


@override_settings(**READY_SETTINGS)
class AppleBeginAndTransactionTests(AppleFixtureMixin, TestCase):
    def test_begin_shape_storage_and_bounded_cleanup(self):
        now = timezone.now()
        AppleAuthTransaction.objects.bulk_create(
            [
                AppleAuthTransaction(
                    state=f"expired-{index}",
                    nonce_hash="a" * 64,
                    expires_at=now - timedelta(seconds=1),
                )
                for index in range(105)
            ]
        )
        live = AppleAuthTransaction.objects.create(
            state="live",
            nonce_hash="b" * 64,
            expires_at=now + timedelta(minutes=5),
        )

        response = self.client.post(reverse("auth-apple-begin"), {}, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            set(response.data),
            {"transaction_id", "state", "nonce", "expires_in"},
        )
        self.assertEqual(response.data["expires_in"], 600)
        self.assertEqual(len(response.data["state"]), 43)
        self.assertEqual(len(response.data["nonce"]), 43)
        row = AppleAuthTransaction.objects.get(id=response.data["transaction_id"])
        self.assertEqual(row.state, response.data["state"])
        self.assertNotEqual(row.nonce_hash, response.data["nonce"])
        self.assertEqual(
            row.nonce_hash,
            __import__("hashlib").sha256(response.data["nonce"].encode()).hexdigest(),
        )
        self.assertEqual(AppleAuthTransaction.objects.filter(expires_at__lte=now).count(), 5)
        self.assertTrue(AppleAuthTransaction.objects.filter(id=live.id).exists())

    def test_begin_rejects_nonempty_or_malformed_body(self):
        extra = self.client.post(reverse("auth-apple-begin"), {"invite_code": "no"}, format="json")
        malformed = self.client.generic(
            "POST",
            reverse("auth-apple-begin"),
            b"{",
            content_type="application/json",
        )
        self.assertEqual(extra.status_code, 400)
        self.assertEqual(extra.data, {"error": "invalid_request"})
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.data, {"error": "invalid_request"})

    def test_malformed_wrong_type_bad_uuid_and_unknown_field_do_not_consume(self):
        row, _ = self.mint_transaction()
        bodies = (
            {"transaction_id": "bad-uuid", "code": "x", "state": STATE},
            {"transaction_id": 1, "code": "x", "state": STATE},
            {"transaction_id": str(row.id), "code": 123, "state": STATE},
            {"transaction_id": str(row.id), "code": "x", "state": 123},
            {
                "transaction_id": str(row.id),
                "code": "x",
                "state": STATE,
                "user": {"name": "Ignored is forbidden"},
            },
        )
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.post(reverse("auth-apple-complete"), body, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {"error": "invalid_grant"})
                row.refresh_from_db()
                self.assertIsNone(row.consumed_at)

        malformed = self.client.generic(
            "POST",
            reverse("auth-apple-complete"),
            b"{",
            content_type="application/json",
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.data, {"error": "invalid_grant"})
        row.refresh_from_db()
        self.assertIsNone(row.consumed_at)

        surrogate = self.client.generic(
            "POST",
            reverse("auth-apple-complete"),
            (f'{{"transaction_id":"{row.id}","code":"x","state":"\\ud800"}}').encode("ascii"),
            content_type="application/json",
        )
        self.assertEqual(surrogate.status_code, 400)
        self.assertEqual(surrogate.data, {"error": "invalid_grant"})
        row.refresh_from_db()
        self.assertIsNone(row.consumed_at)

    def test_state_mismatch_does_not_consume(self):
        for state_value in ("wrong-state", "状態"):
            row, _ = self.mint_transaction()
            response, mocked = self.post_complete(
                row,
                self.token_response(),
                state=state_value,
            )
            with self.subTest(state=state_value):
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {"error": "invalid_grant"})
                mocked.assert_not_called()
                row.refresh_from_db()
                self.assertIsNone(row.consumed_at)

    def test_expired_and_consumed_transactions_are_rejected(self):
        for kwargs in ({"ttl": -1}, {"consumed": True}):
            row, _ = self.mint_transaction(**kwargs)
            response, mocked = self.post_complete(row, self.token_response())
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data, {"error": "invalid_grant"})
            mocked.assert_not_called()

    def test_complete_code_and_state_caps_accept_max_and_reject_max_plus_one(self):
        from .apple_services import AppleTransactionRejected

        transaction_id = "00000000-0000-0000-0000-000000000000"
        base = {
            "transaction_id": transaction_id,
            "code": "x",
            "state": "x",
        }
        for field, limit in (("code", 1024), ("state", 128)):
            accepted = {**base, field: "a" * limit}
            with (
                self.subTest(field=field, size=limit),
                patch(
                    "apps.tenants.apple_views.consume_apple_transaction",
                    side_effect=AppleTransactionRejected("test_stop"),
                ) as consume,
            ):
                response = self.client.post(
                    reverse("auth-apple-complete"),
                    accepted,
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                consume.assert_called_once()

            rejected = {**base, field: "a" * (limit + 1)}
            with (
                self.subTest(field=field, size=limit + 1),
                patch("apps.tenants.apple_views.consume_apple_transaction") as consume,
            ):
                response = self.client.post(
                    reverse("auth-apple-complete"),
                    rejected,
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                consume.assert_not_called()


@override_settings(**READY_SETTINGS)
class ApplePhaseBTests(AppleFixtureMixin, TestCase):
    def test_outbound_form_contains_exact_redirect_and_client_secret(self):
        row, nonce = self.mint_transaction()
        response, mocked = self.post_complete(row, self.token_response(nonce=nonce))
        self.assertEqual(response.status_code, 200, response.content)
        call = mocked.call_args
        self.assertEqual(call.args[0], "https://appleid.apple.com/auth/token")
        self.assertEqual(call.kwargs["timeout"], 5)
        self.assertEqual(call.kwargs["data"]["redirect_uri"], REDIRECT_URI)
        self.assertEqual(call.kwargs["data"]["client_id"], SERVICES_ID)
        self.assertEqual(call.kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(call.kwargs["data"]["code"], "apple-code")
        self.assertIn("client_secret", call.kwargs["data"])

    def test_transport_5xx_and_malformed_json_return_503_and_consume(self):
        cases = (
            httpx.ReadTimeout("timeout"),
            FakeResponse(503, {}),
            FakeResponse(200, json_error=ValueError("bad json")),
            FakeResponse(200, ["not", "an", "object"]),
        )
        for apple_result in cases:
            row, _ = self.mint_transaction()
            with self.subTest(result=type(apple_result).__name__):
                side_effect = apple_result if isinstance(apple_result, Exception) else None
                with patch(
                    "apps.tenants.apple_client.httpx.post",
                    side_effect=side_effect,
                    return_value=None if side_effect else apple_result,
                ):
                    response = self.client.post(
                        reverse("auth-apple-complete"),
                        {"transaction_id": str(row.id), "code": "x", "state": STATE},
                        format="json",
                    )
                self.assertEqual(response.status_code, 503, response.content)
                self.assertEqual(response.data, {"error": "apple_unavailable"})
                row.refresh_from_db()
                self.assertIsNotNone(row.consumed_at)

    def test_apple_4xx_returns_invalid_grant(self):
        row, _ = self.mint_transaction()
        response, _ = self.post_complete(row, FakeResponse(400, {"error": "invalid_grant"}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": "invalid_grant"})

    def test_each_required_claim_missing_returns_invalid_grant(self):
        for claim in ("iss", "aud", "exp", "sub", "nonce"):
            row, nonce = self.mint_transaction()
            response, _ = self.post_complete(
                row,
                self.token_response(nonce=nonce, remove_claims=(claim,)),
            )
            with self.subTest(claim=claim):
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.data, {"error": "invalid_grant"})

    def test_bad_signature_issuer_audience_expiry_and_nonce_fail_closed(self):
        cases = (
            {"private_key": _other_rsa_private_key},
            {"token_overrides": {"iss": "https://evil.example"}},
            {"token_overrides": {"aud": "org.hoodunited.ios"}},
            {"token_overrides": {"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())}},
            {"token_overrides": {"exp": []}},
            {"token_overrides": {"nonce": "wrong-nonce"}},
        )
        for token_kwargs in cases:
            row, nonce = self.mint_transaction()
            response, _ = self.post_complete(
                row,
                self.token_response(nonce=nonce, **token_kwargs),
            )
            with self.subTest(token_kwargs=token_kwargs):
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.data, {"error": "invalid_grant"})
        self.assertEqual(AppleRevocationOutbox.objects.count(), 0)

    def test_jwks_transport_failure_returns_503(self):
        self.jwks.initial_error = OSError("network down")
        row, nonce = self.mint_transaction()
        response, _ = self.post_complete(row, self.token_response(nonce=nonce))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data, {"error": "apple_unavailable"})

    def test_unknown_kid_refreshes_once_then_negative_caches(self):
        unknown_token = self.id_token(kid="unknown-kid")
        nonce_hash = __import__("hashlib").sha256(NONCE.encode()).hexdigest()

        for _ in range(2):
            with self.assertRaisesMessage(Exception, "unknown_kid"):
                verify_apple_id_token(unknown_token, nonce_hash)

        self.assertEqual(self.jwks.calls, [False, True])

    def test_failed_unknown_kid_refresh_is_not_negative_cached(self):
        self.jwks.refresh_error = OSError("refresh failed")
        unknown_token = self.id_token(kid="unknown-kid")
        nonce_hash = __import__("hashlib").sha256(NONCE.encode()).hexdigest()

        for _ in range(2):
            with self.assertRaises(AppleUnavailable):
                verify_apple_id_token(unknown_token, nonce_hash)

        self.assertEqual(self.jwks.calls, [False, True, False, True])

    def test_unicode_unknown_kid_fails_closed_without_type_error(self):
        unknown_token = self.id_token(kid="未知-kid")
        nonce_hash = __import__("hashlib").sha256(NONCE.encode()).hexdigest()

        with self.assertRaises(AppleInvalidGrant):
            verify_apple_id_token(unknown_token, nonce_hash)

        self.assertEqual(self.jwks.calls, [False, True])

    def test_client_secret_claims_are_exact_and_five_minutes(self):
        encoded = generate_apple_client_secret()
        claims = jwt.decode(
            encoded,
            _ec_private_key.public_key(),
            algorithms=["ES256"],
            audience=APPLE_ISSUER,
        )
        header = jwt.get_unverified_header(encoded)
        self.assertEqual(header["kid"], KEY_ID)
        self.assertEqual(claims["iss"], TEAM_ID)
        self.assertEqual(claims["sub"], SERVICES_ID)
        self.assertEqual(claims["aud"], APPLE_ISSUER)
        self.assertEqual(claims["exp"] - claims["iat"], 300)


@override_settings(APPLE_SIWA_SERVICES_ID=SERVICES_ID)
class AppleRealJwksClientTests(SimpleTestCase):
    def setUp(self):
        from . import apple_client

        with apple_client._unknown_kids_lock:
            apple_client._unknown_kids.clear()

    def tearDown(self):
        from . import apple_client

        with apple_client._unknown_kids_lock:
            apple_client._unknown_kids.clear()

    def make_token(self):
        return jwt.encode(
            {
                "iss": APPLE_ISSUER,
                "aud": SERVICES_ID,
                "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
                "sub": SUBJECT,
                "nonce": NONCE,
            },
            _rsa_private_key,
            algorithm="RS256",
            headers={"kid": JWT_KID},
        )

    def test_production_jwks_client_has_five_second_timeout(self):
        from . import apple_client

        self.assertEqual(apple_client._jwks_client.timeout, 5)

    def test_malformed_cached_set_is_invalidated_and_next_request_recovers(self):
        from . import apple_client

        client = jwt.PyJWKClient(
            apple_client.APPLE_JWKS_URL,
            cache_keys=True,
            lifespan=300,
            timeout=5,
        )
        responses = iter(
            [
                {"keys": []},
                {"keys": [_rsa_jwk]},
            ]
        )

        def urlopen(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            return io.BytesIO(json.dumps(next(responses)).encode("utf-8"))

        nonce_hash = __import__("hashlib").sha256(NONCE.encode()).hexdigest()
        with (
            patch("apps.tenants.apple_client._jwks_client", client),
            patch(
                "jwt.jwks_client.urllib.request.urlopen",
                side_effect=urlopen,
            ) as mocked_urlopen,
        ):
            with self.assertRaises(AppleUnavailable):
                verify_apple_id_token(self.make_token(), nonce_hash)
            self.assertIsNone(client.jwk_set_cache.get())
            with apple_client._unknown_kids_lock:
                self.assertNotIn(JWT_KID, apple_client._unknown_kids)

            grant = verify_apple_id_token(self.make_token(), nonce_hash)

        self.assertEqual(grant.subject, SUBJECT)
        self.assertEqual(mocked_urlopen.call_count, 2)

    def test_two_thread_rotation_never_negative_caches_successfully_refreshed_kid(self):
        from . import apple_client

        old_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_other_rsa_private_key.public_key()))
        old_jwk.update({"kid": "old-rsa-key", "use": "sig", "alg": "RS256"})
        client = jwt.PyJWKClient(
            apple_client.APPLE_JWKS_URL,
            cache_keys=True,
            lifespan=300,
            timeout=5,
        )
        client.jwk_set_cache.put({"keys": [old_jwk]})

        refresh_started = threading.Event()
        release_refresh = threading.Event()
        start = threading.Barrier(3)
        urlopen_lock = threading.Lock()
        urlopen_calls = 0
        results: list[str] = []
        errors: list[BaseException] = []

        def urlopen(*args, **kwargs):
            nonlocal urlopen_calls
            with urlopen_lock:
                urlopen_calls += 1
                call_number = urlopen_calls
            refresh_started.set()
            if not release_refresh.wait(5):
                raise TimeoutError("test did not release JWKS refresh")
            payload = {"keys": [_rsa_jwk]} if call_number == 1 else {"keys": [old_jwk]}
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        nonce_hash = __import__("hashlib").sha256(NONCE.encode()).hexdigest()

        def verify_worker():
            start.wait()
            try:
                grant = verify_apple_id_token(self.make_token(), nonce_hash)
                results.append(grant.subject)
            except BaseException as exc:
                errors.append(exc)

        with (
            patch("apps.tenants.apple_client._jwks_client", client),
            patch(
                "jwt.jwks_client.urllib.request.urlopen",
                side_effect=urlopen,
            ) as mocked_urlopen,
        ):
            first = threading.Thread(target=verify_worker)
            second = threading.Thread(target=verify_worker)
            first.start()
            second.start()
            start.wait()
            self.assertTrue(refresh_started.wait(5))
            time.sleep(0.05)
            release_refresh.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [SUBJECT, SUBJECT])
        self.assertEqual(mocked_urlopen.call_count, 1)
        with apple_client._unknown_kids_lock:
            self.assertNotIn(JWT_KID, apple_client._unknown_kids)


@override_settings(**READY_SETTINGS)
class AppleCreationPolicyTests(AppleFixtureMixin, TestCase):
    def test_gate_precedes_email_lookup_and_existing_identity_still_signs_in(self):
        row, nonce = self.mint_transaction()
        with (
            self.settings(PREVIEW_ACCESS_KEY="invite-only"),
            patch("apps.tenants.apple_services._email_policy") as email_policy,
        ):
            response, _ = self.post_complete(row, self.token_response(nonce=nonce))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {"error": "signup_gated"})
        email_policy.assert_not_called()
        self.assertEqual(User.objects.count(), 0)

        user = self.make_user(email="existing@example.com")
        self.make_identity(user)
        row, nonce = self.mint_transaction()
        with self.settings(PREVIEW_ACCESS_KEY="invite-only"):
            response, _ = self.post_complete(
                row,
                self.token_response(nonce=nonce, email="existing@example.com"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["created"])

    def test_email_verified_normalisation(self):
        for index, value in enumerate((True, "true")):
            row, nonce = self.mint_transaction()
            email = f"accepted-{index}@example.com"
            response, _ = self.post_complete(
                row,
                self.token_response(
                    nonce=nonce,
                    subject=f"accepted-subject-{index}",
                    email=email,
                    email_verified=value,
                ),
            )
            with self.subTest(value=value):
                self.assertEqual(response.status_code, 200, response.content)
                self.assertTrue(response.data["created"])

        for index, value in enumerate((False, "false", None)):
            row, nonce = self.mint_transaction()
            response, _ = self.post_complete(
                row,
                self.token_response(
                    nonce=nonce,
                    subject=f"rejected-subject-{index}",
                    email=f"rejected-{str(value).lower()}@example.com",
                    email_verified=value,
                ),
            )
            with self.subTest(value=value):
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {"error": "invalid_grant"})

        row, nonce = self.mint_transaction()
        absent, _ = self.post_complete(
            row,
            self.token_response(
                nonce=nonce,
                subject="absent-verification-subject",
                email="absent-verification@example.com",
                remove_claims=("email_verified",),
            ),
        )
        self.assertEqual(absent.status_code, 400)
        self.assertEqual(absent.data, {"error": "invalid_grant"})

    def test_missing_email_or_refresh_token_creates_nothing(self):
        cases = (
            ("missing_email", {"remove_claims": ("email",)}),
            ("malformed_email", {"email": "x@\ud800.com"}),
            ("missing_refresh", {"refresh_token": None}),
            ("malformed_refresh", {"refresh_token": "\ud800"}),
        )
        for name, response_kwargs in cases:
            before = User.objects.count()
            row, nonce = self.mint_transaction()
            response, _ = self.post_complete(
                row,
                self.token_response(nonce=nonce, **response_kwargs),
            )
            with self.subTest(case=name):
                self.assertEqual(response.status_code, 400)
                self.assertEqual(User.objects.count(), before)
                self.assertEqual(ExternalIdentity.objects.count(), 0)

    def test_active_email_requires_link_inactive_and_duplicates_fail_closed(self):
        active = self.make_user(email="match@example.com")
        row, nonce = self.mint_transaction()
        before = User.objects.count()
        response, _ = self.post_complete(
            row,
            self.token_response(nonce=nonce, email=active.email),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"error": "link_required"})
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(AppleRevocationOutbox.objects.count(), 1)

        inactive = self.make_user(email="inactive@example.com", active=False)
        row, nonce = self.mint_transaction()
        response, _ = self.post_complete(
            row,
            self.token_response(nonce=nonce, email=inactive.email),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": "invalid_grant"})

        User.objects.create_user(username="dupe-1", email="dupe@example.com")
        User.objects.create_user(username="dupe-2", email="dupe@example.com")
        row, nonce = self.mint_transaction()
        response, _ = self.post_complete(
            row,
            self.token_response(nonce=nonce, email="dupe@example.com"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": "invalid_grant"})

    def test_create_persists_unusable_password_ciphertext_and_pw_iat(self):
        row, nonce = self.mint_transaction()
        with patch("apps.tenants.services.ensure_tenant_provisioned") as provision:
            response, _ = self.post_complete(
                row,
                self.token_response(
                    nonce=nonce,
                    email="relay@privaterelay.appleid.com",
                    refresh_token="creation-refresh",
                ),
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.data["created"])
        user = User.objects.get(email="relay@privaterelay.appleid.com")
        provision.assert_not_called()
        self.assertFalse(Tenant.objects.filter(user=user).exists())
        self.assertFalse(user.has_usable_password())
        self.assertIsNotNone(user.password_last_changed_at)
        identity = user.external_identities.get(provider="apple")
        self.assertTrue(identity.email_is_relay)
        self.assertEqual(
            decrypt_apple_refresh_token(identity.refresh_token_encrypted),
            "creation-refresh",
        )
        access = AccessToken(response.data["access"])
        self.assertEqual(access["pw_iat"], int(user.password_last_changed_at.timestamp()))

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        me = self.client.get(reverse("auth-me"))
        self.assertEqual(me.status_code, 200, me.content)
        self.assertTrue(me.data["apple_linked"])

    def test_long_email_uses_opaque_digest_username(self):
        long_email = f"{'a' * 60}@{'b' * 60}.{'c' * 60}.com"
        subject = "raw-apple-subject-must-not-leak"
        row, nonce = self.mint_transaction()
        response, _ = self.post_complete(
            row,
            self.token_response(nonce=nonce, subject=subject, email=long_email),
        )
        self.assertEqual(response.status_code, 200, response.content)
        user = User.objects.get(email=long_email)
        self.assertTrue(user.username.startswith("apple_"))
        self.assertNotIn(subject, user.username)
        self.assertLessEqual(len(user.username), 150)

    def test_savepoint_integrity_error_leaves_no_orphan_user(self):
        row, nonce = self.mint_transaction()
        from django.db import IntegrityError

        with patch(
            "apps.tenants.apple_services.ExternalIdentity.objects.create",
            side_effect=IntegrityError("forced identity race"),
        ):
            response, _ = self.post_complete(row, self.token_response(nonce=nonce))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(ExternalIdentity.objects.count(), 0)

    def test_unexpected_phase_c_failure_still_outboxes_verified_refresh(self):
        row, nonce = self.mint_transaction()
        self.client.raise_request_exception = False
        with patch(
            "apps.tenants.apple_views.resolve_apple_auth",
            side_effect=RuntimeError("database failure"),
        ):
            response, _ = self.post_complete(
                row,
                self.token_response(
                    nonce=nonce,
                    subject="unexpected-failure-subject",
                    refresh_token="must-be-revoked",
                ),
            )
        self.assertEqual(response.status_code, 500)
        outbox = AppleRevocationOutbox.objects.get()
        self.assertEqual(
            decrypt_apple_refresh_token(outbox.token_ciphertext),
            "must-be-revoked",
        )

    def test_integrity_loser_rereads_same_subject_winner_as_sign_in(self):
        from django.db import IntegrityError

        from .apple_services import resolve_apple_auth

        winner = self.make_user(email="winner@example.com")
        winner_identity = self.make_identity(
            winner,
            subject="raced-subject",
            refresh_token="winner-refresh",
        )
        grant = AppleGrant(
            subject="raced-subject",
            issuer=APPLE_ISSUER,
            audience=SERVICES_ID,
            email="loser@example.com",
            email_verified=True,
            email_is_relay=False,
            refresh_token="loser-refresh",
        )
        with (
            patch(
                "apps.tenants.apple_services._find_identity_for_update",
                side_effect=[None, winner_identity],
            ),
            patch(
                "apps.tenants.apple_services._create_identity_user",
                side_effect=IntegrityError("concurrent subject winner"),
            ),
        ):
            resolution = resolve_apple_auth(grant)

        self.assertFalse(resolution.created)
        self.assertEqual(resolution.user, winner)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ExternalIdentity.objects.count(), 1)
        winner_identity.refresh_from_db()
        self.assertEqual(
            decrypt_apple_refresh_token(winner_identity.refresh_token_encrypted),
            "loser-refresh",
        )

    def test_password_signup_winner_is_link_required_on_race_recheck(self):
        from django.db import IntegrityError

        from . import apple_services

        password_user = self.make_user(email="race-email@example.com")
        grant = AppleGrant(
            subject="email-race-subject",
            issuer=APPLE_ISSUER,
            audience=SERVICES_ID,
            email=password_user.email,
            email_verified=True,
            email_is_relay=False,
            refresh_token="race-refresh",
        )
        real_email_policy = apple_services._email_policy
        policy_calls = 0

        def email_policy_after_race(email):
            nonlocal policy_calls
            policy_calls += 1
            if policy_calls == 1:
                return None
            return real_email_policy(email)

        with (
            patch(
                "apps.tenants.apple_services._email_policy",
                side_effect=email_policy_after_race,
            ),
            patch(
                "apps.tenants.apple_services._create_identity_user",
                side_effect=IntegrityError("password signup won"),
            ),
            self.assertRaises(apple_services.AppleResolutionRejected) as rejected,
        ):
            apple_services.resolve_apple_auth(grant)

        self.assertEqual(rejected.exception.error, "link_required")
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ExternalIdentity.objects.count(), 0)


@override_settings(**READY_SETTINGS)
class AppleExistingIdentityTests(AppleFixtureMixin, TestCase):
    def test_sign_in_updates_last_login_and_rotates_refresh(self):
        user = self.make_user(email="existing@example.com")
        old_login = timezone.now() - timedelta(days=1)
        identity = self.make_identity(user, last_login_at=old_login)
        row, nonce = self.mint_transaction()

        response, _ = self.post_complete(
            row,
            self.token_response(
                nonce=nonce,
                email="existing@example.com",
                refresh_token="rotated-refresh",
            ),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.data["created"])
        identity.refresh_from_db()
        self.assertGreater(identity.last_login_at, old_login)
        self.assertEqual(
            decrypt_apple_refresh_token(identity.refresh_token_encrypted),
            "rotated-refresh",
        )
        self.assertEqual(User.objects.count(), 1)

    def test_sign_in_without_refresh_keeps_only_revocation_credential_and_backfills_email(self):
        user = self.make_user(email="existing@example.com")
        identity = self.make_identity(user, refresh_token="keep-me", email="")
        original_ciphertext = identity.refresh_token_encrypted
        row, nonce = self.mint_transaction()

        response, _ = self.post_complete(
            row,
            self.token_response(
                nonce=nonce,
                email="relay@privaterelay.appleid.com",
                refresh_token=None,
            ),
        )

        self.assertEqual(response.status_code, 200, response.content)
        identity.refresh_from_db()
        self.assertEqual(identity.refresh_token_encrypted, original_ciphertext)
        self.assertEqual(identity.email_at_auth, "relay@privaterelay.appleid.com")
        self.assertTrue(identity.email_is_relay)

    def test_inactive_identity_user_fails_closed(self):
        user = self.make_user(email="inactive@example.com", active=False)
        self.make_identity(user)
        row, nonce = self.mint_transaction()
        response, _ = self.post_complete(row, self.token_response(nonce=nonce))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": "invalid_grant"})

    def test_me_reports_unlinked_then_linked(self):
        user = self.make_user()
        self.client.force_authenticate(user)
        unlinked = self.client.get(reverse("auth-me"))
        self.assertFalse(unlinked.data["apple_linked"])
        self.make_identity(user)
        linked = self.client.get(reverse("auth-me"))
        self.assertTrue(linked.data["apple_linked"])
        self.assertEqual(
            set(linked.data),
            set(UserSerializer.Meta.fields) | {"tenant"},
        )
        for forbidden in (
            "external_identities",
            "provider",
            "subject",
            "issuer",
            "audience",
            "refresh_token_encrypted",
            "token_ciphertext",
        ):
            self.assertNotIn(forbidden, linked.data)


@override_settings(**READY_SETTINGS)
class AppleLinkTests(AppleFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.password = "CorrectHorse123!"
        self.user = self.make_user(password=self.password)
        self.client.force_authenticate(self.user)

    def post_link(
        self,
        row,
        response,
        *,
        password=None,
        state=STATE,
    ):
        with patch("apps.tenants.apple_client.httpx.post", return_value=response):
            return self.client.post(
                reverse("auth-apple-link"),
                {
                    "transaction_id": str(row.id),
                    "code": "apple-code",
                    "state": state,
                    "current_password": self.password if password is None else password,
                },
                format="json",
            )

    def test_wrong_missing_and_unusable_password_fail_without_consuming(self):
        row, nonce = self.mint_transaction()
        wrong = self.post_link(row, self.token_response(nonce=nonce), password="wrong")
        self.assertEqual(wrong.status_code, 400)
        row.refresh_from_db()
        self.assertIsNone(row.consumed_at)

        missing_row, _ = self.mint_transaction()
        missing = self.client.post(
            reverse("auth-apple-link"),
            {
                "transaction_id": str(missing_row.id),
                "code": "x",
                "state": STATE,
            },
            format="json",
        )
        self.assertEqual(missing.status_code, 400)
        missing_row.refresh_from_db()
        self.assertIsNone(missing_row.consumed_at)

        self.user.set_unusable_password()
        self.user.save(update_fields=["password", "password_last_changed_at"])
        unusable_row, nonce = self.mint_transaction()
        unusable = self.post_link(
            unusable_row,
            self.token_response(nonce=nonce),
            password=self.password,
        )
        self.assertEqual(unusable.status_code, 400)
        unusable_row.refresh_from_db()
        self.assertIsNone(unusable_row.consumed_at)

    def test_happy_link_and_me_state(self):
        row, nonce = self.mint_transaction()
        response = self.post_link(
            row,
            self.token_response(nonce=nonce, refresh_token="linked-refresh"),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data, {"linked": True})
        identity = self.user.external_identities.get(provider="apple")
        self.assertEqual(
            decrypt_apple_refresh_token(identity.refresh_token_encrypted),
            "linked-refresh",
        )
        me = self.client.get(reverse("auth-me"))
        self.assertTrue(me.data["apple_linked"])

    def test_link_invalidates_tenant_backed_me_cache(self):
        Tenant.objects.create(user=self.user)
        before = self.client.get(reverse("auth-me"))
        self.assertEqual(before.status_code, 200)
        self.assertFalse(before.data["apple_linked"])

        row, nonce = self.mint_transaction()
        linked = self.post_link(
            row,
            self.token_response(nonce=nonce, refresh_token="cache-refresh"),
        )
        self.assertEqual(linked.status_code, 200, linked.content)

        after = self.client.get(reverse("auth-me"))
        self.assertEqual(after.status_code, 200)
        self.assertTrue(after.data["apple_linked"])
        self.assertNotEqual(after.get("X-Cache"), "HIT")

    def test_idempotent_same_subject_rotates_token(self):
        identity = self.make_identity(self.user, refresh_token="old")
        row, nonce = self.mint_transaction()
        response = self.post_link(
            row,
            self.token_response(nonce=nonce, refresh_token="new"),
        )
        self.assertEqual(response.status_code, 200)
        identity.refresh_from_db()
        self.assertEqual(decrypt_apple_refresh_token(identity.refresh_token_encrypted), "new")

    def test_different_subject_on_user_is_already_linked_and_revoked(self):
        self.make_identity(self.user, subject="first-subject")
        row, nonce = self.mint_transaction()
        response = self.post_link(
            row,
            self.token_response(
                nonce=nonce,
                subject="second-subject",
                refresh_token="unpersisted",
            ),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"error": "already_linked"})
        outbox = AppleRevocationOutbox.objects.get()
        self.assertEqual(decrypt_apple_refresh_token(outbox.token_ciphertext), "unpersisted")

    def test_subject_owned_elsewhere_is_in_use_and_revoked(self):
        owner = self.make_user(email="owner@example.com")
        self.make_identity(owner, subject="owned-subject")
        row, nonce = self.mint_transaction()
        response = self.post_link(
            row,
            self.token_response(
                nonce=nonce,
                subject="owned-subject",
                refresh_token="fresh-unpersisted",
            ),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"error": "apple_id_in_use"})
        outbox = AppleRevocationOutbox.objects.get()
        self.assertEqual(
            decrypt_apple_refresh_token(outbox.token_ciphertext),
            "fresh-unpersisted",
        )

    def test_link_requires_refresh_token(self):
        row, nonce = self.mint_transaction()
        response = self.post_link(
            row,
            self.token_response(nonce=nonce, refresh_token=None),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": "invalid_grant"})
        self.assertFalse(ExternalIdentity.objects.filter(user=self.user).exists())

    def test_unauthenticated_is_default_401(self):
        self.client.force_authenticate(user=None)
        row, _ = self.mint_transaction()
        response = self.client.post(
            reverse("auth-apple-link"),
            {
                "transaction_id": str(row.id),
                "code": "x",
                "state": STATE,
                "current_password": self.password,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("detail", response.data)

    def test_current_password_cap_accepts_max_and_rejects_max_plus_one(self):
        transaction_id = "00000000-0000-0000-0000-000000000000"
        base = {
            "transaction_id": transaction_id,
            "code": "x",
            "state": "x",
        }
        with patch.object(User, "check_password", return_value=False) as check_password:
            accepted = self.client.post(
                reverse("auth-apple-link"),
                {**base, "current_password": "a" * 128},
                format="json",
            )
        self.assertEqual(accepted.status_code, 400)
        check_password.assert_called_once()

        with patch.object(User, "check_password", return_value=False) as check_password:
            rejected = self.client.post(
                reverse("auth-apple-link"),
                {**base, "current_password": "a" * 129},
                format="json",
            )
        self.assertEqual(rejected.status_code, 400)
        check_password.assert_not_called()


@override_settings(**READY_SETTINGS)
class AppleThrottleAndLoggingTests(AppleFixtureMixin, TestCase):
    def test_throttle_rates_and_default_429_shape(self):
        self.assertEqual(AppleBeginMinuteThrottle.rate, "30/minute")
        self.assertEqual(AppleCompleteMinuteThrottle.rate, "10/minute")
        self.assertEqual(AppleLinkMinuteThrottle.rate, "10/minute")
        with patch.object(AppleBeginMinuteThrottle, "rate", "1/minute"):
            first = self.client.post(
                reverse("auth-apple-begin"),
                {},
                format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.8, 10.0.0.1",
            )
            second = self.client.post(
                reverse("auth-apple-begin"),
                {},
                format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.8, 10.0.0.2",
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("detail", second.data)

    def test_complete_and_link_throttles_keep_default_429_shape(self):
        with patch.object(AppleCompleteMinuteThrottle, "rate", "1/minute"):
            complete_first = self.client.post(
                reverse("auth-apple-complete"),
                {},
                format="json",
            )
            complete_second = self.client.post(
                reverse("auth-apple-complete"),
                {},
                format="json",
            )
        self.assertEqual(complete_first.status_code, 400)
        self.assertEqual(complete_second.status_code, 429)
        self.assertIn("detail", complete_second.data)

        cache.clear()
        user = self.make_user()
        self.client.force_authenticate(user)
        row, _ = self.mint_transaction()
        body = {
            "transaction_id": str(row.id),
            "code": "x",
            "state": STATE,
            "current_password": "wrong-password",
        }
        with patch.object(AppleLinkMinuteThrottle, "rate", "1/minute"):
            link_first = self.client.post(reverse("auth-apple-link"), body, format="json")
            link_second = self.client.post(reverse("auth-apple-link"), body, format="json")
        self.assertEqual(link_first.status_code, 400)
        self.assertEqual(link_second.status_code, 429)
        self.assertIn("detail", link_second.data)

    def test_secret_material_never_appears_in_apple_logs(self):
        row, nonce = self.mint_transaction()
        raw_code = "super-secret-code"
        raw_refresh = "super-secret-refresh"
        raw_token = self.id_token(nonce=nonce)
        with self.assertLogs("apps.tenants", level="INFO") as captured:
            response, mocked = self.post_complete(
                row,
                FakeResponse(
                    200,
                    {
                        "id_token": raw_token,
                        "refresh_token": raw_refresh,
                    },
                ),
                code=raw_code,
            )
        self.assertEqual(response.status_code, 200)
        logs = "\n".join(captured.output)
        client_secret = mocked.call_args.kwargs["data"]["client_secret"]
        for secret in (raw_code, raw_refresh, raw_token, nonce, client_secret):
            self.assertNotIn(secret, logs)
        self.assertIn("auth.apple.complete.success", logs)

    def test_link_secret_material_never_appears_in_apple_logs(self):
        raw_password = "link-step-up-secret"
        user = self.make_user(
            email="link-log@example.com",
            password=raw_password,
        )
        self.client.force_authenticate(user)
        row, nonce = self.mint_transaction()
        raw_code = "link-super-secret-code"
        raw_refresh = "link-super-secret-refresh"
        raw_token = self.id_token(
            nonce=nonce,
            subject="link-log-subject",
            email="link-log@example.com",
        )

        with (
            self.assertLogs("apps.tenants", level="INFO") as captured,
            patch(
                "apps.tenants.apple_client.httpx.post",
                return_value=FakeResponse(
                    200,
                    {
                        "id_token": raw_token,
                        "refresh_token": raw_refresh,
                    },
                ),
            ) as mocked,
        ):
            response = self.client.post(
                reverse("auth-apple-link"),
                {
                    "transaction_id": str(row.id),
                    "code": raw_code,
                    "state": STATE,
                    "current_password": raw_password,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        logs = "\n".join(captured.output)
        client_secret = mocked.call_args.kwargs["data"]["client_secret"]
        for secret in (
            raw_password,
            raw_code,
            raw_refresh,
            raw_token,
            nonce,
            client_secret,
        ):
            self.assertNotIn(secret, logs)
        self.assertIn("auth.apple.link.success", logs)

    def test_jwks_failure_logs_no_secret_material(self):
        row, nonce = self.mint_transaction()
        raw_code = "jwks-failure-secret-code"
        raw_refresh = "jwks-failure-secret-refresh"
        raw_token = self.id_token(nonce=nonce)
        self.jwks.initial_error = OSError("network down")

        with (
            self.assertLogs("apps.tenants", level="WARNING") as captured,
            patch(
                "apps.tenants.apple_client.httpx.post",
                return_value=FakeResponse(
                    200,
                    {
                        "id_token": raw_token,
                        "refresh_token": raw_refresh,
                    },
                ),
            ) as mocked,
        ):
            response = self.client.post(
                reverse("auth-apple-complete"),
                {
                    "transaction_id": str(row.id),
                    "code": raw_code,
                    "state": STATE,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        logs = "\n".join(captured.output)
        client_secret = mocked.call_args.kwargs["data"]["client_secret"]
        for secret in (raw_code, raw_refresh, raw_token, nonce, client_secret):
            self.assertNotIn(secret, logs)
        self.assertIn("auth.apple.complete.unavailable", logs)

    def test_normal_rejections_log_info_and_transport_failures_warning(self):
        row, _ = self.mint_transaction()
        with self.assertLogs("apps.tenants.apple_views", level="INFO") as normal_logs:
            normal, _ = self.post_complete(
                row,
                self.token_response(),
                state="wrong-state",
            )
        self.assertEqual(normal.status_code, 400)
        self.assertTrue(all(line.startswith("INFO:") for line in normal_logs.output))

        row, _ = self.mint_transaction()
        with (
            self.assertLogs("apps.tenants.apple_views", level="WARNING") as failure_logs,
            patch(
                "apps.tenants.apple_client.httpx.post",
                side_effect=httpx.ReadTimeout("timeout"),
            ),
        ):
            unavailable = self.client.post(
                reverse("auth-apple-complete"),
                {"transaction_id": str(row.id), "code": "x", "state": STATE},
                format="json",
            )
        self.assertEqual(unavailable.status_code, 503)
        self.assertTrue(all(line.startswith("WARNING:") for line in failure_logs.output))


@override_settings(**READY_SETTINGS)
class AppleAtomicAndRaceTests(AppleFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def test_phase_b_runs_after_consume_commit(self):
        user = self.make_user(email="existing@example.com")
        self.make_identity(user)
        row, _ = self.mint_transaction()
        observed = []

        def phase_b(*args, **kwargs):
            observed.append(connection.in_atomic_block)
            row.refresh_from_db()
            self.assertIsNotNone(row.consumed_at)
            return AppleGrant(
                subject=SUBJECT,
                issuer=APPLE_ISSUER,
                audience=SERVICES_ID,
                email="existing@example.com",
                email_verified=True,
                email_is_relay=False,
                refresh_token=None,
            )

        with patch("apps.tenants.apple_views.exchange_apple_code", side_effect=phase_b):
            response = self.client.post(
                reverse("auth-apple-complete"),
                {"transaction_id": str(row.id), "code": "x", "state": STATE},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(observed, [False])

    def test_link_phase_b_runs_after_consume_commit(self):
        password = "LinkAtomicPassword123!"
        user = self.make_user(
            email="link-atomic@example.com",
            password=password,
        )
        self.client.force_authenticate(user)
        row, _ = self.mint_transaction()
        observed = []

        def phase_b(*args, **kwargs):
            observed.append(connection.in_atomic_block)
            row.refresh_from_db()
            self.assertIsNotNone(row.consumed_at)
            return AppleGrant(
                subject="link-atomic-subject",
                issuer=APPLE_ISSUER,
                audience=SERVICES_ID,
                email="link-atomic@example.com",
                email_verified=True,
                email_is_relay=False,
                refresh_token="link-atomic-refresh",
            )

        with patch("apps.tenants.apple_views.exchange_apple_code", side_effect=phase_b):
            response = self.client.post(
                reverse("auth-apple-link"),
                {
                    "transaction_id": str(row.id),
                    "code": "x",
                    "state": STATE,
                    "current_password": password,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(observed, [False])

    def test_real_exchange_http_mock_runs_outside_atomic(self):
        user = self.make_user(email="real-phase-b@example.com")
        self.make_identity(user, subject="real-phase-b-subject")
        row, nonce = self.mint_transaction()
        observed = []

        def apple_post(*args, **kwargs):
            observed.append(connection.in_atomic_block)
            return self.token_response(
                nonce=nonce,
                subject="real-phase-b-subject",
                email="real-phase-b@example.com",
                refresh_token=None,
            )

        with patch("apps.tenants.apple_client.httpx.post", side_effect=apple_post):
            response = self.client.post(
                reverse("auth-apple-complete"),
                {"transaction_id": str(row.id), "code": "x", "state": STATE},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(observed, [False])

    def test_double_complete_has_exactly_one_winner(self):
        user = self.make_user(email="existing@example.com")
        self.make_identity(user)
        row, _ = self.mint_transaction()
        first_in_phase_b = threading.Event()
        release_first = threading.Event()
        statuses: list[int] = []
        errors: list[BaseException] = []

        def phase_b(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            first_in_phase_b.set()
            release_first.wait(5)
            return AppleGrant(
                subject=SUBJECT,
                issuer=APPLE_ISSUER,
                audience=SERVICES_ID,
                email="existing@example.com",
                email_verified=True,
                email_is_relay=False,
                refresh_token=None,
            )

        def post_once():
            client = APIClient()
            try:
                result = client.post(
                    reverse("auth-apple-complete"),
                    {"transaction_id": str(row.id), "code": "x", "state": STATE},
                    format="json",
                )
                statuses.append(result.status_code)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        with patch("apps.tenants.apple_views.exchange_apple_code", side_effect=phase_b):
            first = threading.Thread(target=post_once)
            first.start()
            self.assertTrue(first_in_phase_b.wait(5))
            second = threading.Thread(target=post_once)
            second.start()
            second.join(5)
            release_first.set()
            first.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertCountEqual(statuses, [200, 400])

    def test_concurrent_same_subject_creation_has_one_identity_and_one_sign_in_loser(self):
        from . import apple_services

        loser_paused = threading.Event()
        winner_done = threading.Event()
        results: list[tuple[str, bool]] = []
        errors: list[BaseException] = []
        real_find = apple_services._find_identity_for_update
        loser_first_find = True

        def orchestrated_find(subject):
            nonlocal loser_first_find
            if threading.current_thread().name == "apple-subject-loser" and loser_first_find:
                loser_first_find = False
                loser_paused.set()
                if not winner_done.wait(5):
                    raise TimeoutError("winner did not finish")
                # Model the legitimate READ-COMMITTED race: this transaction
                # observed no identity, while the other connection commits one
                # before its insert reaches the unique constraint.
                return None
            return real_find(subject)

        def grant(email, refresh):
            return AppleGrant(
                subject="concurrent-shared-subject",
                issuer=APPLE_ISSUER,
                audience=SERVICES_ID,
                email=email,
                email_verified=True,
                email_is_relay=False,
                refresh_token=refresh,
            )

        def worker(label, apple_grant):
            connections.close_all()
            try:
                resolution = apple_services.resolve_apple_auth(apple_grant)
                results.append((label, resolution.created))
            except BaseException as exc:
                errors.append(exc)
            finally:
                if label == "winner":
                    winner_done.set()
                connections.close_all()

        with patch(
            "apps.tenants.apple_services._find_identity_for_update",
            side_effect=orchestrated_find,
        ):
            loser = threading.Thread(
                target=worker,
                name="apple-subject-loser",
                args=("loser", grant("loser-race@example.com", "loser-refresh")),
            )
            loser.start()
            self.assertTrue(loser_paused.wait(5))
            winner = threading.Thread(
                target=worker,
                name="apple-subject-winner",
                args=("winner", grant("winner-race@example.com", "winner-refresh")),
            )
            winner.start()
            winner.join(5)
            loser.join(5)

        self.assertFalse(winner.is_alive())
        self.assertFalse(loser.is_alive())
        self.assertEqual(errors, [])
        self.assertCountEqual(results, [("winner", True), ("loser", False)])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ExternalIdentity.objects.count(), 1)

    def test_concurrent_password_signup_claim_becomes_link_required(self):
        from . import apple_services

        loser_at_email_policy = threading.Event()
        password_winner_done = threading.Event()
        errors: list[BaseException] = []
        rejected: list[str] = []
        first_policy = True
        real_email_policy = apple_services._email_policy

        def orchestrated_policy(email):
            nonlocal first_policy
            if threading.current_thread().name == "apple-email-loser" and first_policy:
                first_policy = False
                loser_at_email_policy.set()
                if not password_winner_done.wait(5):
                    raise TimeoutError("password signup did not finish")
                return None
            return real_email_policy(email)

        def apple_worker():
            connections.close_all()
            grant = AppleGrant(
                subject="concurrent-email-subject",
                issuer=APPLE_ISSUER,
                audience=SERVICES_ID,
                email="claimed-midflight@example.com",
                email_verified=True,
                email_is_relay=False,
                refresh_token="email-race-refresh",
            )
            try:
                apple_services.resolve_apple_auth(grant)
            except apple_services.AppleResolutionRejected as exc:
                rejected.append(exc.error)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        def password_worker():
            connections.close_all()
            try:
                User.objects.create_user(
                    username="claimed-midflight@example.com",
                    email="claimed-midflight@example.com",
                    password="PasswordWinner123!",
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                password_winner_done.set()
                connections.close_all()

        with (
            patch(
                "apps.tenants.apple_services._find_identity_for_update",
                return_value=None,
            ),
            patch(
                "apps.tenants.apple_services._email_policy",
                side_effect=orchestrated_policy,
            ),
        ):
            loser = threading.Thread(target=apple_worker, name="apple-email-loser")
            loser.start()
            self.assertTrue(loser_at_email_policy.wait(5))
            winner = threading.Thread(target=password_worker, name="password-email-winner")
            winner.start()
            winner.join(5)
            loser.join(5)

        self.assertFalse(winner.is_alive())
        self.assertFalse(loser.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(rejected, ["link_required"])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ExternalIdentity.objects.count(), 0)
