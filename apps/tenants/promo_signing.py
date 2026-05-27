"""Per-user HMAC token signing for promo redemption URLs.

Each promo email contains a redemption link of the shape::

    {FRONTEND_URL}/promo/redeem/?code=<campaign_code>&token=<signed_token>

``token`` is an HMAC-signed string (signed by Django's ``SECRET_KEY``)
encoding both the campaign code and the user id. The redemption view
verifies that:

  1. The signature is valid (signed by us, untampered).
  2. The campaign code in the token matches the ``?code=`` URL param.
  3. The campaign's ``valid_until`` deadline hasn't passed.

We deliberately do *not* rely on ``TimestampSigner``'s ``max_age``
parameter for expiry — the campaign's deadline drives expiry, so a
token signed at send time stays valid up to ``valid_until`` regardless
of when it was minted. This lets a send-campaign re-run (e.g. a retry
after a partial Mailgun outage) produce tokens with the same expiry as
the originals.
"""

from __future__ import annotations

from django.core.signing import BadSignature, Signer

# Distinct salt so promo tokens can't be substituted for any other
# signed payload elsewhere in the codebase.
_SIGNER_SALT = "nbhd.promos.v1"


def make_promo_token(campaign_code: str, user_id) -> str:
    """Produce a signed token binding (campaign_code, user_id).

    Format: ``<campaign_code>:<user_id>`` signed with Django's
    ``SECRET_KEY``. Stable across re-runs of the send command for the
    same (campaign, user) — useful when a partial send needs to be
    retried, since users who already received the email won't see a
    different link.
    """
    signer = Signer(salt=_SIGNER_SALT)
    return signer.sign(f"{campaign_code}:{user_id}")


def verify_promo_token(expected_campaign_code: str, token: str) -> str | None:
    """Verify a signed promo token. Returns the user id (as the raw
    string the URL carried) on success, ``None`` on any failure
    (bad signature, mismatched campaign code, malformed payload).

    Mismatched campaign code is treated as failure rather than silent
    pass: it indicates the token from one campaign is being used in
    another campaign's URL, which would be a tampering or copy-paste
    error.
    """
    signer = Signer(salt=_SIGNER_SALT)
    try:
        raw = signer.unsign(token)
    except BadSignature:
        return None

    try:
        token_code, user_id = raw.split(":", 1)
    except ValueError:
        return None

    if token_code != expected_campaign_code:
        return None

    return user_id
