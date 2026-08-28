"""Server-enforced confirmation handshakes for outward/destructive tools."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.journal.models import Workspace
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RedditConfirmHandshakeTests(TestCase):
    guidance = (
        "Show this exact draft to the user and wait for an explicit yes; then call again with confirm_token unchanged."
    )

    def setUp(self):
        self.tenant = create_tenant(display_name="Reddit Confirm", telegram_chat_id=701001)
        seed_internal_key(self.tenant)

    def _post(self, payload):
        return self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reddit/tool/",
            data=payload,
            content_type="application/json",
            HTTP_X_NBHD_INTERNAL_KEY="shared-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    @patch("apps.integrations.runtime_views.execute_reddit_tool")
    def test_post_previews_then_executes_only_with_matching_token(self, execute):
        draft = {
            "action": "post",
            "subreddit": "python",
            "title": "Exact title",
            "text": "Exact body",
            "kind": "self",
        }

        preview_response = self._post(draft)

        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.json()
        self.assertEqual(preview["preview"], draft)
        self.assertTrue(preview["confirm_token"])
        self.assertEqual(preview["guidance"], self.guidance)
        execute.assert_not_called()

        execute.return_value = {"post_url": "https://reddit.test/post"}
        confirmed = self._post({**draft, "confirm_token": preview["confirm_token"]})

        self.assertEqual(confirmed.status_code, 200)
        execute.assert_called_once_with(
            self.tenant,
            "post",
            {"subreddit": "python", "title": "Exact title", "text": "Exact body", "kind": "self"},
        )

    @patch("apps.integrations.runtime_views.execute_reddit_tool")
    def test_post_rejects_wrong_expired_and_parameter_changed_tokens(self, execute):
        draft = {
            "action": "post",
            "subreddit": "python",
            "title": "Original",
            "text": "Exact body",
            "kind": "self",
        }
        preview = self._post(draft).json()

        wrong = self._post({**draft, "confirm_token": "wrong"})
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json()["guidance"], self.guidance)

        changed = self._post({**draft, "title": "Changed", "confirm_token": preview["confirm_token"]})
        self.assertEqual(changed.status_code, 400)
        self.assertEqual(changed.json()["reason"], "mismatch")
        self.assertEqual(changed.json()["guidance"], self.guidance)

        issued_at = 1_800_000_000
        with patch("django.core.signing.time.time", return_value=issued_at):
            expiring = self._post(draft).json()["confirm_token"]
        with patch("django.core.signing.time.time", return_value=issued_at + 601):
            expired = self._post({**draft, "confirm_token": expiring})
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(expired.json()["reason"], "expired")
        self.assertEqual(expired.json()["guidance"], self.guidance)
        execute.assert_not_called()

    @patch("apps.integrations.runtime_views.execute_reddit_tool")
    def test_reply_previews_then_executes_only_with_matching_token(self, execute):
        draft = {"action": "reply", "thing_id": "t1_abc123", "text": "Exact reply"}

        preview = self._post(draft).json()

        self.assertEqual(preview["preview"], draft)
        self.assertTrue(preview["confirm_token"])
        execute.assert_not_called()

        execute.return_value = {"comment_url": "https://reddit.test/comment"}
        confirmed = self._post({**draft, "confirm_token": preview["confirm_token"]})

        self.assertEqual(confirmed.status_code, 200)
        execute.assert_called_once_with(
            self.tenant,
            "reply",
            {"thing_id": "t1_abc123", "text": "Exact reply"},
        )

    @patch("apps.integrations.runtime_views.execute_reddit_tool")
    def test_reply_rejects_wrong_expired_and_parameter_changed_tokens(self, execute):
        draft = {"action": "reply", "thing_id": "t1_abc123", "text": "Original"}
        preview = self._post(draft).json()

        wrong = self._post({**draft, "confirm_token": "wrong"})
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json()["guidance"], self.guidance)

        changed = self._post({**draft, "text": "Changed", "confirm_token": preview["confirm_token"]})
        self.assertEqual(changed.status_code, 400)
        self.assertEqual(changed.json()["reason"], "mismatch")
        self.assertEqual(changed.json()["guidance"], self.guidance)

        issued_at = 1_800_000_000
        with patch("django.core.signing.time.time", return_value=issued_at):
            expiring = self._post(draft).json()["confirm_token"]
        with patch("django.core.signing.time.time", return_value=issued_at + 601):
            expired = self._post({**draft, "confirm_token": expiring})
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(expired.json()["reason"], "expired")
        self.assertEqual(expired.json()["guidance"], self.guidance)
        execute.assert_not_called()


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class WorkspaceDeleteConfirmHandshakeTests(TestCase):
    guidance = (
        "Show this workspace deletion preview, obtain explicit confirmation, then retry with confirm_token unchanged."
    )

    def setUp(self):
        self.tenant = create_tenant(display_name="Workspace Confirm", telegram_chat_id=701002)
        seed_internal_key(self.tenant)
        self.default = Workspace.objects.create(
            tenant=self.tenant,
            name="General",
            slug="general",
            is_default=True,
        )
        self.workspace = Workspace.objects.create(tenant=self.tenant, name="Work", slug="work")
        self.tenant.active_workspace = self.workspace
        self.tenant.save(update_fields=["active_workspace"])

    def _delete(self, slug, payload=None):
        return self.client.delete(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/{slug}/",
            data=payload or {},
            content_type="application/json",
            HTTP_X_NBHD_INTERNAL_KEY="shared-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    def test_preview_is_inert_and_matching_token_deletes(self):
        preview_response = self._delete("work")

        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.json()
        self.assertEqual(
            preview["preview"],
            {
                "slug": "work",
                "name": "Work",
                "content_counts": {"associated_records": 0},
                "will_fall_back_to_default": True,
            },
        )
        self.assertTrue(Workspace.objects.filter(id=self.workspace.id).exists())

        confirmed = self._delete("work", {"confirm_token": preview["confirm_token"]})

        self.assertEqual(confirmed.status_code, 200)
        self.assertFalse(Workspace.objects.filter(id=self.workspace.id).exists())
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.active_workspace_id, self.default.id)

    def test_wrong_expired_and_parameter_changed_tokens_are_inert(self):
        other = Workspace.objects.create(tenant=self.tenant, name="Other", slug="other")
        preview = self._delete("work").json()

        wrong = self._delete("work", {"confirm_token": "wrong"})
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json()["guidance"], self.guidance)

        changed = self._delete("other", {"confirm_token": preview["confirm_token"]})
        self.assertEqual(changed.status_code, 400)
        self.assertEqual(changed.json()["reason"], "mismatch")
        self.assertEqual(changed.json()["guidance"], self.guidance)

        issued_at = 1_800_000_000
        with patch("django.core.signing.time.time", return_value=issued_at):
            expiring = self._delete("work").json()["confirm_token"]
        with patch("django.core.signing.time.time", return_value=issued_at + 601):
            expired = self._delete("work", {"confirm_token": expiring})
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(expired.json()["reason"], "expired")
        self.assertEqual(expired.json()["guidance"], self.guidance)

        self.assertEqual(Workspace.objects.filter(id__in=[self.workspace.id, other.id]).count(), 2)
