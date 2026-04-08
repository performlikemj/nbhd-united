"""Tests for workspace routing logic.

Verifies that messages are routed to the correct OpenClaw session based on
tenant workspace state. Critical edge cases:
- Tenants with no workspaces fall back to legacy behavior
- Default workspace uses bare user_param (preserves existing session)
- Non-default workspace appends `:ws:{slug}` suffix
- Within-session routing skips classification
- New-session routing uses embedding similarity
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.journal.models import Workspace
from apps.router.workspace_routing import (
    _build_user_param,
    _get_default,
    _is_new_session,
    resolve_workspace_routing,
    update_active_workspace,
)
from apps.tenants.models import Tenant, User


def _make_tenant(last_msg_minutes_ago=None):
    user = User.objects.create_user(
        username=f"wsr{timezone.now().timestamp()}", password="pass"
    )
    tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
    if last_msg_minutes_ago is not None:
        tenant.last_message_at = timezone.now() - timedelta(minutes=last_msg_minutes_ago)
        tenant.save(update_fields=["last_message_at"])
    return tenant


def _make_workspace(tenant, name, slug, *, is_default=False, description=""):
    return Workspace.objects.create(
        tenant=tenant,
        name=name,
        slug=slug,
        is_default=is_default,
        description=description,
    )


class TestNoWorkspaces(TestCase):
    """Tenants with no workspaces should hit the legacy code path."""

    def test_returns_base_user_id_unchanged(self):
        tenant = _make_tenant()
        user_param, ws, transitioned = resolve_workspace_routing(
            tenant, "8078236299", "any message"
        )
        self.assertEqual(user_param, "8078236299")
        self.assertIsNone(ws)
        self.assertFalse(transitioned)


class TestDefaultWorkspaceParam(TestCase):
    """Default workspace must use the bare user_param (no suffix) to preserve
    the legacy session for users who had history before workspaces existed."""

    def test_default_workspace_no_suffix(self):
        tenant = _make_tenant(last_msg_minutes_ago=5)
        general = _make_workspace(tenant, "General", "general", is_default=True)
        tenant.active_workspace = general
        tenant.save(update_fields=["active_workspace"])

        user_param, ws, _ = resolve_workspace_routing(
            tenant, "8078236299", "anything"
        )
        self.assertEqual(user_param, "8078236299")
        self.assertEqual(ws.id, general.id)

    def test_non_default_workspace_has_suffix(self):
        tenant = _make_tenant(last_msg_minutes_ago=5)
        _make_workspace(tenant, "General", "general", is_default=True)
        work = _make_workspace(tenant, "Work", "work")
        tenant.active_workspace = work
        tenant.save(update_fields=["active_workspace"])

        user_param, ws, _ = resolve_workspace_routing(
            tenant, "8078236299", "budget question"
        )
        self.assertEqual(user_param, "8078236299:ws:work")
        self.assertEqual(ws.id, work.id)


class TestWithinSessionRouting(TestCase):
    """Within an active session (<30 min gap), use the active workspace
    without classifying the message."""

    def test_uses_active_workspace_without_classification(self):
        tenant = _make_tenant(last_msg_minutes_ago=5)
        general = _make_workspace(tenant, "General", "general", is_default=True)
        work = _make_workspace(tenant, "Work", "work", description="budget meetings")
        tenant.active_workspace = work
        tenant.save(update_fields=["active_workspace"])

        with patch("apps.router.workspace_routing._classify_message") as classify_mock:
            user_param, ws, transitioned = resolve_workspace_routing(
                tenant, "8078236299", "what should I cook for dinner?"
            )
            classify_mock.assert_not_called()

        self.assertEqual(user_param, "8078236299:ws:work")
        self.assertEqual(ws.id, work.id)
        self.assertFalse(transitioned)


class TestNewSessionClassification(TestCase):
    """On a new session (>30 min gap), classify the message and pick the
    best-matching workspace by embedding similarity."""

    def test_classification_called_on_new_session(self):
        tenant = _make_tenant(last_msg_minutes_ago=60)
        general = _make_workspace(tenant, "General", "general", is_default=True)
        work = _make_workspace(tenant, "Work", "work")
        tenant.active_workspace = general
        tenant.save(update_fields=["active_workspace"])

        with patch("apps.router.workspace_routing._classify_message", return_value=work) as classify_mock:
            user_param, ws, transitioned = resolve_workspace_routing(
                tenant, "8078236299", "Q3 budget status please"
            )
            classify_mock.assert_called_once()

        self.assertEqual(user_param, "8078236299:ws:work")
        self.assertEqual(ws.id, work.id)
        self.assertTrue(transitioned)  # Switched from general to work

    def test_classification_returns_none_falls_back_to_active(self):
        tenant = _make_tenant(last_msg_minutes_ago=60)
        general = _make_workspace(tenant, "General", "general", is_default=True)
        tenant.active_workspace = general
        tenant.save(update_fields=["active_workspace"])

        with patch("apps.router.workspace_routing._classify_message", return_value=None):
            user_param, ws, transitioned = resolve_workspace_routing(
                tenant, "8078236299", "ambiguous message"
            )

        # Falls back to active (general), no transition since same workspace
        self.assertEqual(user_param, "8078236299")  # general → no suffix
        self.assertEqual(ws.id, general.id)
        self.assertFalse(transitioned)


class TestNoActiveWorkspaceFallback(TestCase):
    """Tenants with workspaces but no active_workspace set should fall back
    to the default workspace, then to the first workspace."""

    def test_falls_back_to_default(self):
        tenant = _make_tenant(last_msg_minutes_ago=5)
        general = _make_workspace(tenant, "General", "general", is_default=True)
        _make_workspace(tenant, "Work", "work")
        # active_workspace deliberately not set

        user_param, ws, _ = resolve_workspace_routing(
            tenant, "8078236299", "hi"
        )
        self.assertEqual(user_param, "8078236299")
        self.assertEqual(ws.id, general.id)

    def test_falls_back_to_first_when_no_default(self):
        tenant = _make_tenant(last_msg_minutes_ago=5)
        first = _make_workspace(tenant, "Work", "work")
        _make_workspace(tenant, "Personal", "personal")

        user_param, ws, _ = resolve_workspace_routing(
            tenant, "8078236299", "hi"
        )
        # Without a default, the first workspace gets the suffix
        self.assertEqual(user_param, "8078236299:ws:work")
        self.assertEqual(ws.id, first.id)


class TestUpdateActiveWorkspace(TestCase):
    """update_active_workspace persists the routing decision."""

    def test_updates_tenant_and_workspace(self):
        tenant = _make_tenant()
        work = _make_workspace(tenant, "Work", "work")

        update_active_workspace(tenant, work)

        tenant.refresh_from_db()
        work.refresh_from_db()
        self.assertEqual(tenant.active_workspace_id, work.id)
        self.assertIsNotNone(work.last_used_at)

    def test_handles_none_workspace(self):
        tenant = _make_tenant()
        # Should not crash
        update_active_workspace(tenant, None)
