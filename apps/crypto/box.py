"""box.py — the only surface callers use to encrypt/decrypt content.

Encryption-at-rest Phase 1 (PR3). Ships DARK: nothing outside `apps/crypto`
calls this yet — it exists to be proven correct (round-trip, fail-closed on
tamper, dual-read of legacy plaintext, audited bulk decrypt) before any
Phase 2+ content column is wired through it.

Composition: `codec.py` (envelope + AES-GCM), `cache.py` (per-process DEK
cache, one broker unwrap per cold `(tenant, epoch)`), `kdf.py` (HKDF subkey
derivation), `nolog.py` (`RedactedStr` — decrypted values that refuse to print
themselves), and `audit.py` (fires only for human-initiated
`admin`/`owner_request` reads).

FORMAT CONTRACT (permanent on-disk format as of 2026-07-11): content is
AES-256-GCM-sealed under the WORKING KEY `HKDF(dek, domain)`, NOT the raw DEK.
`domain` defaults to `content-v1` (directive §3.1's `K_content`) and is a
parameter so Phase 3/4 can derive `search-v1` / `map-v1` subkeys off the same
DEK. The envelope layout (`codec.py`) and the AAD are unchanged — the subkey
substitutes for the DEK at the AES-GCM step only. Whatever `domain` seals the
first persisted row is that column's format forever (short of a re-encrypt
migration); today zero ciphertext is persisted, so this is the free moment to
fix it. A raw-DEK-sealed blob will NOT decrypt through this module.
"""

from __future__ import annotations

from . import audit, cache, codec, kdf
from .codec import CryptoError  # noqa: F401  (re-exported — box is the public surface)
from .nolog import RedactedStr

__all__ = ["CryptoError", "RedactedStr", "encrypt", "decrypt", "decrypt_bulk"]

Blob = bytes | bytearray | memoryview | str | None


def encrypt(
    tenant_id: object,
    table: str,
    column: str,
    plaintext: str | None,
    *,
    domain: str = kdf.CONTENT_V1,
) -> bytes | None:
    """Encrypt `plaintext` for `(tenant_id, table, column)`.

    - `None` -> `None` (nothing to store).
    - `""` -> `b""` — a real sealed envelope for an empty string is pure
      waste (~30+ bytes to say "nothing"); `b""` is an unambiguous sentinel,
      distinguishable from any real envelope (always >= 15 bytes) and from
      legacy plaintext (which would be a non-empty `str`).
    - Anything else -> a sealed envelope (see `codec.py`) under the working key
      `HKDF(dek, domain)` at epoch 0 — Phase 1 has no rotation yet, so
      encryption always targets the current (only) epoch. `domain` defaults to
      `content-v1` (directive §3.1); Phase 3/4 pass `search-v1` / `map-v1` for
      their own subkeys. This working-key derivation is the PERMANENT on-disk
      format — see the module docstring.
    """
    if plaintext is None:
        return None
    if plaintext == "":
        return b""

    dek = cache.get_dek(tenant_id, 0)
    working_key = kdf.subkey(dek, domain)
    aad = codec.build_aad(tenant_id, table, column)
    return codec.seal(working_key, 0, aad, plaintext.encode("utf-8"))


def _decrypt_one(tenant_id: object, table: str, column: str, blob: Blob, *, domain: str) -> RedactedStr | None:
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
        if blob[0] != codec.MARKER:
            # A `bytea` `_enc` column only ever holds a 0x01 envelope, b"",
            # or NULL — legacy plaintext lives in the OLD `str` TextField and
            # arrives above via `isinstance(str)`. There is NO legitimate
            # producer of unmarked non-empty bytes here, so treating them as
            # "legacy verbatim" would be a fail-OPEN auth bypass: bytes planted
            # in an `_enc` column would read back as attacker-chosen plaintext
            # with no key and no GCM check. Fail closed instead.
            raise codec.CryptoError("unmarked non-empty bytes in a ciphertext column — refusing to read")
        dek_epoch = codec.unpack(blob)[0]
        try:
            dek = cache.get_dek(tenant_id, dek_epoch)
        except Exception as exc:
            # An envelope naming an epoch with no TenantDek row, or a purged/
            # unreachable KEK, surfaces as TenantDek.DoesNotExist / a broker
            # LookupError. decrypt()'s contract is RedactedStr | None | raise
            # CryptoError — so normalize these to CryptoError rather than
            # letting a raw DB/broker exception escape the crypto boundary.
            raise codec.CryptoError(f"cannot resolve DEK for epoch {dek_epoch}: {type(exc).__name__}") from exc
        working_key = kdf.subkey(dek, domain)
        aad = codec.build_aad(tenant_id, table, column)
        plaintext = codec.open_envelope(working_key, aad, blob)
        return RedactedStr(plaintext.decode("utf-8"))

    # Unexpected type — fail soft into the legacy/verbatim contract rather
    # than raising on a shape we didn't anticipate.
    return RedactedStr(str(blob))


def decrypt(
    tenant_id: object,
    table: str,
    column: str,
    blob: Blob,
    *,
    domain: str = kdf.CONTENT_V1,
) -> RedactedStr | None:
    """Dual-read decrypt of one stored value. ALWAYS returns a `RedactedStr` (or `None`).

    - `blob is None` -> `None`.
    - `blob == b""` -> `RedactedStr("")`.
    - `blob` is bytes/bytearray/memoryview starting with the `0x01` marker
      -> AES-GCM decrypted under the working key `HKDF(dek, domain)` for the
      epoch named in the envelope header, AAD-bound to `(tenant_id, table,
      column)`. `domain` MUST match the one that encrypted the value (defaults
      to `content-v1`); a mismatch fails closed at the GCM tag check.
    - legacy plaintext `str` (from a not-yet-migrated text column) -> returned
      verbatim, wrapped in `RedactedStr`. This is the ONLY dual-read path.
    - non-empty bytes/bytearray/memoryview WITHOUT the marker -> `CryptoError`.
      A `bytea` `_enc` column never legitimately holds unmarked bytes, so
      reading them verbatim would be a fail-open auth bypass (planted bytes
      returning as attacker-chosen plaintext with no key/GCM check).

    Fails closed: a tampered/mismatched envelope, unmarked ciphertext-column
    bytes, or an unresolvable DEK (missing/ purged KEK) all raise
    `CryptoError` — never garbage or partial plaintext. Caller must call
    `.reveal()` on the result to see the real string — see `nolog.RedactedStr`.

    Emits ONE decrypt-audit event (`row_count=1`) via the ambient principal
    set by `audit.set_principal()` — silent unless that principal is
    `admin`/`owner_request`.
    """
    result = _decrypt_one(tenant_id, table, column, blob, domain=domain)
    audit.emit(tenant_id, table, column, row_count=1)
    return result


def decrypt_bulk(
    tenant_id: object,
    table: str,
    column: str,
    blobs: list[Blob],
    *,
    principal: str = "system",
    domain: str = kdf.CONTENT_V1,
) -> list[RedactedStr | None]:
    """Dual-read decrypt of many stored values from the same `(tenant, table, column)`.

    Same per-item dual-read contract as `decrypt`. Because every item shares
    a `(tenant_id, epoch)` DEK lookup and `cache.get_dek` memoizes per
    process, decrypting N envelopes here costs exactly ONE broker unwrap for
    the whole batch (the first item's cache miss; every subsequent item —
    including different rows at the same epoch — is a cache hit).

    `principal` attributes THIS call's single audit event (`row_count=len(blobs)`)
    — silent unless `principal` is `admin`/`owner_request`. It is passed as a
    one-shot `principal_override` and does NOT mutate the shared ambient
    `_PRINCIPAL` ContextVar: on a reused worker thread that would leak into
    the next request (a later `system` decrypt false-audited as `admin`, or a
    bulk defaulting to `system` silencing a genuinely-ambient `admin` read).
    Defaults to `"system"` (silent) since most bulk-decrypt call sites are the
    service itself (rendering a feed, running a cron), not a human reading
    through the admin console.
    """
    results = [_decrypt_one(tenant_id, table, column, blob, domain=domain) for blob in blobs]
    audit.emit(tenant_id, table, column, row_count=len(blobs), principal_override=principal)
    return results
