"""Email-verification token generator.

Subclasses Django's PasswordResetTokenGenerator so the token's HMAC
includes the email + email_verified flag. Effect:
  - Token invalidates the moment the user verifies (email_verified flips).
  - Token invalidates if the user changes their email.
  - Token expires per Django's PASSWORD_RESET_TIMEOUT setting (default 3 days).

We deliberately re-use the password-reset machinery (uidb64 encoding, HMAC,
timeout) so there is one mental model for "signed link to a user".
"""

from __future__ import annotations

from django.contrib.auth.tokens import PasswordResetTokenGenerator


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):  # noqa: D401
        # Include email_verified so the token dies the instant the user
        # successfully verifies. Include email so changing the email
        # address (future feature) also invalidates outstanding links.
        return f"{user.pk}{timestamp}{user.email}{int(bool(user.email_verified))}"


email_verification_token_generator = EmailVerificationTokenGenerator()
