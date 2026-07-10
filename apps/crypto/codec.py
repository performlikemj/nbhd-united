"""Envelope codec — encryption-at-rest Phase 1 (PR3).

Byte layout (CONTINUITY_encryption-phase1.md §1 PR3 — EXACT, do not change
without a version bump to the marker byte)::

    byte   0      : 0x01                 alg/version marker.
                    Presence = "this is NBHD ciphertext".
                    Absence = legacy plaintext (dual-read, see box.py).
    bytes  1..2   : dek_epoch, uint16 BE  which tenant_deks row decrypts this
    bytes  3..14  : nonce, 12 random bytes
    bytes 15..    : AES-GCM ciphertext || 16-byte GCM tag

AAD = f"{tenant_id}:{table}:{column}".encode("utf-8") — NO row id, and must
be byte-identical at encrypt time and decrypt time. Binding tenant/table/
column into the AAD (rather than just encrypting the value) means ciphertext
copied from one column, row, or tenant into another fails to authenticate —
that's the point of AAD, not a limitation of it.
"""

from __future__ import annotations

import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MARKER = 0x01
NONCE_LEN = 12
TAG_LEN = 16
HEADER_LEN = 1 + 2 + NONCE_LEN  # marker + epoch + nonce = 15


class CryptoError(Exception):
    """Ciphertext failed to authenticate/decrypt, or the envelope is malformed.

    Fail-closed: raised instead of ever returning garbage, a partially
    decrypted value, or silently falling back to something plaintext-shaped.
    """


def build_aad(tenant_id: object, table: str, column: str) -> bytes:
    """Build the AAD binding a ciphertext to its (tenant, table, column).

    No row id — see module docstring. `tenant_id` is coerced with `str()` so
    a `uuid.UUID` and its string form always produce the same AAD.
    """
    return f"{tenant_id}:{table}:{column}".encode()


def pack(dek_epoch: int, nonce: bytes, ct: bytes) -> bytes:
    """Assemble the envelope from its parts."""
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce must be {NONCE_LEN} bytes, got {len(nonce)}")
    if not (0 <= dek_epoch <= 0xFFFF):
        raise ValueError(f"dek_epoch must fit in a uint16, got {dek_epoch}")
    return bytes([MARKER]) + struct.pack(">H", dek_epoch) + nonce + ct


def unpack(blob: bytes) -> tuple[int, bytes, bytes]:
    """Split a marked envelope into (dek_epoch, nonce, ciphertext||tag).

    Assumes the caller already identified `blob` as an NBHD envelope (dual-
    read discrimination — checking byte0 == MARKER, or blob == b"" — belongs
    in box.py, not here). Any structural violation found here (too short,
    wrong marker) is treated as tampering and raises CryptoError, never a
    silent best-effort parse.
    """
    if len(blob) < HEADER_LEN:
        raise CryptoError(f"envelope too short: {len(blob)} bytes, need >= {HEADER_LEN}")
    if blob[0] != MARKER:
        raise CryptoError(f"unexpected envelope marker byte: {blob[0]!r}")
    dek_epoch = struct.unpack(">H", blob[1:3])[0]
    nonce = blob[3:HEADER_LEN]
    ct = blob[HEADER_LEN:]
    return dek_epoch, nonce, ct


def seal(dek: bytes, dek_epoch: int, aad: bytes, plaintext: bytes) -> bytes:
    """Encrypt `plaintext` under `dek` (AES-256-GCM) and pack it into an envelope.

    A fresh random 12-byte nonce is drawn for every call — DEKs are 32 random
    bytes reused across many rows, so nonce reuse (not key reuse) is the
    actual risk AES-GCM depends on avoiding; `os.urandom` gives a
    cryptographically negligible collision probability at Phase-1 volumes.
    """

    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(dek).encrypt(nonce, plaintext, aad)
    return pack(dek_epoch, nonce, ct)


def open_envelope(dek: bytes, aad: bytes, blob: bytes) -> bytes:
    """Unpack + AES-GCM-decrypt an envelope.

    Raises CryptoError on ANY authentication failure — tampered ciphertext,
    tampered tag, wrong AAD (tenant/table/column mismatch), or the wrong DEK
    (wrong tenant, or a stale/incorrect epoch's key). `cryptography`'s
    `InvalidTag` never leaks partial plaintext; catching-and-re-raising here
    only narrows the exception type for callers.
    """
    _dek_epoch, nonce, ct = unpack(blob)
    try:
        return AESGCM(dek).decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        raise CryptoError("AEAD authentication failed — tampered ciphertext or wrong AAD/DEK") from exc
