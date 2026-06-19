"""Tests for the web→app PKCE handoff (/api/v1/auth/authorize/ + /exchange/).

Covers the directive's contract: a single-use, short-TTL PKCE code is minted
for an authenticated web user and swapped — with the on-device verifier — for a
SimpleJWT pair. EVERY exchange failure must collapse to the identical
``400 {"error": "invalid_grant"}`` (no oracle), and the minted token MUST carry
``pw_iat`` so it survives ``JWTAuthenticationWithRLS`` for password-rotated
users.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from .oauth_models import (
    OAuthAuthorizationCode,
    generate_authorization_code,
    hash_authorization_code,
    pkce_s256,
)
from .serializers import EmailTokenObtainPairSerializer

User = get_user_model()

REDIRECT_URI = "nbhd://auth/callback"
# A realistic on-device verifier (43-char base64url) and its S256 challenge.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = pkce_s256(VERIFIER)


def _mint_code(user, *, challenge=CHALLENGE, redirect_uri=REDIRECT_URI, ttl=300):
    """Create an OAuthAuthorizationCode row directly; return the raw code."""
    raw, code_hash = generate_authorization_code()
    OAuthAuthorizationCode.objects.create(
        user=user,
        code_hash=code_hash,
        code_challenge=challenge,
        redirect_uri=redirect_uri,
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )
    return raw


class ExchangeViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="web@example.com", email="web@example.com", password="OldPass123!"
        )
        self.url = reverse("auth-exchange")

    def _post(self, **body):
        return self.client.post(self.url, body, format="json")

    # 1. Happy path ---------------------------------------------------------
    def test_happy_path_returns_token_pair_with_pw_iat(self):
        code = _mint_code(self.user)
        resp = self._post(code=code, code_verifier=VERIFIER, redirect_uri=REDIRECT_URI)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)

        access = AccessToken(resp.data["access"])
        self.assertIn("pw_iat", access, "minted access token must carry pw_iat")

        # The token must actually authenticate against an IsAuthenticated
        # endpoint — this is the RefreshToken.for_user regression guard.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['access']}")
        me = self.client.get(reverse("auth-me"))
        self.assertEqual(me.status_code, 200, me.content)

    def test_exchanged_token_authenticates_after_password_rotation(self):
        """The CRITICAL guard: a user who has rotated their password has a
        non-null ``password_last_changed_at``. A token minted via
        ``RefreshToken.for_user`` (no pw_iat) would be rejected by
        ``JWTAuthenticationWithRLS``; the serializer's ``get_token`` carries the
        claim and is accepted."""
        self.user.set_password("RotatedPass456!")
        self.user.save(update_fields=["password", "password_last_changed_at"])
        self.assertIsNotNone(self.user.password_last_changed_at)

        code = _mint_code(self.user)
        resp = self._post(code=code, code_verifier=VERIFIER, redirect_uri=REDIRECT_URI)
        self.assertEqual(resp.status_code, 200, resp.content)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['access']}")
        me = self.client.get(reverse("auth-me"))
        self.assertEqual(
            me.status_code,
            200,
            "exchanged token must authenticate for a password-rotated user "
            "(proves get_token, not RefreshToken.for_user)",
        )

    # 2. Expired code -------------------------------------------------------
    def test_expired_code_is_invalid_grant(self):
        code = _mint_code(self.user, ttl=-10)
        resp = self._post(code=code, code_verifier=VERIFIER, redirect_uri=REDIRECT_URI)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_grant"})

    # 3. Reused code --------------------------------------------------------
    def test_reused_code_is_invalid_grant_and_consumed_once(self):
        code = _mint_code(self.user)
        first = self._post(code=code, code_verifier=VERIFIER, redirect_uri=REDIRECT_URI)
        self.assertEqual(first.status_code, 200, first.content)

        second = self._post(code=code, code_verifier=VERIFIER, redirect_uri=REDIRECT_URI)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data, {"error": "invalid_grant"})

        row = OAuthAuthorizationCode.objects.get(code_hash=hash_authorization_code(code))
        self.assertIsNotNone(row.consumed_at)

    # 4. Bad PKCE -----------------------------------------------------------
    def test_wrong_verifier_is_invalid_grant(self):
        code = _mint_code(self.user)
        resp = self._post(code=code, code_verifier="not-the-right-verifier", redirect_uri=REDIRECT_URI)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_grant"})

    def test_non_ascii_verifier_is_invalid_grant_not_500(self):
        """A non-ASCII verifier can't hash to a base64url challenge — it must
        be a generic 400, never an unhandled 500 (which would be an oracle)."""
        code = _mint_code(self.user)
        resp = self._post(code=code, code_verifier="naïve-vérifier-✓", redirect_uri=REDIRECT_URI)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_grant"})

    # 5. Wrong redirect_uri -------------------------------------------------
    def test_wrong_redirect_uri_is_invalid_grant(self):
        code = _mint_code(self.user)
        resp = self._post(code=code, code_verifier=VERIFIER, redirect_uri="nbhd://evil/callback")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_grant"})

    # 6. Missing fields -----------------------------------------------------
    def test_missing_fields_are_invalid_grant(self):
        code = _mint_code(self.user)
        for body in (
            {"code": "", "code_verifier": VERIFIER, "redirect_uri": REDIRECT_URI},
            {"code": code, "code_verifier": "", "redirect_uri": REDIRECT_URI},
            {"code": code, "code_verifier": VERIFIER, "redirect_uri": ""},
            {},
        ):
            resp = self.client.post(self.url, body, format="json")
            self.assertEqual(resp.status_code, 400, body)
            self.assertEqual(resp.data, {"error": "invalid_grant"}, body)

    def test_unknown_code_is_invalid_grant(self):
        resp = self._post(code="totally-bogus", code_verifier=VERIFIER, redirect_uri=REDIRECT_URI)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_grant"})

    # 9. Inactive user ------------------------------------------------------
    def test_inactive_user_is_invalid_grant(self):
        code = _mint_code(self.user)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        resp = self._post(code=code, code_verifier=VERIFIER, redirect_uri=REDIRECT_URI)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_grant"})

    def test_exchange_allows_anonymous(self):
        """The exchange endpoint is AllowAny — a missing/invalid code returns
        the generic 400, never a 401 (which would prove the route is authed)."""
        resp = self._post(code="x", code_verifier="y", redirect_uri=REDIRECT_URI)
        self.assertEqual(resp.status_code, 400)


class AuthorizeBeginViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="spa@example.com", email="spa@example.com", password="Pass1234!")
        self.url = reverse("auth-authorize")
        self.exchange_url = reverse("auth-exchange")

    def _auth(self):
        token = EmailTokenObtainPairSerializer.get_token(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    # 7. Begin validation ---------------------------------------------------
    def test_unauthenticated_authorize_is_401(self):
        resp = self.client.post(
            self.url,
            {
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_wrong_method_is_invalid_request(self):
        self._auth()
        resp = self.client.post(
            self.url,
            {
                "code_challenge": CHALLENGE,
                "code_challenge_method": "plain",
                "redirect_uri": REDIRECT_URI,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_request"})

    def test_disallowed_redirect_uri_is_invalid_request(self):
        self._auth()
        resp = self.client.post(
            self.url,
            {
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "redirect_uri": "https://evil.example/callback",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_request"})

    def test_missing_code_challenge_is_invalid_request(self):
        self._auth()
        resp = self.client.post(
            self.url,
            {"code_challenge_method": "S256", "redirect_uri": REDIRECT_URI},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_request"})

    def test_non_b64url_code_challenge_is_invalid_request(self):
        self._auth()
        resp = self.client.post(
            self.url,
            {
                "code_challenge": "has spaces & symbols!",
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"error": "invalid_request"})

    # Full round-trip: begin → exchange ------------------------------------
    def test_begin_then_exchange_round_trip(self):
        self._auth()
        begin = self.client.post(
            self.url,
            {
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
                "state": "the-app-nonce",
                "client": "ios",
            },
            format="json",
        )
        self.assertEqual(begin.status_code, 200, begin.content)
        self.assertIn("code", begin.data)
        # 8. State passthrough: the begin endpoint does NOT mint/echo state —
        # it is the app's nonce, carried through by the SPA into the redirect.
        self.assertNotIn("state", begin.data)

        code = begin.data["code"]
        # Exchange is AllowAny — drop the Bearer header to simulate the app.
        anon = APIClient()
        exch = anon.post(
            self.exchange_url,
            {"code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT_URI},
            format="json",
        )
        self.assertEqual(exch.status_code, 200, exch.content)
        self.assertIn("access", exch.data)
        self.assertIn("refresh", exch.data)

    def test_minted_code_is_bound_to_minting_user(self):
        """The exchanged token belongs to the user who minted the code."""
        self._auth()
        begin = self.client.post(
            self.url,
            {
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
            },
            format="json",
        )
        code = begin.data["code"]
        anon = APIClient()
        exch = anon.post(
            self.exchange_url,
            {"code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT_URI},
            format="json",
        )
        access = AccessToken(exch.data["access"])
        self.assertEqual(str(access["user_id"]), str(self.user.id))


# NOTE: the single-use guarantee under true concurrency rests on the
# ``select_for_update`` row lock in ExchangeView (two simultaneous redeems →
# one waits, sees ``consumed_at`` set, and returns invalid_grant). A threaded
# test of that race was dropped: threaded DB tests deadlock the flush of later
# TransactionTestCases in the full suite. The single-use logic itself is
# covered deterministically by
# ``test_reused_code_is_invalid_grant_and_consumed_once`` above.
