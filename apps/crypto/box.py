"""box.py — the only surface callers use to encrypt/decrypt content.

Encryption-at-rest Phase 1 (PR3). Ships DARK: nothing outside `apps/crypto`
calls this yet — it exists to be proven correct (round-trip, fail-closed on
tamper, dual-read of legacy plaintext, audited bulk decrypt) before any
Phase 2+ content column is wired through it.

Composition: `codec.py` (envelope + AES-GCM), `cache.py` (per-process DEK
cache, one broker unwrap per cold `(tenant, epoch)`), `nolog.py`
(`RedactedStr` — decrypted values that refuse to print themselves), and
`audit.py` (fires only for human-initiated `admin`/`owner_request` reads).
"""

from __future__ import annotations

from . import audit, cache, codec
from .codec import CryptoError  # noqa: F401  (re-exported — box is the public surface)
from .nolog import RedactedStr

__all__ = ["CryptoError", "RedactedStr", "encrypt", "decrypt", "decrypt_bulk"]

Blob = bytes | bytearray | memoryview | str | None


def encrypt(tenant_id: object, table: str, column: str, plaintext: str | None) -> bytes | None:
    """Encrypt `plaintext` for `(tenant_id, table, column)`.

    - `None` -> `None` (nothing to store).
    - `""` -> `b""` — a real sealed envelope for an empty string is pure
      waste (~30+ bytes to say "nothing"); `b""` is an unambiguous sentinel,
      distinguishable from any real envelope (always >= 15 bytes) and from
      legacy plaintext (which would be a non-empty `str`).
    - Anything else -> a sealed envelope (see `codec.py`) under the tenant's
      DEK at epoch 0 — Phase 1 has no rotation yet, so encryption always
      targets the current (only) epoch.
    """
    if plaintext is None:
        return None
    if plaintext == "":
        return b""

    dek = cache.get_dek(tenant_id, 0)
    aad = codec.build_aad(tenant_id, table, column)
    return codec.seal(dek, 0, aad, plaintext.encode("utf-8"))


def _decrypt_one(tenant_id: object, table: str, column: str, blob: Blob) -> RedactedStr | None:
    """Dual-read a single stored value. See `decrypt`'s docstring for the contract."""
    if blob is None:
        return None

    if isinstance(blob, str):
        # Legacy plaintext, read straight from a not-yet-migrated text
        # column — verbatim, no marker to check.
        return RedactedStr(blob)

    if isinstance(blob, memoryview):
        blob = bytes(blob)
    if isinstance(blob, (bytes, bytearray)):
        blob = bytes(blob)
        if blob == b"":
            return RedactedStr("")
        if blob[0] == codec.MARKER:
            dek_epoch = codec.unpack(blob)[0]
            dek = cache.get_dek(tenant_id, dek_epoch)
            aad = codec.build_aad(tenant_id, table, column)
            plaintext = codec.open_envelope(dek, aad, blob)
            return RedactedStr(plaintext.decode("utf-8"))
        # Legacy bytes with no marker: verbatim pass-through, decoded as
        # text (this is what "legacy plaintext bytes" actually contain).
        return RedactedStr(blob.decode("utf-8"))

    # Unexpected type — fail soft into the legacy/verbatim contract rather
    # than raising on a shape we didn't anticipate.
    return RedactedStr(str(blob))


def decrypt(tenant_id: object, table: str, column: str, blob: Blob) -> RedactedStr | None:
    """Dual-read decrypt of one stored value. ALWAYS returns a `RedactedStr` (or `None`).

    - `blob is None` -> `None`.
    - `blob == b""` -> `RedactedStr("")`.
    - `blob` is bytes/bytearray/memoryview starting with the `0x01` marker
      -> AES-GCM decrypted under the DEK for the epoch named in the envelope
      header, AAD-bound to `(tenant_id, table, column)`.
    - anything else (legacy plaintext `str`, or bytes/memoryview with no
      marker) -> returned verbatim, wrapped in `RedactedStr`.

    Fails closed: a tampered/mismatched envelope raises `CryptoError`, never
    a garbage or partial plaintext. Caller must call `.reveal()` on the
    result to see the real string — see `nolog.RedactedStr`.

    Emits ONE decrypt-audit event (`row_count=1`) via the ambient principal
    set by `audit.set_principal()` — silent unless that principal is
    `admin`/`owner_request`.
    """
    result = _decrypt_one(tenant_id, table, column, blob)
    audit.emit(tenant_id, table, column, row_count=1)
    return result


def decrypt_bulk(
    tenant_id: object,
    table: str,
    column: str,
    blobs: list[Blob],
    *,
    principal: str = "system",
) -> list[RedactedStr | None]:
    """Dual-read decrypt of many stored values from the same `(tenant, table, column)`.

    Same per-item dual-read contract as `decrypt`. Because every item shares
    a `(tenant_id, epoch)` DEK lookup and `cache.get_dek` memoizes per
    process, decrypting N envelopes here costs exactly ONE broker unwrap for
    the whole batch (the first item's cache miss; every subsequent item —
    including different rows at the same epoch — is a cache hit).

    `principal` sets the ambient decrypt-audit principal for this call (see
    `audit.set_principal`) before emitting ONE audit event covering the
    whole batch (`row_count=len(blobs)`) — silent unless `principal` is
    `admin`/`owner_request`. Defaults to `"system"` (silent) since most
    bulk-decrypt call sites are the service itself (rendering a feed,
    running a cron), not a human reading through the admin console.
    """
    results = [_decrypt_one(tenant_id, table, column, blob) for blob in blobs]
    audit.set_principal(principal)
    audit.emit(tenant_id, table, column, row_count=len(blobs))
    return results
