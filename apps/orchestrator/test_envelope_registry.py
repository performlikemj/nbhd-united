"""Tests for the envelope registry.

Covers registration mechanics (decorator, ordering, uniqueness), the
universal post_save/post_delete handler that triggers ``push_user_md``,
and tenant-id resolution from various model shapes. Render-loop
integration is exercised by ``test_cron_envelope.RenderManagedRegionTest``
since the production registry is already populated at app boot.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from apps.orchestrator.envelope_registry import (
    _reset_registry_for_tests,
    _resolve_tenant_id,
    _universal_refresh_receiver,
    all_sections,
    register_section,
)
from apps.tenants.services import create_tenant


class RegistryRegistrationTest(TestCase):
    def setUp(self):
        # Snapshot real registry, reset for isolation, restore in tearDown.
        from apps.orchestrator import envelope_registry

        self._snapshot = list(envelope_registry._REGISTRY)
        _reset_registry_for_tests()

    def tearDown(self):
        from apps.orchestrator import envelope_registry

        _reset_registry_for_tests()
        envelope_registry._REGISTRY.extend(self._snapshot)

    def test_register_adds_to_registry(self):
        @register_section(
            key="alpha",
            heading="## Alpha",
            enabled=lambda t: True,
            refresh_on=(),
            order=10,
        )
        def _alpha(_t):
            return "body-a"

        sections = all_sections()
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].key, "alpha")
        self.assertEqual(sections[0].heading, "## Alpha")
        # Decorator returns the original function unchanged.
        self.assertEqual(_alpha("ignored"), "body-a")

    def test_sorted_by_order_ascending(self):
        @register_section(key="z", heading="## Z", enabled=lambda t: True, order=300)
        def _z(_t):
            return "z"

        @register_section(key="a", heading="## A", enabled=lambda t: True, order=100)
        def _a(_t):
            return "a"

        @register_section(key="m", heading="## M", enabled=lambda t: True, order=200)
        def _m(_t):
            return "m"

        keys = [s.key for s in all_sections()]
        self.assertEqual(keys, ["a", "m", "z"])

    def test_duplicate_key_raises(self):
        @register_section(key="dup", heading="## D", enabled=lambda t: True, order=10)
        def _first(_t):
            return ""

        with self.assertRaises(ValueError) as ctx:

            @register_section(key="dup", heading="## D2", enabled=lambda t: True, order=20)
            def _second(_t):
                return ""

        self.assertIn("dup", str(ctx.exception))

    def test_section_is_frozen_dataclass(self):
        @register_section(key="frozen", heading="## F", enabled=lambda t: True, order=10)
        def _f(_t):
            return ""

        section = all_sections()[0]
        with self.assertRaises(Exception):  # FrozenInstanceError
            section.order = 999

    def test_section_render_callable_is_the_decorated_function(self):
        @register_section(key="callable", heading="## C", enabled=lambda t: True, order=10)
        def _r(tenant):
            return f"hello {tenant}"

        section = all_sections()[0]
        self.assertEqual(section.render("world"), "hello world")


class TenantIdResolutionTest(TestCase):
    """The universal receiver pulls tenant_id from various model shapes."""

    def test_resolves_via_direct_fk_column(self):
        instance = mock.MagicMock()
        instance.tenant_id = "abc-123"
        self.assertEqual(_resolve_tenant_id(instance), "abc-123")

    def test_resolves_via_relation_when_fk_column_missing(self):
        instance = mock.MagicMock(spec=["tenant"])
        instance.tenant = mock.MagicMock()
        instance.tenant.id = "xyz-789"
        self.assertEqual(_resolve_tenant_id(instance), "xyz-789")

    def test_returns_none_when_no_tenant_attached(self):
        # Use a plain object so getattr returns None for both attrs.
        class _Bare:
            pass

        self.assertEqual(_resolve_tenant_id(_Bare()), None)


class UniversalRefreshReceiverTest(TestCase):
    """post_save / post_delete handler triggers push_user_md after commit."""

    def setUp(self):
        cache.clear()
        self.tenant = create_tenant(display_name="Reg", telegram_chat_id=920000)

    def tearDown(self):
        cache.clear()

    @mock.patch("apps.orchestrator.envelope_registry.threading.Thread")
    def test_no_op_when_instance_has_no_tenant(self, mock_thread):
        class _Orphan:
            pass

        with self.captureOnCommitCallbacks(execute=True):
            _universal_refresh_receiver(sender=_Orphan, instance=_Orphan())
        mock_thread.assert_not_called()

    @mock.patch("apps.orchestrator.workspace_envelope.upload_workspace_file")
    @mock.patch("apps.orchestrator.workspace_envelope.download_workspace_file")
    def test_schedules_push_for_real_tenant_save(self, mock_download, mock_upload):
        """A real instance with tenant_id triggers a debounced push."""
        from apps.lessons.models import Lesson

        mock_download.return_value = None
        with self.captureOnCommitCallbacks(execute=True):
            Lesson.objects.create(
                tenant=self.tenant,
                text="A meaningful lesson",
                source_type="conversation",
                status="approved",
            )
        # The receiver schedules a daemon-thread push; in the test runner
        # the thread runs and ends up calling upload_workspace_file at
        # least once (cache may debounce subsequent attempts).
        self.assertGreaterEqual(mock_upload.call_count, 1)
