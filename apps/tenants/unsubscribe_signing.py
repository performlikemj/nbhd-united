"""Per-user HMAC token signing for one-click email-unsubscribe URLs.

Mirrors ``promo_signing.py`` but with a distinct salt so an unsubscribe
token can never be substituted for a promo-redemption token (or vice
versa). Each marketing email carries a link of the shape::

    {API_BASE_URL}/api/v1/tenants/unsubscribe/<token>/

(the unsubscribe view is a backend Django endpoint that renders its own
page, so the link points at ``API_BASE_URL`` — not the static frontend.)

``token`` is an HMAC-signed string (signed by Django's ``SECRET_KEY``)
encoding the user id and, optionally, an opt-out **category**. The
unsubscribe view verifies the signature and, on success, sets the flag
the category names:

- ``marketing`` (default) → ``User.email_opt_out``. Marketing tokens keep
  the original bare-user-id payload, so every previously sent link (and
  every promo-campaign send going forward) is byte-identical and keeps
  working unchanged.
- ``service`` → ``User.service_email_opt_out`` (operational notices such
  as the channel-sunset broadcast). Encoded as ``<user_id>|service``
  inside the same signed payload; user ids are UUIDs, so ``|`` can never
  appear in a legacy payload.

No expiry is applied — an unsubscribe link stays valid indefinitely
(RFC 8058 one-click links are expected to keep working long after the
message was sent).
"""

from __future__ import annotations

from django.core.signing import BadSignature, Signer

# Distinct salt so unsubscribe tokens can't be swapped for promo tokens
# or any other signed payload elsewhere in the codebase.
_SIGNER_SALT = "nbhd.unsub.v1"

CATEGORY_MARKETING = "marketing"
CATEGORY_SERVICE = "service"
_VALID_CATEGORIES = frozenset({CATEGORY_MARKETING, CATEGORY_SERVICE})

# Separator between user id and category inside the signed payload. User
# ids are UUID strings (hex + dashes) so this character is unambiguous.
_CATEGORY_SEP = "|"


def make_unsubscribe_token(user_id, category: str = CATEGORY_MARKETING) -> str:
    """Produce a signed token binding a user id (and opt-out category).

    Stable for a given (user, category) across every send, so the same
    address always unsubscribes the same account regardless of which
    email the click came from. ``marketing`` tokens keep the legacy
    bare-user-id payload — byte-identical to tokens minted before
    categories existed.
    """
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"Unknown unsubscribe category: {category!r}")
    signer = Signer(salt=_SIGNER_SALT)
    if category == CATEGORY_MARKETING:
        return signer.sign(str(user_id))
    return signer.sign(f"{user_id}{_CATEGORY_SEP}{category}")


def parse_unsubscribe_token(token: str) -> tuple[str, str] | None:
    """Verify a signed unsubscribe token and split out its category.

    Returns ``(user_id, category)`` on success — legacy payloads without a
    category parse as ``marketing`` — or ``None`` on any failure (bad
    signature, malformed payload, unknown category). The caller treats
    ``None`` as a 404 so an invalid or tampered token leaks nothing.
    """
    signer = Signer(salt=_SIGNER_SALT)
    try:
        payload = signer.unsign(token)
    except BadSignature:
        return None

    if _CATEGORY_SEP not in payload:
        # Legacy (pre-category) token — always marketing.
        return payload, CATEGORY_MARKETING

    user_id, _, category = payload.rpartition(_CATEGORY_SEP)
    if not user_id or category not in _VALID_CATEGORIES:
        return None
    return user_id, category


def verify_unsubscribe_token(token: str) -> str | None:
    """Verify a signed unsubscribe token and return the user id.

    Back-compat shim over :func:`parse_unsubscribe_token` (original
    pre-category contract): returns the user id string on success, ``None``
    on any failure. Callers that need the category use ``parse``.
    """
    parsed = parse_unsubscribe_token(token)
    return parsed[0] if parsed else None
