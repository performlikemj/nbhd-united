"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

import jwt
from django.test import SimpleTestCase


class PyJwtSdkContractTest(SimpleTestCase):
    def test_encode_decode_round_trip_uses_our_kwargs(self):
        secret = "offline-contract-test-secret-key-material"
        token = jwt.encode(
            {"sub": "subject"},
            secret,
            algorithm="HS256",
            headers={"kid": "key-id", "typ": "JWT"},
        )
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["sub"], "verify_aud": False, "verify_iss": False},
        )

        self.assertEqual(claims["sub"], "subject")
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "key-id")

    def test_jwk_client_constructor_and_methods_accept_our_shapes(self):
        client = jwt.PyJWKClient(
            "https://example.test/keys",
            cache_keys=True,
            lifespan=300,
            timeout=5,
        )

        inspect.signature(client.get_signing_keys).bind(refresh=True)
        self.assertTrue(callable(client.jwk_set_cache.put))

    def test_caught_exception_paths_exist(self):
        self.assertTrue(issubclass(jwt.PyJWTError, Exception))
        self.assertTrue(issubclass(jwt.PyJWKClientConnectionError, jwt.PyJWTError))
