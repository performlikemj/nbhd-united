"""Tests for the /health/ build-identity field.

The CI deploy gate polls /health/ and passes only when the serving revision's
``build`` == the deploying git SHA (config/health.py echoes
``settings.SENTRY_RELEASE``, which the deploy stamps = github.sha). These assert
the field is present when the release is set and empty-safe when it is not.
"""

from __future__ import annotations

from django.test import TestCase, override_settings


class HealthBuildFieldTests(TestCase):
    @override_settings(SENTRY_RELEASE="abc123deadbeef")
    def test_build_present_when_release_set(self):
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["build"], "abc123deadbeef")

    @override_settings(SENTRY_RELEASE="")
    def test_build_empty_string_when_release_unset(self):
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        # Additive + empty-safe: absent/"" is what a pre-field revision serves,
        # and the CI gate treats that as not-ready rather than erroring.
        self.assertEqual(body["build"], "")

    @override_settings(SENTRY_RELEASE="abc123deadbeef")
    def test_status_shape_unchanged(self):
        # The build key is strictly additive — status is still "ok".
        body = self.client.get("/health/").json()
        self.assertEqual(set(body.keys()), {"status", "build"})
