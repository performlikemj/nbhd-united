"""Transactional Sign in with Apple resolution and revocation-outbox writes."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .apple_client import AppleGrant
from .apple_crypto import encrypt_apple_refresh_token
from .apple_models import (
    APPLE_PROVIDER,
    AppleAuthTransaction,
    AppleRevocationOutbox,
    ExternalIdentity,
)
from .apple_models import (
    AppleGrant as AppleGrantRecord,
)
from .models import User

logger = logging.getLogger(__name__)


class AppleTransactionRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AppleResolutionRejected(Exception):
    def __init__(self, error: str, reason: str):
        self.error = error
        self.reason = reason
        super().__init__(error)


@dataclass(frozen=True)
class AppleAuthResolution:
    user: User
    created: bool


def _find_identity_for_update(subject: str) -> ExternalIdentity | None:
    return (
        ExternalIdentity.objects.select_for_update(of=("self",))
        .select_related("user")
        .filter(provider=APPLE_PROVIDER, subject=subject)
        .first()
    )


def consume_apple_transaction(transaction_id, state: str, *, expected_purpose: str) -> str:
    """Validate and consume one popup transaction under a row lock."""

    with transaction.atomic():
        try:
            row = AppleAuthTransaction.objects.select_for_update(of=("self",)).get(id=transaction_id)
        except AppleAuthTransaction.DoesNotExist as exc:
            raise AppleTransactionRejected("transaction_not_found") from exc

        if not hmac.compare_digest(state.encode("utf-8"), row.state.encode("utf-8")):
            raise AppleTransactionRejected("state_mismatch")
        now = timezone.now()
        if row.purpose != expected_purpose:
            raise AppleTransactionRejected("purpose_mismatch")
        if row.consumed_at is not None:
            raise AppleTransactionRejected("transaction_consumed")
        if now >= row.expires_at:
            raise AppleTransactionRejected("transaction_expired")

        row.consumed_at = now
        row.save(update_fields=["consumed_at"])
        return row.nonce_hash


def get_apple_transaction_purpose(transaction_id) -> str | None:
    """Read the immutable lane so callers can run lane readiness pre-consume."""

    return AppleAuthTransaction.objects.filter(id=transaction_id).values_list("purpose", flat=True).first()


def _email_policy(email: str) -> None:
    matches = list(User.objects.filter(email__iexact=email).order_by("date_joined", "id")[:3])
    if not matches:
        return
    if len(matches) > 1:
        raise AppleResolutionRejected("invalid_grant", "duplicate_local_email")
    if matches[0].is_active:
        raise AppleResolutionRejected("link_required", "active_email_match")
    raise AppleResolutionRejected("invalid_grant", "inactive_email_match")


def _update_existing_identity(
    identity: ExternalIdentity,
    grant: AppleGrant,
) -> User:
    user = identity.user
    if not user.is_active:
        raise AppleResolutionRejected("invalid_grant", "inactive_identity_user")

    now = timezone.now()
    update_fields = ["last_login_at"]
    identity.last_login_at = now
    if not identity.email_at_auth and grant.email:
        identity.email_at_auth = grant.email
        identity.email_is_relay = grant.email_is_relay
        identity.email_verified_at_auth = grant.email_verified
        update_fields.extend(["email_at_auth", "email_is_relay", "email_verified_at_auth"])
    identity.save(update_fields=update_fields)
    return user


def _is_web_lane(client_id: str) -> bool:
    services_id = getattr(settings, "APPLE_SIWA_SERVICES_ID", "")
    return isinstance(services_id, str) and client_id == services_id.strip()


def _write_legacy_web_grant(
    identity: ExternalIdentity,
    client_id: str,
    ciphertext: str,
) -> None:
    """Keep the previous revision's single web-owned token slot coherent."""

    if not _is_web_lane(client_id):
        return
    now = timezone.now()
    identity.audience = client_id
    identity.refresh_token_encrypted = ciphertext
    identity.refresh_token_updated_at = now
    identity.save(
        update_fields=[
            "audience",
            "refresh_token_encrypted",
            "refresh_token_updated_at",
        ]
    )


def _upsert_apple_grant(
    identity: ExternalIdentity,
    grant: AppleGrant,
    *,
    ciphertext: str | None = None,
) -> str | None:
    if ciphertext is None:
        if not grant.refresh_token:
            return None
        ciphertext = encrypt_apple_refresh_token(grant.refresh_token)
    AppleGrantRecord.objects.update_or_create(
        identity=identity,
        client_id=grant.audience,
        defaults={"refresh_token_encrypted": ciphertext},
    )
    _write_legacy_web_grant(identity, grant.audience, ciphertext)
    return ciphertext


def _fallback_username(subject: str) -> str:
    return f"apple_{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:32]}"


def _create_identity_user(
    grant: AppleGrant,
    username: str,
    ciphertext: str,
    *,
    display_name: str = "Friend",
    terms_version: str | None = None,
) -> User:
    user = User.objects.create_user(
        username=username,
        email=grant.email,
        display_name=display_name,
    )
    # Django 6's UserManager writes make_password(None) directly and therefore
    # does not call this model's password-stamp override. Persist both fields
    # explicitly so the pw_iat token minted after commit authenticates.
    user.set_unusable_password()
    update_fields = ["password", "password_last_changed_at"]
    if terms_version:
        user.terms_version = terms_version
        user.terms_accepted_at = timezone.now()
        update_fields.extend(["terms_version", "terms_accepted_at"])
    user.save(update_fields=update_fields)
    now = timezone.now()
    legacy_ciphertext = ciphertext if _is_web_lane(grant.audience) else ""
    identity = ExternalIdentity.objects.create(
        user=user,
        provider=APPLE_PROVIDER,
        subject=grant.subject,
        issuer=grant.issuer,
        audience=grant.audience,
        email_at_auth=grant.email,
        email_is_relay=grant.email_is_relay,
        email_verified_at_auth=grant.email_verified,
        refresh_token_encrypted=legacy_ciphertext,
        refresh_token_updated_at=now,
        last_login_at=now,
    )
    AppleGrantRecord.objects.update_or_create(
        identity=identity,
        client_id=grant.audience,
        defaults={"refresh_token_encrypted": ciphertext},
    )
    return user


def resolve_apple_auth(grant: AppleGrant) -> AppleAuthResolution:
    """Resolve an existing Apple identity or create a new local account."""

    with transaction.atomic():
        identity = _find_identity_for_update(grant.subject)
        if identity is not None:
            _upsert_apple_grant(identity, grant)
            return AppleAuthResolution(_update_existing_identity(identity, grant), False)

        # Gate ordering is deliberately before both email validation and lookup.
        if getattr(settings, "PREVIEW_ACCESS_KEY", ""):
            raise AppleResolutionRejected("signup_gated", "preview_gate")
        if not grant.email or not grant.email_verified:
            raise AppleResolutionRejected("invalid_grant", "email_not_verified")
        _email_policy(grant.email)
        # Native identity-token-only grants deliberately carry no refresh
        # token, making this sign-in-only guard load-bearing. Native create/link
        # requires code exchange plus a per-audience grant schema (see playbook).
        if not grant.refresh_token:
            raise AppleResolutionRejected("invalid_grant", "missing_refresh_token")

        ciphertext = encrypt_apple_refresh_token(grant.refresh_token)
        fallback = _fallback_username(grant.subject)
        use_email_username = len(grant.email) <= 150
        if use_email_username and User.objects.filter(username=grant.email).exists():
            # A password signup can commit after the first email-policy read
            # but before username selection. Re-run policy before falling back
            # so its newly-claimed email becomes link_required rather than a
            # duplicate Apple account with an opaque username.
            _email_policy(grant.email)
            use_email_username = False
        usernames = [grant.email, fallback] if use_email_username else [fallback]

        for username in usernames:
            try:
                # Nested atomic is a savepoint inside Phase C. If either User or
                # identity uniqueness races, the whole attempted pair rolls
                # back before we re-read the winner.
                with transaction.atomic():
                    user = _create_identity_user(grant, username, ciphertext)
                return AppleAuthResolution(user, True)
            except IntegrityError:
                identity = _find_identity_for_update(grant.subject)
                if identity is not None:
                    _upsert_apple_grant(identity, grant, ciphertext=ciphertext)
                    return AppleAuthResolution(_update_existing_identity(identity, grant), False)
                # A password signup can win the email/username race. Re-run the
                # entire email policy after savepoint rollback so no orphan User
                # survives and the result becomes link_required.
                _email_policy(grant.email)
                if username == fallback:
                    break

        raise AppleResolutionRejected("invalid_grant", "identity_create_conflict")


def resolve_apple_native_auth(grant: AppleGrant) -> AppleAuthResolution:
    """Sign in an existing Apple subject without creating native accounts."""

    with transaction.atomic():
        identity = _find_identity_for_update(grant.subject)
        if identity is None:
            raise AppleResolutionRejected("account_not_found", "native_signin_only")
        return AppleAuthResolution(_update_existing_identity(identity, grant), False)


def _native_display_name(given_name: str, family_name: str) -> str:
    display_name = " ".join(part.strip() for part in (given_name, family_name) if part.strip())
    return display_name or "Friend"


def resolve_apple_native_code_auth(
    grant: AppleGrant,
    *,
    given_name: str = "",
    family_name: str = "",
    terms_version: str = "",
) -> AppleAuthResolution:
    """Resolve a server-exchanged native grant, creating when policy allows."""

    with transaction.atomic():
        identity = _find_identity_for_update(grant.subject)
        if identity is not None:
            _upsert_apple_grant(identity, grant)
            return AppleAuthResolution(_update_existing_identity(identity, grant), False)

        if getattr(settings, "PREVIEW_ACCESS_KEY", ""):
            raise AppleResolutionRejected("signup_gated", "preview_gate")
        if not grant.email:
            raise AppleResolutionRejected("email_unavailable", "email_unavailable")
        if not grant.email_verified:
            raise AppleResolutionRejected("invalid_grant", "email_not_verified")
        _email_policy(grant.email)
        if not grant.refresh_token:
            raise AppleResolutionRejected("invalid_grant", "missing_refresh_token")
        accepted_terms_version = terms_version.strip()
        if not accepted_terms_version:
            raise AppleResolutionRejected("invalid_grant", "missing_terms")

        ciphertext = encrypt_apple_refresh_token(grant.refresh_token)
        fallback = _fallback_username(grant.subject)
        use_email_username = len(grant.email) <= 150
        if use_email_username and User.objects.filter(username=grant.email).exists():
            _email_policy(grant.email)
            use_email_username = False
        usernames = [grant.email, fallback] if use_email_username else [fallback]
        display_name = _native_display_name(given_name, family_name)

        for username in usernames:
            try:
                with transaction.atomic():
                    user = _create_identity_user(
                        grant,
                        username,
                        ciphertext,
                        display_name=display_name,
                        terms_version=accepted_terms_version,
                    )
                return AppleAuthResolution(user, True)
            except IntegrityError:
                identity = _find_identity_for_update(grant.subject)
                if identity is not None:
                    _upsert_apple_grant(identity, grant, ciphertext=ciphertext)
                    return AppleAuthResolution(_update_existing_identity(identity, grant), False)
                _email_policy(grant.email)
                if username == fallback:
                    break

        raise AppleResolutionRejected("invalid_grant", "identity_create_conflict")


def link_apple_identity(user: User, grant: AppleGrant) -> None:
    """Link a fresh Apple grant to an authenticated, password-stepped-up user."""

    # Native identity-token-only grants deliberately carry no refresh token,
    # making this sign-in-only guard load-bearing. Native create/link requires
    # code exchange plus a per-audience grant schema (see playbook).
    if not grant.refresh_token:
        raise AppleResolutionRejected("invalid_grant", "missing_refresh_token")
    ciphertext = encrypt_apple_refresh_token(grant.refresh_token)

    with transaction.atomic():
        current = (
            ExternalIdentity.objects.select_for_update(of=("self",))
            .select_related("user")
            .filter(user=user, provider=APPLE_PROVIDER)
            .first()
        )
        if current is not None:
            if current.subject == grant.subject:
                _upsert_apple_grant(current, grant, ciphertext=ciphertext)
                _update_existing_identity(current, grant)
                return
            raise AppleResolutionRejected("already_linked", "different_identity_on_user")

        owner = (
            ExternalIdentity.objects.select_for_update(of=("self",))
            .select_related("user")
            .filter(provider=APPLE_PROVIDER, subject=grant.subject)
            .first()
        )
        if owner is not None:
            if owner.user_id == user.id:
                _upsert_apple_grant(owner, grant, ciphertext=ciphertext)
                _update_existing_identity(owner, grant)
                return
            raise AppleResolutionRejected("apple_id_in_use", "identity_owned_elsewhere")

        now = timezone.now()
        try:
            with transaction.atomic():
                identity = ExternalIdentity.objects.create(
                    user=user,
                    provider=APPLE_PROVIDER,
                    subject=grant.subject,
                    issuer=grant.issuer,
                    audience=grant.audience,
                    email_at_auth=grant.email,
                    email_is_relay=grant.email_is_relay,
                    email_verified_at_auth=grant.email_verified,
                    refresh_token_encrypted=(ciphertext if _is_web_lane(grant.audience) else ""),
                    refresh_token_updated_at=now,
                    last_login_at=now,
                )
                _upsert_apple_grant(identity, grant, ciphertext=ciphertext)
            return
        except IntegrityError:
            current = (
                ExternalIdentity.objects.select_for_update(of=("self",))
                .select_related("user")
                .filter(user=user, provider=APPLE_PROVIDER)
                .first()
            )
            if current is not None and current.subject == grant.subject:
                _upsert_apple_grant(current, grant, ciphertext=ciphertext)
                _update_existing_identity(current, grant)
                return
            if current is not None:
                raise AppleResolutionRejected("already_linked", "different_identity_race") from None
            if ExternalIdentity.objects.filter(provider=APPLE_PROVIDER, subject=grant.subject).exists():
                raise AppleResolutionRejected("apple_id_in_use", "identity_owner_race") from None
            raise AppleResolutionRejected("invalid_grant", "identity_link_conflict") from None


def _publish_apple_revocations(outbox_ids: tuple[str, ...]) -> None:
    from apps.cron.publish import publish_task

    for outbox_id in outbox_ids:
        try:
            publish_task(
                "revoke_apple_token",
                outbox_id,
                idempotency_key=f"apple-revoke-{outbox_id}",
            )
        except Exception:
            logger.warning(
                "auth.apple.revocation.publish_failed outbox_id=%s",
                outbox_id,
                exc_info=True,
            )


def _schedule_outbox_publication(outbox_ids: list[str]) -> None:
    if not outbox_ids:
        return
    immutable_ids = tuple(outbox_ids)
    transaction.on_commit(lambda: _publish_apple_revocations(immutable_ids))


def enqueue_unpersisted_apple_grant(grant: AppleGrant) -> AppleRevocationOutbox | None:
    """Persist and enqueue a verified refresh token that will not be retained."""

    return enqueue_received_apple_refresh_token(
        grant.refresh_token,
        grant.audience,
        subject=grant.subject,
        subject_verified=True,
    )


def enqueue_received_apple_refresh_token(
    refresh_token: str | None,
    client_id: str,
    *,
    subject: str | None = None,
    subject_verified: bool = False,
) -> AppleRevocationOutbox | None:
    """Durably retain a received token without requiring trusted ID claims."""

    if not refresh_token:
        return None
    ciphertext = encrypt_apple_refresh_token(refresh_token)
    with transaction.atomic():
        row = AppleRevocationOutbox.objects.create(
            token_ciphertext=ciphertext,
            subject=subject,
            subject_verified=subject_verified,
            client_id=client_id,
        )
        _schedule_outbox_publication([str(row.id)])
    return row


def _copy_identity_tokens_to_outbox(user: User, *, using: str | None = None) -> list[str]:
    database = using or user._state.db or "default"
    outbox_ids: list[str] = []
    identities = list(
        ExternalIdentity.objects.using(database)
        .select_for_update(of=("self",))
        .filter(user=user, provider=APPLE_PROVIDER)
        .only("id", "subject")
    )
    grants = (
        AppleGrantRecord.objects.using(database)
        .select_for_update(of=("self",))
        .filter(identity__in=identities)
        .select_related("identity")
        .only(
            "client_id",
            "refresh_token_encrypted",
            "identity__subject",
        )
    )
    for grant in grants:
        existing = (
            AppleRevocationOutbox.objects.using(database)
            .filter(
                subject=grant.identity.subject,
                client_id=grant.client_id,
                token_ciphertext=grant.refresh_token_encrypted,
            )
            .first()
        )
        if existing is not None:
            continue
        row = AppleRevocationOutbox.objects.using(database).create(
            token_ciphertext=grant.refresh_token_encrypted,
            subject=grant.identity.subject,
            subject_verified=True,
            client_id=grant.client_id,
        )
        outbox_ids.append(str(row.id))
    return outbox_ids


def revoke_apple_before_delete(user: User) -> None:
    """Atomically preserve Apple grants, then publish only outbox UUIDs."""

    database = user._state.db or "default"
    with transaction.atomic(using=database):
        outbox_ids = _copy_identity_tokens_to_outbox(user, using=database)
        _schedule_outbox_publication(outbox_ids)


def write_apple_revocation_outbox_fallback(user: User, *, using: str | None = None) -> None:
    """Signal-only belt-and-braces copy. Deliberately performs no publishing."""

    _copy_identity_tokens_to_outbox(user, using=using)
