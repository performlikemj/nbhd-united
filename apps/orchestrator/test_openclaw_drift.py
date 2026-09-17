"""OpenClaw desired-state drift comparison + the per-field storage reconcile.

All SimpleTestCase (no DB): the comparison takes SDK-shaped objects, and the
reconcile is exercised against a mocked container client.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.orchestrator.azure_client import (
    _OC_STATE_ENV,
    _WORKSPACE_MOUNT_OPTIONS,
    _ensure_oc_state_dir_in_template,
    ensure_openclaw_storage_ready,
    update_container_image,
)
from apps.orchestrator.openclaw_drift import (
    FIELD_CONTAINER,
    FIELD_IMAGE,
    FIELD_OC_STATE_MOUNT,
    FIELD_OC_STATE_VOLUME,
    FIELD_WORKSPACE_MOUNT_OPTIONS,
    IMAGE_DB_MISMATCH,
    IMAGE_STALE_ALLOWED,
    IMAGE_STALE_GATED,
    FleetDriftReport,
    TenantDrift,
    compare_image,
    compare_storage,
    compare_template,
    env_field,
    format_alert,
    image_tag_of,
)

CANARY = "148ccf1c-ef13-47f8-aaaa-bbbbbbbbbbbb"
OTHER = "99999999-0000-0000-0000-000000000000"
IMAGE = "nbhdunited.azurecr.io/nbhd-openclaw:2026.9.4-abc1234"


def _converged_app(image: str = IMAGE):
    """A template exactly as ``create_container_app`` would provision it."""
    container = SimpleNamespace(
        name="openclaw",
        image=image,
        volume_mounts=[
            SimpleNamespace(volume_name="workspace", mount_path="/home/node/.openclaw"),
            SimpleNamespace(volume_name="oc-state", mount_path="/home/node/oc-state"),
        ],
        env=[SimpleNamespace(name=k, value=v) for k, v in _OC_STATE_ENV.items()]
        + [SimpleNamespace(name="OPENCLAW_DISABLE_BONJOUR", value="1")],
    )
    app = MagicMock()
    app.template.containers = [container]
    app.template.volumes = [
        SimpleNamespace(name="workspace", storage_type="AzureFile", mount_options=_WORKSPACE_MOUNT_OPTIONS),
        SimpleNamespace(name="oc-state", storage_type="EmptyDir"),
    ]
    return app


def _legacy_app(image: str = IMAGE):
    """A pre-9.4 template: share mounted root-owned, no oc-state, no env."""
    container = SimpleNamespace(
        name="openclaw",
        image=image,
        volume_mounts=[SimpleNamespace(volume_name="workspace", mount_path="/home/node/.openclaw")],
        env=[SimpleNamespace(name="OPENCLAW_DISABLE_BONJOUR", value="1")],
    )
    app = MagicMock()
    app.template.containers = [container]
    app.template.volumes = [SimpleNamespace(name="workspace", storage_type="AzureFile", mount_options=None)]
    return app


class CompareStorageTest(SimpleTestCase):
    def test_converged_template_has_no_drift(self):
        self.assertEqual(compare_storage(_converged_app()), [])

    def test_legacy_template_reports_every_field(self):
        fields = {d.field for d in compare_storage(_legacy_app())}
        expected = {FIELD_WORKSPACE_MOUNT_OPTIONS, FIELD_OC_STATE_VOLUME, FIELD_OC_STATE_MOUNT} | {
            env_field(n) for n in _OC_STATE_ENV
        }
        self.assertEqual(fields, expected)
        # Storage drift is always alert-worthy.
        self.assertTrue(all(d.alert for d in compare_storage(_legacy_app())))

    def test_hand_edited_env_value_is_drift(self):
        app = _converged_app()
        for e in app.template.containers[0].env:
            if e.name == "OPENCLAW_STATE_DIR":
                e.value = "/home/node/.openclaw/state"  # someone pointed it back at SMB
        drift = compare_storage(app)
        self.assertEqual([d.field for d in drift], [env_field("OPENCLAW_STATE_DIR")])
        self.assertEqual(drift[0].expected, "/home/node/oc-state")
        self.assertEqual(drift[0].actual, "/home/node/.openclaw/state")

    def test_hand_edited_mount_options_is_drift(self):
        app = _converged_app()
        app.template.volumes[0].mount_options = "uid=0,gid=0"
        drift = compare_storage(app)
        self.assertEqual([d.field for d in drift], [FIELD_WORKSPACE_MOUNT_OPTIONS])

    def test_missing_workspace_volume_is_flagged_unfixable(self):
        app = _converged_app()
        app.template.volumes = [v for v in app.template.volumes if v.name != "workspace"]
        drift = compare_storage(app)
        self.assertEqual([d.field for d in drift], [FIELD_WORKSPACE_MOUNT_OPTIONS])
        self.assertIn("not auto-fixable", drift[0].note)

    def test_missing_openclaw_container_short_circuits(self):
        app = MagicMock()
        app.template.containers = [SimpleNamespace(name="sidecar")]
        app.template.volumes = []
        drift = compare_storage(app)
        self.assertEqual([d.field for d in drift], [FIELD_CONTAINER])

    def test_ensure_helper_and_compare_agree(self):
        """The reconcile helper must leave NOTHING the comparison still flags —
        otherwise ensure_openclaw_ready would report drift it cannot fix."""
        app = _legacy_app()
        self.assertTrue(_ensure_oc_state_dir_in_template(app))
        self.assertEqual(compare_storage(app), [])
        # And the helper is idempotent on the now-converged template.
        self.assertFalse(_ensure_oc_state_dir_in_template(app))

    def test_5_28_image_env_present_is_drift_then_reconciled_away(self):
        """The relocation env is version-gated. On a 5.28 image it must be
        ABSENT (5.28 honors OPENCLAW_STATE_DIR — leaving it moves 5.28's live
        state onto the wipe-on-restart EmptyDir). compare_storage flags a
        5.28 tenant that still carries the env, and _ensure_oc_state_dir_in_template
        removes it so the two agree — no false drift-alert on the 5.28 fleet."""
        app = _converged_app(image="nbhdunited.azurecr.io/nbhd-openclaw:2026.5.28-cc3bcd2")
        drift = compare_storage(app)
        self.assertEqual({d.field for d in drift}, {env_field(n) for n in _OC_STATE_ENV})
        # Reconcile removes the env for a 5.28 image → then fully in sync.
        self.assertTrue(_ensure_oc_state_dir_in_template(app))
        self.assertEqual(compare_storage(app), [])

    def test_5_28_image_converged_storage_no_env_has_no_drift(self):
        """A 5.28 tenant with the node-owned mount + oc-state volume but NO
        relocation env is fully in sync and must NOT alert."""
        app = _converged_app(image="nbhdunited.azurecr.io/nbhd-openclaw:2026.5.28-cc3bcd2")
        app.template.containers[0].env = [SimpleNamespace(name="OPENCLAW_DISABLE_BONJOUR", value="1")]
        self.assertEqual(compare_storage(app), [])


class CompareImageTest(SimpleTestCase):
    def test_on_desired_tag_is_clean(self):
        self.assertEqual(compare_image(_converged_app(), desired_tag="2026.9.4-abc1234", rollout_allowed=False), [])

    def test_stale_and_not_allowlisted_is_gated_no_alert(self):
        drift = compare_image(_converged_app(), desired_tag="2026.9.5-def5678", rollout_allowed=False)
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0].field, FIELD_IMAGE)
        self.assertFalse(drift[0].alert)
        self.assertEqual(drift[0].note, IMAGE_STALE_GATED)
        self.assertEqual(drift[0].actual, "2026.9.4-abc1234")

    def test_stale_and_allowlisted_is_pending_no_alert(self):
        drift = compare_image(_converged_app(), desired_tag="2026.9.5-def5678", rollout_allowed=True)
        self.assertFalse(drift[0].alert)
        self.assertEqual(drift[0].note, IMAGE_STALE_ALLOWED)

    def test_latest_or_empty_desired_disables_stale_check(self):
        self.assertEqual(compare_image(_converged_app(), desired_tag="latest", rollout_allowed=True), [])
        self.assertEqual(compare_image(_converged_app(), desired_tag="", rollout_allowed=True), [])

    def test_db_live_mismatch_alerts_even_when_gated(self):
        drift = compare_image(
            _converged_app(),
            desired_tag="2026.9.4-abc1234",
            rollout_allowed=False,
            db_image_tag="2026.5.28-755d789",
        )
        self.assertEqual(len(drift), 1)
        self.assertTrue(drift[0].alert)
        self.assertIn(IMAGE_DB_MISMATCH, drift[0].note)
        self.assertEqual(drift[0].expected, "2026.5.28-755d789")
        self.assertEqual(drift[0].actual, "2026.9.4-abc1234")

    def test_empty_db_tag_skips_mismatch_check(self):
        drift = compare_image(_converged_app(), desired_tag="2026.9.4-abc1234", rollout_allowed=False, db_image_tag="")
        self.assertEqual(drift, [])

    def test_image_tag_of(self):
        self.assertEqual(image_tag_of(IMAGE), "2026.9.4-abc1234")
        self.assertEqual(image_tag_of("nbhdunited.azurecr.io/nbhd-openclaw"), "")
        self.assertEqual(image_tag_of("localhost:5000/nbhd-openclaw"), "")
        self.assertEqual(image_tag_of(None), "")


class CompareTemplateGateTest(SimpleTestCase):
    """compare_template wires the real allowlist + OPENCLAW_IMAGE_TAG setting."""

    @override_settings(OPENCLAW_IMAGE_TAG="2026.9.5-def5678", OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS="")
    def test_empty_allowlist_gates_everyone(self):
        drift = compare_template(_converged_app(), tenant_id=CANARY, db_image_tag="2026.9.4-abc1234")
        self.assertEqual([d.note for d in drift], [IMAGE_STALE_GATED])

    @override_settings(OPENCLAW_IMAGE_TAG="2026.9.5-def5678", OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=CANARY)
    def test_allowlisted_tenant_is_pending_not_gated(self):
        drift = compare_template(_converged_app(), tenant_id=CANARY, db_image_tag="2026.9.4-abc1234")
        self.assertEqual([d.note for d in drift], [IMAGE_STALE_ALLOWED])
        other = compare_template(_converged_app(), tenant_id=OTHER, db_image_tag="2026.9.4-abc1234")
        self.assertEqual([d.note for d in other], [IMAGE_STALE_GATED])

    @override_settings(OPENCLAW_IMAGE_TAG="2026.9.5-def5678", OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS="")
    def test_storage_drift_alerts_alongside_gated_image(self):
        drift = compare_template(_legacy_app(), tenant_id=OTHER, db_image_tag="2026.9.4-abc1234")
        alerting = [d for d in drift if d.alert]
        gated = [d for d in drift if not d.alert]
        self.assertEqual(len(gated), 1)
        self.assertEqual(gated[0].field, FIELD_IMAGE)
        self.assertEqual(len(alerting), 3 + len(_OC_STATE_ENV))


@override_settings(AZURE_RESOURCE_GROUP="rg-test")
@patch("apps.orchestrator.azure_client._is_mock", return_value=False)
@patch("apps.orchestrator.azure_client.get_container_client")
class EnsureOpenclawStorageReadyTest(SimpleTestCase):
    def _client(self, mock_get_client, app):
        client = MagicMock()
        client.container_apps.get.return_value = app
        client.container_apps.begin_create_or_update.return_value = MagicMock()
        mock_get_client.return_value = client
        return client

    def test_converged_is_noop(self, mock_get_client, _mock):
        client = self._client(mock_get_client, _converged_app())
        result = ensure_openclaw_storage_ready("oc-tenant")
        self.assertTrue(result.in_sync)
        self.assertFalse(result.revision_created)
        client.container_apps.begin_create_or_update.assert_not_called()

    def test_legacy_is_fixed_per_field_and_written(self, mock_get_client, _mock):
        app = _legacy_app()
        client = self._client(mock_get_client, app)
        result = ensure_openclaw_storage_ready("oc-tenant")
        self.assertFalse(result.in_sync)
        self.assertTrue(result.revision_created)
        self.assertEqual(result.unfixable_fields, [])
        self.assertEqual(len(result.fixed_fields), 3 + len(_OC_STATE_ENV))
        client.container_apps.begin_create_or_update.assert_called_once_with("rg-test", "oc-tenant", app)
        # Second run on the (now mutated) template is a clean no-op.
        client.container_apps.begin_create_or_update.reset_mock()
        again = ensure_openclaw_storage_ready("oc-tenant")
        self.assertTrue(again.in_sync)
        client.container_apps.begin_create_or_update.assert_not_called()

    def test_dry_run_reports_but_never_writes(self, mock_get_client, _mock):
        client = self._client(mock_get_client, _legacy_app())
        result = ensure_openclaw_storage_ready("oc-tenant", dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.revision_created)
        self.assertEqual(len(result.fixed_fields), 3 + len(_OC_STATE_ENV))
        client.container_apps.begin_create_or_update.assert_not_called()

    def test_unfixable_field_is_reported_and_rest_still_written(self, mock_get_client, _mock):
        app = _legacy_app()
        app.template.volumes = []  # workspace share volume gone — cannot be recreated here
        client = self._client(mock_get_client, app)
        result = ensure_openclaw_storage_ready("oc-tenant")
        self.assertEqual(result.unfixable_fields, [FIELD_WORKSPACE_MOUNT_OPTIONS])
        self.assertIn(FIELD_OC_STATE_VOLUME, result.fixed_fields)
        self.assertTrue(result.revision_created)
        client.container_apps.begin_create_or_update.assert_called_once()

    def test_mock_short_circuits(self, mock_get_client, mock_is_mock):
        mock_is_mock.return_value = True
        result = ensure_openclaw_storage_ready("oc-tenant")
        self.assertTrue(result.mock)
        self.assertTrue(result.in_sync)
        mock_get_client.assert_not_called()


@override_settings(AZURE_RESOURCE_GROUP="rg-test")
@patch("apps.orchestrator.azure_client._is_mock", return_value=False)
@patch("apps.orchestrator.azure_client.get_container_client")
class ImageBumpBakesStorageTest(SimpleTestCase):
    def test_update_container_image_converges_storage(self, mock_get_client, _mock):
        """The auto-roll path must never land a 9.4 image on a container that
        lacks the storage it needs to boot."""
        app = _legacy_app(image="nbhdunited.azurecr.io/nbhd-openclaw:old")
        client = MagicMock()
        client.container_apps.get.return_value = app
        mock_get_client.return_value = client

        update_container_image("oc-tenant", "nbhdunited.azurecr.io/nbhd-openclaw:2026.9.4-abc1234")

        self.assertEqual(compare_storage(app), [])
        self.assertEqual(app.template.containers[0].image, "nbhdunited.azurecr.io/nbhd-openclaw:2026.9.4-abc1234")


class FormatAlertTest(SimpleTestCase):
    def _report(self, n_alerting: int, n_gated: int = 0, error: int = 0) -> FleetDriftReport:
        report = FleetDriftReport(checked=n_alerting + n_gated + error + 2)
        for i in range(n_alerting):
            report.tenants.append(
                TenantDrift(
                    tenant_id=f"{i:08x}-0000-0000-0000-000000000000",
                    container_name=f"oc-{i:08x}",
                    fields=compare_storage(_legacy_app())[:2],
                )
            )
        for i in range(n_gated):
            report.tenants.append(
                TenantDrift(
                    tenant_id=f"{i:08x}-1111-0000-0000-000000000000",
                    container_name=f"oc-gated{i}",
                    fields=compare_image(_converged_app(), desired_tag="2026.9.5-x", rollout_allowed=False),
                )
            )
        for i in range(error):
            report.tenants.append(
                TenantDrift(
                    tenant_id=f"{i:08x}-2222-0000-0000-000000000000",
                    container_name=f"oc-err{i}",
                    error="ResourceNotFoundError: (ResourceNotFound) body-with-details",
                )
            )
        return report

    def test_gated_only_report_does_not_alert(self):
        report = self._report(0, n_gated=3)
        self.assertEqual(report.alerting, [])
        self.assertEqual(len(report.gated_only), 3)

    def test_alert_lists_field_names_only(self):
        body = format_alert(self._report(2, n_gated=1))
        self.assertIn("2/", body)
        self.assertIn("oc-00000000 (00000000)", body)
        self.assertIn(FIELD_WORKSPACE_MOUNT_OPTIONS, body)
        self.assertNotIn("oc-gated", body)
        self.assertNotIn(_WORKSPACE_MOUNT_OPTIONS, body)  # never values
        self.assertIn("ensure_openclaw_ready", body)

    def test_error_tenant_alerts_with_class_only(self):
        body = format_alert(self._report(0, error=1))
        self.assertIn("unreadable (ResourceNotFoundError)", body)
        self.assertNotIn("body-with-details", body)

    def test_alert_is_truncated_safely(self):
        body = format_alert(self._report(40))
        self.assertLessEqual(len(body), 1000)
        self.assertIn("... and 30 more", body)
        # A pathological single line still cannot exceed Pushover's cap.
        report = self._report(1)
        report.tenants[0].fields = [f for f in compare_storage(_legacy_app())] * 40
        self.assertLessEqual(len(format_alert(report)), 1000)
