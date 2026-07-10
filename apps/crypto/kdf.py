"""HKDF-SHA256 subkey derivation — encryption-at-rest Phase 1 (PR3).

Lets a single tenant DEK serve multiple purposes (content ciphertext, a
future PII-map key, a future blind-index search key) without ever using the
same raw key material for two different jobs. Each purpose gets its own
`info` string; HKDF with a fixed empty salt and the DEK as input key material
deterministically derives an independent-looking 32-byte subkey per info
string. Phase 1 ships the function and all three known infos; only
`content-v1` has a consumer today (box.py encrypts directly under the DEK,
not yet under `subkey(dek, CONTENT_V1)` — that wiring is a later phase).
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SUBKEY_LEN = 32

CONTENT_V1 = "content-v1"
MAP_V1 = "map-v1"
SEARCH_V1 = "search-v1"


def subkey(dek: bytes, info: str) -> bytes:
    """Derive a 32-byte subkey from `dek` for a given purpose (`info`).

    HKDF-SHA256, salt=b"" (fixed — the DEK itself is already high-entropy
    random key material, so a salt buys nothing here), info=info.encode().
    """
    hkdf = HKDF(algorithm=hashes.SHA256(), length=SUBKEY_LEN, salt=b"", info=info.encode("utf-8"))
    return hkdf.derive(dek)
