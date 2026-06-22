"""Audit A13 – FA-0173-P1: cache-tag bump coverage for PendingExtraction receiver.

The _bump_on_pending_extraction receiver in apps/dashboard/receivers.py was
added as part of the FA-0173 fix but no corresponding test was added to
apps/dashboard/test_receivers.py.  This file closes that gap by mirroring the
pattern used for the four pre-existing receivers (AssistantInsight, UserVoicePref,
Goal, Document).
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from apps.common.cache import get_tag_version
from apps.journal.models import PendingExtraction
from apps.tenants.services import create_tenant


class PendingExtractionCacheBumpTests(TestCase):
    """Verify that _bump_on_pending_extraction fires the dashboard cache bump."""

    def setUp(self):
        self.tenant = create_tenant(display_name="A13CacheBump", telegram_chat_id=913913)
        self.baseline = get_tag_version(self.tenant.id, "dashboard")

    def _v(self) -> int:
        return get_tag_version(self.tenant.id, "dashboard")

    def _make_extraction(self, **overrides):
        defaults = dict(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.TASK,
            text="Write unit tests for the cache receiver.",
            expires_at=timezone.now() + timezone.timedelta(days=7),
            status=PendingExtraction.Status.PENDING,
        )
        defaults.update(overrides)
        return PendingExtraction.objects.create(**defaults)

    # --- save (create) ---------------------------------------------------

    def test_pending_extraction_save_bumps_tag(self):
        """Creating a PendingExtraction must bump the dashboard cache tag."""
        self._make_extraction()
        self.assertGreater(self._v(), self.baseline)

    # --- status change (mirrors ExtractionDismissView write path) --------

    def test_pending_extraction_status_change_bumps_tag(self):
        """Changing status to DISMISSED (the ExtractionDismissView path) must bump."""
        extraction = self._make_extraction()
        before = self._v()
        extraction.status = PendingExtraction.Status.DISMISSED
        extraction.save()
        self.assertGreater(self._v(), before)

    def test_pending_extraction_approve_bumps_tag(self):
        """Changing status to APPROVED (the typed-task approve path) must bump."""
        extraction = self._make_extraction()
        before = self._v()
        extraction.status = PendingExtraction.Status.APPROVED
        extraction.save()
        self.assertGreater(self._v(), before)

    # --- delete ----------------------------------------------------------

    def test_pending_extraction_delete_bumps_tag(self):
        """Deleting a PendingExtraction must bump the dashboard cache tag."""
        extraction = self._make_extraction()
        before = self._v()
        extraction.delete()
        self.assertGreater(self._v(), before)
