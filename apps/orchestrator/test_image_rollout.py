"""The per-tenant OpenClaw image auto-roll allowlist (image_rollout_allowed).

Default (empty) MUST allow nobody so a deploy that bumps OPENCLAW_IMAGE_TAG
never rolls the fleet on its own; a staged rollout opts tenants in.
"""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from apps.orchestrator.image_rollout import image_rollout_allowed

CANARY = "148ccf1c-ef13-47f8-aaaa-bbbbbbbbbbbb"
OTHER = "99999999-0000-0000-0000-000000000000"


class ImageRolloutAllowlistTest(SimpleTestCase):
    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS="")
    def test_empty_allows_nobody(self):
        self.assertFalse(image_rollout_allowed(CANARY))
        self.assertFalse(image_rollout_allowed(OTHER))

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS="   ")
    def test_whitespace_only_allows_nobody(self):
        self.assertFalse(image_rollout_allowed(CANARY))

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS="*")
    def test_star_allows_everyone(self):
        self.assertTrue(image_rollout_allowed(CANARY))
        self.assertTrue(image_rollout_allowed(OTHER))

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=CANARY)
    def test_single_id_allows_only_it(self):
        self.assertTrue(image_rollout_allowed(CANARY))
        self.assertFalse(image_rollout_allowed(OTHER))

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=f" {CANARY} , {OTHER} ")
    def test_comma_list_trims_and_matches(self):
        self.assertTrue(image_rollout_allowed(CANARY))
        self.assertTrue(image_rollout_allowed(OTHER))
        self.assertFalse(image_rollout_allowed("00000000-0000-0000-0000-000000000000"))

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=CANARY)
    def test_accepts_uuid_or_str(self):
        import uuid

        self.assertTrue(image_rollout_allowed(uuid.UUID(CANARY)))
        self.assertTrue(image_rollout_allowed(CANARY))
