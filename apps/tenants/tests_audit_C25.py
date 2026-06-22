"""Regression tests for cluster C25 — SignupView email normalisation and password validation.

FA-1054: email is lowercased/stripped before duplicate check and stored value.
FA-1095: validate_password() is called before create_user, enforcing AUTH_PASSWORD_VALIDATORS.
"""


from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

SIGNUP_URL = "/api/v1/auth/signup/"


class SignupEmailNormalisationTests(TestCase):
    """FA-1054 — email must be lowercased and duplicate-check must be case-insensitive."""

    def setUp(self):
        self.client = APIClient()

    def test_signup_lowercases_email(self):
        """Email with mixed case is stored lowercase."""
        resp = self.client.post(
            SIGNUP_URL,
            {"email": "Test@Example.COM", "password": "Str0ng!Pass99"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(username="test@example.com")
        self.assertEqual(user.email, "test@example.com")

    def test_signup_strips_whitespace_from_email(self):
        """Leading/trailing whitespace in the email is removed before use."""
        resp = self.client.post(
            SIGNUP_URL,
            {"email": "  user@example.com  ", "password": "Str0ng!Pass99"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(User.objects.filter(email="user@example.com").exists())

    def test_duplicate_email_case_insensitive_returns_409(self):
        """Signup with the same address in different case should return 409 (not create a dup)."""
        User.objects.create_user(
            username="existing@example.com",
            email="existing@example.com",
            password="Str0ng!Pass99",
        )
        resp = self.client.post(
            SIGNUP_URL,
            {"email": "EXISTING@example.com", "password": "Str0ng!Pass99"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        # Only one account should exist.
        self.assertEqual(User.objects.filter(email__iexact="existing@example.com").count(), 1)


class SignupPasswordValidationTests(TestCase):
    """FA-1095 — validate_password must be called at signup, mirroring the reset flow."""

    def setUp(self):
        self.client = APIClient()

    def test_common_password_rejected(self):
        """Posting a common password (e.g. 'password') should return 400."""
        resp = self.client.post(
            SIGNUP_URL,
            {"email": "newuser@example.com", "password": "password"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("detail", resp.data)
        # No user should have been created.
        self.assertFalse(User.objects.filter(email="newuser@example.com").exists())

    def test_numeric_only_password_rejected(self):
        """Entirely numeric passwords should be rejected."""
        resp = self.client.post(
            SIGNUP_URL,
            {"email": "newuser2@example.com", "password": "12345678"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_too_short_password_rejected(self):
        """Passwords shorter than the minimum length should be rejected."""
        resp = self.client.post(
            SIGNUP_URL,
            {"email": "newuser3@example.com", "password": "Ab1!"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_strong_password_accepted(self):
        """A strong, unique password should allow signup."""
        resp = self.client.post(
            SIGNUP_URL,
            {"email": "newuser4@example.com", "password": "Xk9$mP2nQr!7vZ"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)
