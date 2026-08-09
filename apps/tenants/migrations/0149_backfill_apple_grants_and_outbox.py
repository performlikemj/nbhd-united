"""Backfill per-audience Apple grants and revocation client provenance."""

from __future__ import annotations

import re

from django.conf import settings
from django.db import migrations

APPLE_PROVIDER = "apple"
IDENTITY_AUDIENCE = "identity_audience"
SERVICES_DEFAULT = "services_default"
_CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]\Z")


def _known_client_ids() -> set[str]:
    return {
        value.strip()
        for value in (
            getattr(settings, "APPLE_SIWA_SERVICES_ID", ""),
            getattr(settings, "APPLE_SIWA_BUNDLE_ID", ""),
        )
        if isinstance(value, str) and value.strip()
    }


def _is_valid_lane_audience(audience: str, known_client_ids: set[str]) -> bool:
    return bool(
        audience
        and audience == audience.strip()
        and "." in audience
        and ".." not in audience
        and _CLIENT_ID_PATTERN.fullmatch(audience)
        and audience in known_client_ids
    )


def backfill_apple_grants_and_outbox(apps, schema_editor) -> None:
    ExternalIdentity = apps.get_model("tenants", "ExternalIdentity")
    AppleGrant = apps.get_model("tenants", "AppleGrant")
    AppleRevocationOutbox = apps.get_model("tenants", "AppleRevocationOutbox")

    known_client_ids = _known_client_ids()
    token_identities = ExternalIdentity.objects.exclude(refresh_token_encrypted="")
    for identity in token_identities.iterator():
        if not _is_valid_lane_audience(identity.audience, known_client_ids):
            raise RuntimeError(
                f"Cannot backfill Apple grant: token-bearing identity {identity.pk} has invalid audience provenance"
            )
        AppleGrant.objects.get_or_create(
            identity_id=identity.pk,
            client_id=identity.audience,
            defaults={"refresh_token_encrypted": identity.refresh_token_encrypted},
        )

    pending_outbox = AppleRevocationOutbox.objects.filter(client_id__isnull=True)
    subjects = set(pending_outbox.exclude(subject__isnull=True).values_list("subject", flat=True))
    audience_by_subject = dict(
        ExternalIdentity.objects.filter(
            provider=APPLE_PROVIDER,
            subject__in=subjects,
        ).values_list("subject", "audience")
    )
    services_id = getattr(settings, "APPLE_SIWA_SERVICES_ID", "")

    for row in pending_outbox.iterator():
        if row.subject in audience_by_subject:
            row.client_id = audience_by_subject[row.subject]
            row.backfill_source = IDENTITY_AUDIENCE
        else:
            row.client_id = services_id
            row.backfill_source = SERVICES_DEFAULT
        row.save(update_fields=["client_id", "backfill_source"])


def reverse_apple_grants_and_outbox(apps, schema_editor) -> None:
    ExternalIdentity = apps.get_model("tenants", "ExternalIdentity")
    AppleGrant = apps.get_model("tenants", "AppleGrant")
    AppleRevocationOutbox = apps.get_model("tenants", "AppleRevocationOutbox")

    for identity in ExternalIdentity.objects.exclude(refresh_token_encrypted="").iterator():
        AppleGrant.objects.filter(
            identity_id=identity.pk,
            client_id=identity.audience,
            refresh_token_encrypted=identity.refresh_token_encrypted,
        ).delete()

    AppleRevocationOutbox.objects.filter(backfill_source__in=[IDENTITY_AUDIENCE, SERVICES_DEFAULT]).update(
        client_id=None, backfill_source=""
    )


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0148_applegrant_applerevocationoutbox_backfill_source_and_more"),
    ]

    operations = [
        migrations.RunPython(
            backfill_apple_grants_and_outbox,
            reverse_apple_grants_and_outbox,
        ),
    ]
