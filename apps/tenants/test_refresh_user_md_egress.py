from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.tenants.models import Tenant, User


class RefreshUserMdEgressCommandTest(TestCase):
    def _tenant(self, username: str, *, provisioned: bool, container_id: str = "oc-test") -> Tenant:
        user = User.objects.create_user(username=username)
        return Tenant.objects.create(
            user=user,
            container_id=container_id if provisioned else "",
            provisioned_at=datetime(2026, 8, 26, tzinfo=UTC) if provisioned else None,
        )

    @patch("apps.tenants.management.commands.refresh_user_md_egress.upload_workspace_file")
    @patch("apps.tenants.management.commands.refresh_user_md_egress.render_safe_user_md")
    @patch("apps.tenants.management.commands.refresh_user_md_egress.download_workspace_file")
    def test_refreshes_provisioned_tenants_and_reports_counts_only(self, download, render, upload):
        refreshed = self._tenant("refresh-ok", provisioned=True)
        failed = self._tenant("refresh-fail", provisioned=True)
        provisioned_without_container_id = self._tenant(
            "refresh-no-container-id",
            provisioned=True,
            container_id="",
        )
        skipped = self._tenant("refresh-skip", provisioned=False)
        download.return_value = "historical raw block"
        render.side_effect = lambda tenant, _existing: (
            "safe managed content" if tenant.user.username == "refresh-ok" else None
        )
        stdout = StringIO()

        call_command("refresh_user_md_egress", stdout=stdout)

        upload.assert_called_once_with(str(refreshed.id), "workspace/USER.md", "safe managed content")
        queried_ids = {call.args[0] for call in download.call_args_list}
        self.assertEqual(
            queried_ids,
            {str(refreshed.id), str(failed.id), str(provisioned_without_container_id.id)},
        )
        output = stdout.getvalue().strip()
        self.assertEqual(output, "tenants_refreshed=1 tenants_failed=2")
        self.assertNotIn(str(refreshed.id), output)
        self.assertNotIn(str(failed.id), output)
        self.assertNotIn(str(skipped.id), output)
