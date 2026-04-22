"""Tests for Personal Access Token auth and management."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User
from .pat_models import PersonalAccessToken, generate_pat, hash_token


class PATModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="pat_user@example.com",
            email="pat_user@example.com",
            password="testpass123",
        )

    def test_generate_pat_returns_prefixed_token(self):
        raw, prefix, token_hash = generate_pat()
        self.assertTrue(raw.startswith("pat_"))
        self.assertEqual(len(prefix), 8)
        self.assertEqual(len(token_hash), 64)  # SHA-256 hex

    def test_hash_is_deterministic(self):
        raw, _, expected_hash = generate_pat()
        self.assertEqual(hash_token(raw), expected_hash)

    def test_is_valid_active_token(self):
        _, prefix, token_hash = generate_pat()
        pat = PersonalAccessToken.objects.create(
            user=self.user,
            name="Test",
            token_prefix=prefix,
            token_hash=token_hash,
        )
        self.assertTrue(pat.is_valid)

    def test_is_valid_revoked_token(self):
        _, prefix, token_hash = generate_pat()
        pat = PersonalAccessToken.objects.create(
            user=self.user,
            name="Test",
            token_prefix=prefix,
            token_hash=token_hash,
            revoked_at=timezone.now(),
        )
        self.assertFalse(pat.is_valid)

    def test_is_valid_expired_token(self):
        _, prefix, token_hash = generate_pat()
        pat = PersonalAccessToken.objects.create(
            user=self.user,
            name="Test",
            token_prefix=prefix,
            token_hash=token_hash,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        self.assertFalse(pat.is_valid)

    def test_is_valid_future_expiry(self):
        _, prefix, token_hash = generate_pat()
        pat = PersonalAccessToken.objects.create(
            user=self.user,
            name="Test",
            token_prefix=prefix,
            token_hash=token_hash,
            expires_at=timezone.now() + timedelta(days=30),
        )
        self.assertTrue(pat.is_valid)


class PATAuthenticationTest(TestCase):
    """Test that PAT tokens authenticate API requests."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="pat_auth@example.com",
            email="pat_auth@example.com",
            password="testpass123",
        )
        from apps.tenants.services import create_tenant

        self.tenant = create_tenant(display_name="PAT Auth User", telegram_chat_id=900)
        # Reassign user to the tenant's user for auth
        self.user = self.tenant.user
        self.client = APIClient()

    def _create_pat(self, **kwargs):
        raw, prefix, token_hash = generate_pat()
        defaults = {
            "user": self.user,
            "name": "Test Token",
            "token_prefix": prefix,
            "token_hash": token_hash,
        }
        defaults.update(kwargs)
        pat = PersonalAccessToken.objects.create(**defaults)
        return raw, pat

    def test_valid_pat_authenticates(self):
        raw, _ = self._create_pat()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 200)

    def test_invalid_pat_returns_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer pat_invalidtoken123456789012345678901234567")
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 401)

    def test_revoked_pat_returns_401(self):
        raw, pat = self._create_pat()
        pat.revoked_at = timezone.now()
        pat.save(update_fields=["revoked_at"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 401)

    def test_expired_pat_returns_401(self):
        raw, _ = self._create_pat(expires_at=timezone.now() - timedelta(hours=1))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 401)

    def test_pat_updates_last_used_at(self):
        raw, pat = self._create_pat()
        self.assertIsNone(pat.last_used_at)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.client.get("/api/v1/auth/me/")
        pat.refresh_from_db()
        self.assertIsNotNone(pat.last_used_at)

    def test_jwt_still_works_alongside_pat(self):
        """JWT auth must not break when PAT auth is also registered."""
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 200)


class PATManagementTest(TestCase):
    """Test PAT CRUD endpoints (JWT-authenticated)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="pat_mgmt@example.com",
            email="pat_mgmt@example.com",
            password="testpass123",
        )
        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_create_pat_returns_raw_token(self):
        response = self.client.post(
            "/api/v1/auth/tokens/create/",
            {"name": "YardTalk on MacBook"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["token"].startswith("pat_"))
        self.assertIn("warning", data)
        self.assertEqual(data["name"], "YardTalk on MacBook")

    def test_create_pat_with_expiry(self):
        response = self.client.post(
            "/api/v1/auth/tokens/create/",
            {"name": "Expiring Token", "expires_in_days": 30},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIsNotNone(response.json()["expires_at"])

    def test_list_pats_excludes_revoked(self):
        _, prefix1, hash1 = generate_pat()
        _, prefix2, hash2 = generate_pat()
        PersonalAccessToken.objects.create(
            user=self.user,
            name="Active",
            token_prefix=prefix1,
            token_hash=hash1,
        )
        PersonalAccessToken.objects.create(
            user=self.user,
            name="Revoked",
            token_prefix=prefix2,
            token_hash=hash2,
            revoked_at=timezone.now(),
        )

        response = self.client.get("/api/v1/auth/tokens/")
        self.assertEqual(response.status_code, 200)
        names = [t["name"] for t in response.json()]
        self.assertIn("Active", names)
        self.assertNotIn("Revoked", names)

    def test_revoke_pat(self):
        _, prefix, token_hash = generate_pat()
        pat = PersonalAccessToken.objects.create(
            user=self.user,
            name="To Revoke",
            token_prefix=prefix,
            token_hash=token_hash,
        )

        response = self.client.delete(f"/api/v1/auth/tokens/{pat.id}/")
        self.assertEqual(response.status_code, 204)

        pat.refresh_from_db()
        self.assertIsNotNone(pat.revoked_at)

    def test_revoke_already_revoked_returns_400(self):
        _, prefix, token_hash = generate_pat()
        pat = PersonalAccessToken.objects.create(
            user=self.user,
            name="Already Revoked",
            token_prefix=prefix,
            token_hash=token_hash,
            revoked_at=timezone.now(),
        )

        response = self.client.delete(f"/api/v1/auth/tokens/{pat.id}/")
        self.assertEqual(response.status_code, 400)

    def test_revoke_other_users_token_returns_404(self):
        other_user = User.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            password="pass123",
        )
        _, prefix, token_hash = generate_pat()
        pat = PersonalAccessToken.objects.create(
            user=other_user,
            name="Other's Token",
            token_prefix=prefix,
            token_hash=token_hash,
        )

        response = self.client.delete(f"/api/v1/auth/tokens/{pat.id}/")
        self.assertEqual(response.status_code, 404)
