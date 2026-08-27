"""Regression tests for the per-Gunicorn-worker PII model warm-up."""

from __future__ import annotations

import importlib
import os
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from django.apps import AppConfig
from django.test import SimpleTestCase

from apps.pii import engine
from apps.pii.apps import PiiConfig
from apps.pii.redactor import redact_text
from config.test_runner import QuietCronSignalRunner


def _post_worker_init():
    config = runpy.run_path(str(Path(__file__).parents[2] / "gunicorn.conf.py"))
    return config["post_worker_init"]


class PiiWorkerPrewarmTests(SimpleTestCase):
    def setUp(self):
        self.worker = SimpleNamespace(log=Mock())
        self.engine_state = (
            engine._pipeline,
            engine._pipeline_load_error,
            engine._pattern_recognizers,
        )
        engine._pipeline = None
        engine._pipeline_load_error = None
        engine._pattern_recognizers = None

    def tearDown(self):
        (
            engine._pipeline,
            engine._pipeline_load_error,
            engine._pattern_recognizers,
        ) = self.engine_state

    @patch("apps.crypto.prewarm.start_prewarm_thread")
    @patch("apps.pii.engine.get_pii_pipeline")
    def test_worker_hook_invokes_engine_loader_exactly_once(self, loader, _dek_prewarm):
        _post_worker_init()(self.worker)

        loader.assert_called_once_with()
        self.worker.log.info.assert_any_call("post_worker_init: PII pipeline warmed")

    @patch("apps.crypto.prewarm.start_prewarm_thread")
    def test_shared_transport_pings_without_constructing_local_detector(self, _dek_prewarm):
        with (
            patch.dict(os.environ, {"PII_DETECTOR_TRANSPORT": "shared", "PII_SHARED_WARM_WAIT_S": "0"}),
            patch("apps.pii.shared_client.ping_shared_detector", return_value=True) as ping,
            patch("apps.pii.engine.get_pii_pipeline") as loader,
        ):
            _post_worker_init()(self.worker)

        ping.assert_called_once()
        loader.assert_not_called()
        shared_logs = [
            call for call in self.worker.log.info.call_args_list if "PII shared transport ready" in call.args[0]
        ]
        self.assertEqual(len(shared_logs), 1)

    @patch("apps.crypto.prewarm.start_prewarm_thread")
    def test_shared_transport_warm_timeout_fails_open(self, _dek_prewarm):
        with (
            patch.dict(os.environ, {"PII_DETECTOR_TRANSPORT": "shared", "PII_SHARED_WARM_WAIT_S": "0"}),
            patch("apps.pii.shared_client.ping_shared_detector", return_value=False),
            patch("apps.pii.engine.get_pii_pipeline") as loader,
        ):
            _post_worker_init()(self.worker)

        loader.assert_not_called()
        self.worker.log.warning.assert_called_once_with(
            "post_worker_init: PII shared transport not ready after %.1f s; "
            "failing open (unconfirmed receipts) until ready",
            0.0,
        )

    @patch("apps.crypto.prewarm.start_prewarm_thread")
    @patch("apps.pii.engine.get_pii_pipeline", side_effect=RuntimeError("model unavailable"))
    def test_worker_hook_logs_load_failure_and_does_not_raise(self, loader, _dek_prewarm):
        _post_worker_init()(self.worker)

        loader.assert_called_once_with()
        self.worker.log.error.assert_called_once()
        self.assertIn("pattern-recognizer fallback remains active", self.worker.log.error.call_args.args[0])

    @patch("apps.crypto.prewarm.start_prewarm_thread")
    def test_real_load_failure_is_cached_and_pattern_fallback_remains(self, _dek_prewarm):
        load_error = RuntimeError("synthetic model load failure")
        transformers = ModuleType("transformers")
        transformers.AutoTokenizer = SimpleNamespace(from_pretrained=Mock(side_effect=load_error))
        transformers.AutoModelForTokenClassification = SimpleNamespace(from_pretrained=Mock())
        transformers.pipeline = Mock()

        with patch.dict(sys.modules, {"transformers": transformers}):
            _post_worker_init()(self.worker)
            result = redact_text("Email synthetic.person@example.com", tier="starter")

        self.assertIs(engine._pipeline_load_error, load_error)
        transformers.AutoTokenizer.from_pretrained.assert_called_once_with(engine._HF_MODEL_REPO)
        self.worker.log.error.assert_called_once()
        self.assertNotIn("synthetic.person@example.com", result)
        self.assertIn("[EMAIL_ADDRESS_", result)


class ModelFreeDjangoContextTests(SimpleTestCase):
    def test_app_ready_and_test_runner_never_trigger_model_warmup(self):
        self.assertIs(PiiConfig.ready, AppConfig.ready)
        pii_module = importlib.import_module("apps.pii")

        with patch("apps.pii.engine.get_pii_pipeline") as loader:
            for command in ("migrate", "test"):
                with patch.object(sys, "argv", ["manage.py", command]):
                    PiiConfig("apps.pii", pii_module).ready()
                    QuietCronSignalRunner()

        loader.assert_not_called()
