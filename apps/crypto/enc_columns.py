"""Shared record type for a column enrolled in encryption-at-rest.

Phase 2 shipped chat with a hand-rolled pair of AAD 2-tuples in
``apps/router/enc_columns.py``. Phase 3 expands to five more apps (journal,
lessons, insights, core, fuel) with 40+ columns, so the per-app constants
modules need one shared vocabulary instead of ad-hoc tuples:

  * The **AAD 2-tuple** ``(table, column)`` is what ``apps.crypto.box.encrypt`` /
    ``decrypt`` bind every ciphertext to — byte-identical at every encrypt and
    decrypt site, forever (a drift silently makes prior rows fail the GCM auth
    check — the one permanent-data-loss vector, directive red-team #1). It is the
    *logical* plaintext column name, never the ``_enc`` sidecar.
  * The **(model, value_field, enc_field)** triple is what the later ladder PRs
    consume: the dual-write writers, the ``encrypt_*_history`` backfill, the
    ``read_encrypted_*`` dual-read helper, and the generalized completeness
    predicate (``count_unsealed_rows(tenant, columns)`` — Phase-3 plan §3.3).

``EncColumn`` carries both in one immutable record so a column is declared
exactly once per app. ``model`` is an ``"app_label.ModelName"`` string resolved
lazily via ``django.apps.apps.get_model`` — the constants modules stay free of
Django-model imports (importable with no app registry, no circular-import risk).

``table`` is hard-coded here, NOT derived from ``Model._meta.db_table``: the AAD
must be a frozen on-disk constant, so a future ``db_table`` rename must never
silently re-key existing ciphertext. Every ``table`` string is verified against
the model's ``Meta.db_table`` when the record is written and must never change.
"""

from __future__ import annotations

from typing import NamedTuple


class EncColumn(NamedTuple):
    """One column enrolled in encryption-at-rest (Phase 3+)."""

    model: str
    """``"app_label.ModelName"`` — resolve with ``django.apps.apps.get_model``."""

    value_field: str
    """Plaintext column / logical AAD column (e.g. ``"title"``). NEVER the sidecar."""

    enc_field: str
    """The ``bytea`` sidecar that stores the sealed envelope (e.g. ``"title_enc"``)."""

    table: str
    """The model's ``Meta.db_table`` — the frozen AAD table string."""

    is_json: bool = False
    """True when the plaintext is a ``JSONField``. The dual-write/backfill path
    seals ``json.dumps(value)`` and the read path ``json.loads`` the decrypted
    string (Phase-3 plan §1.5). Recorded here so writers and the CI guard's
    JSON key-path detection share one source of truth for which columns are JSON."""

    @property
    def aad(self) -> tuple[str, str]:
        """The ``(table, column)`` AAD 2-tuple — ``box.encrypt(tid, *col.aad, value)``."""
        return (self.table, self.value_field)
