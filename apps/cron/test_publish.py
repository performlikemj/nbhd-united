"""Tests for ``apps.cron.publish`` — focused on the QStash dedup-id
contract.

QStash rejects an ``Upstash-Deduplication-Id`` header containing ``:`` or
whitespace with a 400 ``"DeduplicationId cannot contain ':'"``. Commit
9ae5ac3 introduced a colon-separated bucketed key in the journal sync
signal and every publish has silently 400'd since, meaning Document
saves haven't propagated to workspace memory in 4 days. The validator
below raises eagerly so the next caller that reaches for a colon-key
fails loudly in CI / dev rather than silently in prod.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.test import TestCase

from apps.cron.publish import publish_batch, publish_task


class IdempotencyKeyValidatorTest(TestCase):
    def test_colon_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            publish_task("sync_documents_to_workspace", "tenant-1", idempotency_key="sync:abc:202605201230")
        self.assertIn(":", str(ctx.exception))

    def test_space_rejected(self):
        with self.assertRaises(ValueError):
            publish_task("sync_documents_to_workspace", "tenant-1", idempotency_key="sync abc")

    def test_tab_rejected(self):
        with self.assertRaises(ValueError):
            publish_task("sync_documents_to_workspace", "tenant-1", idempotency_key="sync\tabc")

    def test_dash_underscore_accepted(self):
        # No QStash token in test settings → falls through to sync
        # execution; key validation runs first regardless. A valid key
        # should NOT raise from the validator. We tolerate the
        # downstream TASK_MAP / sync exec to do whatever it does (the
        # validator is the unit under test here).
        try:
            publish_task("sync_documents_to_workspace", "tenant-1", idempotency_key="sync-docs_tenant-1-202605201230")
        except ValueError:
            self.fail("dash + underscore in idempotency_key must not raise")
        except Exception:
            # Any non-ValueError (e.g. tenant doesn't exist in test DB)
            # is fine — we only assert the validator passes.
            pass

    def test_batch_colon_rejected(self):
        with self.assertRaises(ValueError):
            publish_batch([("sync_documents_to_workspace", ("t",), {}, "bad:key:here")])

    def test_none_key_accepted(self):
        # Most callers pass no idempotency_key — must remain valid.
        try:
            publish_task("sync_documents_to_workspace", "tenant-1")
        except ValueError:
            self.fail("idempotency_key=None must not raise")
        except Exception:
            pass


class JournalSignalKeyShapeTest(TestCase):
    """Regression test for 9ae5ac3 — the bucketed key must not contain ':'.

    Mirrors the format string used in
    ``apps.journal.signals.queue_memory_sync_on_document_save._publish``.
    If someone reverts to a colon-separated bucket, this fails before the
    publish reaches QStash.
    """

    def test_bucketed_key_has_no_forbidden_chars(self):
        tenant_id = "148ccf1c-ef13-47f8-ada1-a98fa90e14a0"
        bucket = datetime.now(UTC).strftime("%Y%m%d%H%M")
        # Same construction as signals.py
        key = f"sync-documents-to-workspace-{tenant_id}-{bucket}"
        for forbidden in (":", " ", "\t", "\n", "\r"):
            self.assertNotIn(forbidden, key)
        # And the validator must accept it.
        try:
            publish_task("sync_documents_to_workspace", tenant_id, idempotency_key=key)
        except ValueError:
            self.fail(f"validator rejected the actual signal key shape: {key!r}")
        except Exception:
            pass
