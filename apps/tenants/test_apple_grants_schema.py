"""Schema and migration coverage for the dark SIWA grants expansion."""

from __future__ import annotations

import importlib

from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from .apple_models import AppleGrant, AppleRevocationOutbox, ExternalIdentity
from .models import User

SERVICES_ID = "org.hoodunited.web"
BUNDLE_ID = "org.hoodunited.nbhd"

migration = importlib.import_module("apps.tenants.migrations.0149_backfill_apple_grants_and_outbox")


def _identity(
    *,
    username: str,
    subject: str,
    audience: str,
    ciphertext: str = "",
) -> ExternalIdentity:
    user = User.objects.create_user(username=username, email=f"{username}@example.com")
    return ExternalIdentity.objects.create(
        user=user,
        subject=subject,
        audience=audience,
        refresh_token_encrypted=ciphertext,
        refresh_token_updated_at=timezone.now(),
    )


@override_settings(
    APPLE_SIWA_SERVICES_ID=SERVICES_ID,
    APPLE_SIWA_BUNDLE_ID=BUNDLE_ID,
)
class AppleGrantSchemaTests(TestCase):
    def test_grant_is_unique_per_identity_and_client_id(self):
        identity = _identity(
            username="grant-unique",
            subject="grant-unique-subject",
            audience=SERVICES_ID,
        )
        AppleGrant.objects.create(
            identity=identity,
            client_id=SERVICES_ID,
            refresh_token_encrypted="ciphertext-one",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AppleGrant.objects.create(
                identity=identity,
                client_id=SERVICES_ID,
                refresh_token_encrypted="ciphertext-two",
            )

    def test_backfill_copies_ciphertext_with_identity_audience_provenance(self):
        identity = _identity(
            username="grant-backfill",
            subject="grant-backfill-subject",
            audience=BUNDLE_ID,
            ciphertext="encrypted-refresh-token-verbatim",
        )

        migration.backfill_apple_grants_and_outbox(django_apps, None)

        grant = AppleGrant.objects.get(identity=identity)
        self.assertEqual(grant.client_id, BUNDLE_ID)
        self.assertEqual(
            grant.refresh_token_encrypted,
            "encrypted-refresh-token-verbatim",
        )

    def test_backfill_raises_for_empty_token_bearing_audience(self):
        _identity(
            username="grant-empty-audience",
            subject="grant-empty-audience-subject",
            audience="",
            ciphertext="encrypted-refresh-token",
        )

        with self.assertRaisesRegex(RuntimeError, "invalid audience provenance"):
            migration.backfill_apple_grants_and_outbox(django_apps, None)

        self.assertFalse(AppleGrant.objects.exists())

    def test_backfill_raises_for_audience_outside_known_lanes(self):
        _identity(
            username="grant-unknown-audience",
            subject="grant-unknown-audience-subject",
            audience="org.hoodunited.unknown",
            ciphertext="encrypted-refresh-token",
        )

        with self.assertRaisesRegex(RuntimeError, "invalid audience provenance"):
            migration.backfill_apple_grants_and_outbox(django_apps, None)

        self.assertFalse(AppleGrant.objects.exists())

    def test_reverse_deletes_only_backfilled_grant_shape(self):
        identity = _identity(
            username="grant-reverse",
            subject="grant-reverse-subject",
            audience=SERVICES_ID,
            ciphertext="legacy-ciphertext",
        )
        migration.backfill_apple_grants_and_outbox(django_apps, None)
        AppleGrant.objects.create(
            identity=identity,
            client_id=BUNDLE_ID,
            refresh_token_encrypted="native-ciphertext",
        )

        migration.reverse_apple_grants_and_outbox(django_apps, None)

        self.assertFalse(AppleGrant.objects.filter(client_id=SERVICES_ID).exists())
        self.assertTrue(AppleGrant.objects.filter(client_id=BUNDLE_ID).exists())

    def test_outbox_backfill_uses_identity_audience_or_services_default(self):
        identity = _identity(
            username="outbox-match",
            subject="outbox-matched-subject",
            audience=BUNDLE_ID,
        )
        matched = AppleRevocationOutbox.objects.create(
            token_ciphertext="matched-ciphertext",
            subject=identity.subject,
        )
        unmatched = AppleRevocationOutbox.objects.create(
            token_ciphertext="unmatched-ciphertext",
            subject="missing-subject",
        )

        migration.backfill_apple_grants_and_outbox(django_apps, None)

        matched.refresh_from_db()
        unmatched.refresh_from_db()
        self.assertEqual(matched.client_id, BUNDLE_ID)
        self.assertEqual(matched.backfill_source, "identity_audience")
        self.assertTrue(matched.subject_verified)
        self.assertEqual(unmatched.client_id, SERVICES_ID)
        self.assertEqual(unmatched.backfill_source, "services_default")
        self.assertTrue(unmatched.subject_verified)


class UserExpandSchemaTests(TestCase):
    def test_lower_email_constraint_blocks_case_variant_duplicate(self):
        User.objects.create_user(username="email-lower", email="Member@Example.com")

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="email-upper", email="member@example.COM")

    def test_lower_email_constraint_allows_multiple_blank_emails(self):
        User.objects.create_user(username="blank-email-one", email="")
        User.objects.create_user(username="blank-email-two", email="")

        self.assertEqual(User.objects.filter(email="").count(), 2)

    def test_terms_columns_are_writable(self):
        accepted_at = timezone.now()
        user = User.objects.create_user(username="terms-user", email="terms@example.com")
        user.terms_version = "2026-08"
        user.terms_accepted_at = accepted_at
        user.save(update_fields=["terms_version", "terms_accepted_at"])

        user.refresh_from_db()
        self.assertEqual(user.terms_version, "2026-08")
        self.assertEqual(user.terms_accepted_at, accepted_at)
