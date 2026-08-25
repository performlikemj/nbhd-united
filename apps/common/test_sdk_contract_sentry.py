"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import logging

import sentry_sdk
from django.test import SimpleTestCase
from sentry_sdk.integrations.logging import LoggingIntegration


class SentrySdkContractTest(SimpleTestCase):
    def test_client_accepts_our_init_options_without_transport(self):
        def before_send(event, _hint):
            return event

        def before_send_log(log, _hint):
            return log

        integration = LoggingIntegration(sentry_logs_level=logging.WARNING)

        client = sentry_sdk.Client(
            dsn=None,
            environment="test",
            release=None,
            send_default_pii=False,
            include_local_variables=False,
            enable_logs=True,
            integrations=[integration],
            traces_sample_rate=1.0,
            profile_session_sample_rate=1.0,
            profile_lifecycle="trace",
            before_send=before_send,
            before_send_log=before_send_log,
        )

        self.assertFalse(client.options["send_default_pii"])
        self.assertFalse(client.options["include_local_variables"])
        client.close()

    def test_scope_and_capture_methods_exist(self):
        with sentry_sdk.new_scope() as scope:
            self.assertTrue(callable(scope.set_tag))
            self.assertTrue(callable(scope.set_extra))
        self.assertTrue(callable(sentry_sdk.set_tag))
        self.assertTrue(callable(sentry_sdk.capture_exception))
        self.assertTrue(callable(sentry_sdk.capture_message))
