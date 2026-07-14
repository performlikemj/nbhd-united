"""Tests for the service-notice unsubscribe category (channel-sunset PR).

The marketing opt-out (``email_opt_out``, one-click unsubscribe view,
RFC 8058 headers) predates this; these tests pin the SERVICE sibling:

- category-carrying tokens: ``service`` sets ``service_email_opt_out`` and
  never the marketing flag; legacy category-less tokens keep working as
  marketing (backward compatibility — links in already-sent emails).
- marketing tokens are byte-identical to the pre-category format, so the
  promo path regresses nowhere (its own module ``test_comeback_campaign``
  also still runs unchanged).
"""

from __future__ import annotations

import secrets

from django.test import TestCase

from apps.tenants.models import Tenant, User
from apps.tenants.unsubscribe_signing import (
    CATEGORY_MARKETING,
    CATEGORY_SERVICE,
    make_unsubscribe_token,
    parse_unsubscribe_token,
    verify_unsubscribe_token,
)


def _user(email="svc@e.com") -> User:
    user = User.objects.create_user(username=f"{email}-{secrets.token_hex(3)}", email=email)
    Tenant.objects.create(user=user)
    return user


class ServiceTokenSigningTest(TestCase):
    def test_service_token_roundtrip(self):
        token = make_unsubscribe_token("user-abc", category=CATEGORY_SERVICE)
        self.assertEqual(parse_unsubscribe_token(token), ("user-abc", CATEGORY_SERVICE))
        # The back-compat verifier still yields the user id.
        self.assertEqual(verify_unsubscribe_token(token), "user-abc")

    def test_marketing_token_is_legacy_format(self):
        # Marketing tokens must stay byte-identical to the pre-category mint
        # so every previously sent unsubscribe link keeps verifying — and the
        # promo campaign's sends never change shape.
        from django.core.signing import Signer

        legacy = Signer(salt="nbhd.unsub.v1").sign("user-abc")
        self.assertEqual(make_unsubscribe_token("user-abc"), legacy)
        self.assertEqual(parse_unsubscribe_token(legacy), ("user-abc", CATEGORY_MARKETING))

    def test_unknown_category_rejected_at_mint(self):
        with self.assertRaises(ValueError):
            make_unsubscribe_token("user-abc", category="everything")

    def test_tampered_and_garbage_tokens_fail(self):
        token = make_unsubscribe_token("user-abc", category=CATEGORY_SERVICE)
        self.assertIsNone(parse_unsubscribe_token(token + "x"))
        self.assertIsNone(parse_unsubscribe_token("not-a-token"))


class ServiceUnsubscribeViewTest(TestCase):
    def _url(self, token: str) -> str:
        return f"/api/v1/tenants/unsubscribe/{token}/"

    def test_service_token_sets_service_flag_not_marketing(self):
        user = _user()
        token = make_unsubscribe_token(user.id, category=CATEGORY_SERVICE)

        resp = self.client.post(self._url(token))
        self.assertEqual(resp.status_code, 200)

        user.refresh_from_db()
        self.assertTrue(user.service_email_opt_out)
        self.assertIsNotNone(user.service_email_opt_out_at)
        # The marketing flag is untouched — independent categories.
        self.assertFalse(user.email_opt_out)
        self.assertIsNone(user.email_opt_out_at)

    def test_legacy_token_still_sets_marketing_flag(self):
        # Backward compatibility: a token minted before categories existed
        # (bare user id payload) must keep opting the user out of MARKETING.
        user = _user("legacy@e.com")
        token = make_unsubscribe_token(user.id)  # legacy/default format

        resp = self.client.post(self._url(token))
        self.assertEqual(resp.status_code, 200)

        user.refresh_from_db()
        self.assertTrue(user.email_opt_out)
        self.assertFalse(user.service_email_opt_out)

    def test_get_does_not_mutate_service_flag(self):
        # Scanner-safety parity with the marketing flow: GET renders the
        # confirmation page without opting out.
        user = _user("scanner@e.com")
        token = make_unsubscribe_token(user.id, category=CATEGORY_SERVICE)

        resp = self.client.get(self._url(token))
        self.assertEqual(resp.status_code, 200)

        user.refresh_from_db()
        self.assertFalse(user.service_email_opt_out)

    def test_service_post_idempotent(self):
        user = _user("twice@e.com")
        token = make_unsubscribe_token(user.id, category=CATEGORY_SERVICE)

        self.client.post(self._url(token))
        user.refresh_from_db()
        first_at = user.service_email_opt_out_at

        resp = self.client.post(self._url(token))
        self.assertEqual(resp.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.service_email_opt_out_at, first_at)

    def test_service_page_copy_names_service_notices(self):
        user = _user("copy@e.com")
        token = make_unsubscribe_token(user.id, category=CATEGORY_SERVICE)

        get_page = self.client.get(self._url(token)).content.decode()
        self.assertIn("service and operational notice emails", get_page)

        post_page = self.client.post(self._url(token)).content.decode()
        self.assertIn("service or operational notice emails", post_page)
