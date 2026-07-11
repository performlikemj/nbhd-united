"""Read-side dual-read policy for the encrypted chat columns (Phase 2 PR-4).

``apps.crypto.box.decrypt`` already dual-reads any stored value — a ``0x01``
envelope decrypts, a legacy ``str`` returns verbatim, ``b""`` returns ``""`` — so
the ONLY policy layered here is the per-tenant read flag
``Tenant.read_encrypted_chat``: when it is ON, route the stored value (prefer the
``_enc`` sidecar, fall back to legacy plaintext for a not-yet-backfilled row)
through ``box.decrypt``; when it is OFF, serve the legacy plaintext untouched.
AAD ``(table, column)`` tuples come from ``apps.router.enc_columns`` — never
hand-typed (plan risk #6).

Every value returned is a ``RedactedStr``; the caller MUST ``.reveal()`` it at
the egress seam (the RedactedStr CI guard + the .reveal()-at-egress convention —
a decrypted value must never reach a Response/json buffer un-revealed).

Two entry points, matching the audit shape (plan §5 PR-4 + amendment b):
  * ``read_value`` — ONE row. ``box.decrypt`` audits under the AMBIENT principal
    (``owner_request``, set at the DRF auth boundary by #1129 for owner API
    reads; ``system`` / ``system_cron`` and silent elsewhere). NO per-view
    ``set_principal``.
  * ``read_values_bulk`` — MANY rows sharing ``(tenant, aad)``: ONE
    ``box.decrypt_bulk`` → ONE audit event (``row_count=N``) under an explicit
    ``principal`` (owner API pages pass ``owner_request``; the system digest
    builder passes ``system``, staying silent).
"""

from __future__ import annotations

from apps.crypto.nolog import RedactedStr


def reads_encrypted(tenant) -> bool:
    """True when this tenant should serve chat content from the ``_enc`` columns."""
    return bool(getattr(tenant, "read_encrypted_chat", False))


def read_value(tenant, aad: tuple[str, str], enc_blob, legacy_value: str) -> RedactedStr:
    """Single-row dual-read -> ``RedactedStr``. Audits once under the ambient principal."""
    if not reads_encrypted(tenant):
        return RedactedStr(legacy_value)
    from apps.crypto import box

    stored = enc_blob if enc_blob is not None else legacy_value
    return box.decrypt(tenant.id, aad[0], aad[1], stored)


def read_values_bulk(
    tenant,
    aad: tuple[str, str],
    pairs: list[tuple[object, str]],
    *,
    principal: str = "owner_request",
) -> list[RedactedStr]:
    """Bulk dual-read for rows sharing ``(tenant, aad)`` -> ``list[RedactedStr]``.

    ONE ``box.decrypt_bulk`` -> ONE audit event (``row_count=N``) under
    ``principal``. ``pairs`` is ``[(enc_blob, legacy_value), ...]`` in row order.
    """
    if not reads_encrypted(tenant):
        return [RedactedStr(legacy) for _, legacy in pairs]
    from apps.crypto import box

    blobs = [enc if enc is not None else legacy for enc, legacy in pairs]
    return box.decrypt_bulk(tenant.id, aad[0], aad[1], blobs, principal=principal)
