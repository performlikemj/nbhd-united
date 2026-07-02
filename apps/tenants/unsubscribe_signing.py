"""Per-user HMAC token signing for one-click email-unsubscribe URLs.

Mirrors ``promo_signing.py`` but with a distinct salt so an unsubscribe
token can never be substituted for a promo-redemption token (or vice
versa). Each marketing email carries a link of the shape::

    {API_BASE_URL}/api/v1/tenants/unsubscribe/<token>/

(the unsubscribe view is a backend Django endpoint that renders its own
page, so the link points at ``API_BASE_URL`` — not the static frontend.)

``token`` is an HMAC-signed string (signed by Django's ``SECRET_KEY``)
encoding the user id. The unsubscribe view verifies the signature and,
on success, sets ``User.email_opt_out``. No expiry is applied — an
unsubscribe link stays valid indefinitely (RFC 8058 one-click links are
expected to keep working long after the message was sent).
"""

from __future__ import annotations

from django.core.signing import BadSignature, Signer

# Distinct salt so unsubscribe tokens can't be swapped for promo tokens
# or any other signed payload elsewhere in the codebase.
_SIGNER_SALT = "nbhd.unsub.v1"


def make_unsubscribe_token(user_id) -> str:
    """Produce a signed token binding a user id.

    Stable for a given user across every send, so the same address always
    unsubscribes the same account regardless of which campaign email the
    click came from.
    """
    signer = Signer(salt=_SIGNER_SALT)
    return signer.sign(str(user_id))


def verify_unsubscribe_token(token: str) -> str | None:
    """Verify a signed unsubscribe token.

    Returns the user id (as the raw string the URL carried) on success,
    ``None`` on any failure (bad signature, malformed payload). The caller
    treats ``None`` as a 404 so an invalid or tampered token leaks nothing.
    """
    signer = Signer(salt=_SIGNER_SALT)
    try:
        return signer.unsign(token)
    except BadSignature:
        return None
