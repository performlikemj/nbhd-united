"""Regression coverage for the OpenClaw plugin-packaging CI guard.

The guard (``scripts/check_openclaw_plugin_packaging.py``) would have caught the
2026-07-05 incident at PR time: ``config_generator`` emitted
``plugins.load.paths: /opt/nbhd/plugins/nbhd-friends-tools`` but the Dockerfile
never COPY'd that plugin, so OpenClaw hard-failed boot with ``plugin path not
found``. These tests pin the pure detection logic (inject a fake missing plugin,
assert the failure names it) and assert the real repo is currently clean.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_GUARD_PATH = Path(settings.BASE_DIR) / "scripts" / "check_openclaw_plugin_packaging.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_openclaw_plugin_packaging", _GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


class PluginPackagingDetectionTests(SimpleTestCase):
    def test_clean_state_has_no_errors(self):
        shippable = {"nbhd-friends-tools", "nbhd-fuel-tools"}
        copied = {"nbhd-friends-tools", "nbhd-fuel-tools"}
        emittable = {"nbhd-friends-tools"}
        self.assertEqual(guard.find_packaging_errors(shippable, copied, emittable), [])

    def test_missing_shippable_plugin_is_flagged_by_name(self):
        """A plugin with source + manifest but no COPY line — the friends bug."""
        shippable = {"nbhd-friends-tools", "nbhd-agenda-tools", "nbhd-fuel-tools"}
        copied = {"nbhd-fuel-tools"}  # friends + agenda COPY forgotten
        emittable = {"nbhd-friends-tools"}
        errors = guard.find_packaging_errors(shippable, copied, emittable)
        self.assertTrue(errors)
        joined = " ".join(errors)
        self.assertIn("nbhd-friends-tools", joined)
        self.assertIn("nbhd-agenda-tools", joined)
        self.assertIn("plugin path not found", joined)

    def test_dangling_config_reference_is_flagged(self):
        """A plugin the generator can emit but the image never COPYs — the exact
        boot-crash property, independent of the manifest set."""
        shippable = {"nbhd-fuel-tools"}
        copied = {"nbhd-fuel-tools"}
        emittable = {"nbhd-friends-tools"}  # generator references it, not copied
        errors = guard.find_packaging_errors(shippable, copied, emittable)
        self.assertTrue(any("nbhd-friends-tools" in e for e in errors))

    def test_extra_copied_plugin_is_not_an_error(self):
        """COPYing a plugin that isn't emittable/shippable-tracked is harmless."""
        shippable = {"nbhd-fuel-tools"}
        copied = {"nbhd-fuel-tools", "nbhd-something-extra"}
        emittable = {"nbhd-fuel-tools"}
        self.assertEqual(guard.find_packaging_errors(shippable, copied, emittable), [])


class PluginPackagingRepoStateTests(SimpleTestCase):
    """The real repo must be clean — this is the assertion CI runs."""

    def test_repo_dockerfile_packages_every_plugin(self):
        shippable = guard.shippable_plugins()
        copied = guard.dockerfile_copied_plugins()
        emittable = guard.config_emittable_plugins()
        errors = guard.find_packaging_errors(shippable, copied, emittable)
        self.assertEqual(errors, [], f"Dockerfile.openclaw plugin packaging is broken: {errors}")

    def test_friends_agenda_and_datebook_are_packaged(self):
        """Explicit pins, including B2b's image-before-config Datebook wiring."""
        copied = guard.dockerfile_copied_plugins()
        self.assertIn("nbhd-friends-tools", copied)
        self.assertIn("nbhd-agenda-tools", copied)
        self.assertIn("nbhd-datebook-tools", copied)
        self.assertIn("nbhd-datebook-tools", guard.shippable_plugins())
        self.assertIn("nbhd-datebook-tools", guard.config_emittable_plugins())

    def test_deleted_image_plugin_is_absent_from_dockerfile(self):
        self.assertNotIn("image-gen", guard.DOCKERFILE.read_text())
