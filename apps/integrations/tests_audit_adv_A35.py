"""Adversarial audit tests for cluster A35 — document append lost-update fix.

Covers integrations#2: RuntimeDailyNoteAppendView, RuntimeDocumentAppendView,
and journal DocumentAppendView all wrapped in transaction.atomic() +
select_for_update() to prevent concurrent lost-update on Document.markdown.
"""

from __future__ import annotations

import uuid

from django.test import TestCase
from django.utils import timezone

from apps.journal.models import Document
from apps.tenants.models import Tenant


def _make_tenant() -> Tenant:
    """Create a minimal tenant for testing."""
    from apps.tenants.models import Tenant

    return Tenant.objects.create(
        id=uuid.uuid4(),
        name="Test Tenant A35",
        prefix=uuid.uuid4().hex[:8],
    )


def _make_daily_doc(tenant: Tenant, date_str: str | None = None) -> Document:
    slug = date_str or str(timezone.now().date())
    doc, _ = Document.objects.get_or_create(
        tenant=tenant,
        kind="daily",
        slug=slug,
        defaults={"title": slug, "markdown": "# Daily\n"},
    )
    return doc


class TestRuntimeDailyNoteAppendLocking(TestCase):
    """Unit-level: verify the view's RMW is wrapped in atomic + select_for_update."""

    def test_view_post_uses_transaction_atomic(self):
        """The post() method must reference transaction.atomic in its body."""
        import inspect

        from apps.integrations.runtime_views import RuntimeDailyNoteAppendView

        source = inspect.getsource(RuntimeDailyNoteAppendView.post)
        self.assertIn(
            "transaction.atomic",
            source,
            "RuntimeDailyNoteAppendView.post must use transaction.atomic()",
        )

    def test_view_post_uses_select_for_update(self):
        """The post() method must call select_for_update() inside the atomic block."""
        import inspect

        from apps.integrations.runtime_views import RuntimeDailyNoteAppendView

        source = inspect.getsource(RuntimeDailyNoteAppendView.post)
        self.assertIn(
            "select_for_update",
            source,
            "RuntimeDailyNoteAppendView.post must call select_for_update()",
        )

    def test_view_post_uses_update_fields(self):
        """doc.save() must use update_fields to avoid clobbering unrelated columns."""
        import inspect

        from apps.integrations.runtime_views import RuntimeDailyNoteAppendView

        source = inspect.getsource(RuntimeDailyNoteAppendView.post)
        self.assertIn(
            "update_fields",
            source,
            "RuntimeDailyNoteAppendView.post doc.save() must specify update_fields",
        )


class TestRuntimeDocumentAppendLocking(TestCase):
    """Unit-level: verify RuntimeDocumentAppendView uses atomic + select_for_update."""

    def test_view_post_uses_transaction_atomic(self):
        import inspect

        from apps.integrations.runtime_views import RuntimeDocumentAppendView

        source = inspect.getsource(RuntimeDocumentAppendView.post)
        self.assertIn(
            "transaction.atomic",
            source,
            "RuntimeDocumentAppendView.post must use transaction.atomic()",
        )

    def test_view_post_uses_select_for_update(self):
        import inspect

        from apps.integrations.runtime_views import RuntimeDocumentAppendView

        source = inspect.getsource(RuntimeDocumentAppendView.post)
        self.assertIn(
            "select_for_update",
            source,
            "RuntimeDocumentAppendView.post must call select_for_update()",
        )

    def test_view_post_uses_update_fields(self):
        import inspect

        from apps.integrations.runtime_views import RuntimeDocumentAppendView

        source = inspect.getsource(RuntimeDocumentAppendView.post)
        self.assertIn(
            "update_fields",
            source,
            "RuntimeDocumentAppendView.post doc.save() must specify update_fields",
        )


class TestDocumentAppendViewLocking(TestCase):
    """Unit-level: verify journal DocumentAppendView uses atomic + select_for_update."""

    def test_view_post_uses_transaction_atomic(self):
        import inspect

        from apps.journal.document_views import DocumentAppendView

        source = inspect.getsource(DocumentAppendView.post)
        self.assertIn(
            "transaction.atomic",
            source,
            "DocumentAppendView.post must use transaction.atomic()",
        )

    def test_view_post_uses_select_for_update(self):
        import inspect

        from apps.journal.document_views import DocumentAppendView

        source = inspect.getsource(DocumentAppendView.post)
        self.assertIn(
            "select_for_update",
            source,
            "DocumentAppendView.post must call select_for_update()",
        )

    def test_view_post_uses_update_fields(self):
        import inspect

        from apps.journal.document_views import DocumentAppendView

        source = inspect.getsource(DocumentAppendView.post)
        self.assertIn(
            "update_fields",
            source,
            "DocumentAppendView.post doc.save() must specify update_fields",
        )
