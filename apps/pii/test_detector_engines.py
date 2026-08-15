"""Regression coverage for the flag-gated DeBERTa/Liquid detector seam."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.pii import engine, liquid_engine
from apps.pii.config import DEBERTA_LABEL_MAP, LIQUID_LABEL_MAP
from apps.pii.redactor import redact_text


class DetectorEngineSelectionTests(SimpleTestCase):
    def setUp(self):
        self.warned_values = set(engine._warned_unknown_detector_engines)
        engine._warned_unknown_detector_engines.clear()

    def tearDown(self):
        engine._warned_unknown_detector_engines.clear()
        engine._warned_unknown_detector_engines.update(self.warned_values)

    def test_default_engine_is_deberta(self):
        expected = object()
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("apps.pii.engine.get_deberta_pii_pipeline", return_value=expected) as deberta,
            patch("apps.pii.liquid_engine.get_liquid_pii_pipeline") as liquid,
        ):
            os.environ.pop("PII_DETECTOR_ENGINE", None)
            self.assertIs(engine.get_pii_pipeline(), expected)

        deberta.assert_called_once_with()
        liquid.assert_not_called()

    def test_explicit_liquid_engine(self):
        expected = object()
        with (
            patch.dict(os.environ, {"PII_DETECTOR_ENGINE": "liquid"}),
            patch("apps.pii.engine.get_deberta_pii_pipeline") as deberta,
            patch("apps.pii.liquid_engine.get_liquid_pii_pipeline", return_value=expected) as liquid,
        ):
            self.assertIs(engine.get_pii_pipeline(), expected)

        liquid.assert_called_once_with()
        deberta.assert_not_called()

    def test_unknown_engine_logs_and_falls_back_to_deberta(self):
        expected = object()
        with (
            patch.dict(os.environ, {"PII_DETECTOR_ENGINE": "experimental"}),
            patch("apps.pii.engine.get_deberta_pii_pipeline", return_value=expected) as deberta,
            patch("apps.pii.liquid_engine.get_liquid_pii_pipeline") as liquid,
            self.assertLogs("apps.pii.engine", level="WARNING") as logs,
        ):
            self.assertIs(engine.get_pii_pipeline(), expected)

        deberta.assert_called_once_with()
        liquid.assert_not_called()
        self.assertIn("Unknown PII_DETECTOR_ENGINE='experimental'", logs.output[0])
        self.assertIn("falling back to deberta", logs.output[0])


class LiquidLabelMappingTests(SimpleTestCase):
    def test_all_40_liquid_types_have_documented_mapping_decisions(self):
        expected = {
            "identity.person_name": "PERSON",
            "identity.ssn": "ID_DOCUMENT",
            "identity.national_id": "ID_DOCUMENT",
            "identity.passport": "ID_DOCUMENT",
            "identity.drivers_license": "ID_DOCUMENT",
            "identity.date_of_birth": "DATE_OF_BIRTH",
            "identity.tax_id": "ID_DOCUMENT",
            "contact.email": "EMAIL_ADDRESS",
            "contact.phone": "PHONE_NUMBER",
            "contact.address": "LOCATION",
            "contact.postal_code": "LOCATION",
            "contact.ip_address": "IP_ADDRESS",
            "financial.credit_card": "CREDIT_CARD",
            "financial.iban": "IBAN_CODE",
            "financial.bank_account": "ACCOUNT",
            "financial.swift_bic": "ACCOUNT",
            "financial.crypto_wallet": "CRYPTO_ADDRESS",
            "financial.amount": None,
            "credential.api_key": "PASSWORD",
            "credential.password": "PASSWORD",
            "credential.private_key": "PASSWORD",
            "credential.jwt": "PASSWORD",
            "credential.connection_string": "PASSWORD",
            "developer.login_credentials": "PASSWORD",
            "online.username": "PERSON",
            "online.url": None,
            "device.mac_address": "IP_ADDRESS",
            "device.imei": "PHONE_NUMBER",
            "developer.device_id": "ID_DOCUMENT",
            "location.gps_coordinates": "LOCATION",
            "healthcare.medical_record": "ID_DOCUMENT",
            "healthcare.health_plan_id": "ID_DOCUMENT",
            "healthcare.condition": None,
            "healthcare.medication": None,
            "org.company_name": None,
            "special.religion": None,
            "special.political": None,
            "special.orientation": None,
            "special.health_status": None,
            "legal.case_number": "ID_DOCUMENT",
        }
        self.assertEqual(LIQUID_LABEL_MAP, expected)
        self.assertEqual(len(LIQUID_LABEL_MAP), 40)

        for raw_label, entity_type in expected.items():
            if entity_type is None:
                self.assertNotIn(raw_label, DEBERTA_LABEL_MAP)
            else:
                self.assertEqual(DEBERTA_LABEL_MAP[raw_label], entity_type)


class EngineIndependentPresidioTests(SimpleTestCase):
    def setUp(self):
        self.pattern_recognizers = engine._pattern_recognizers
        engine._pattern_recognizers = None

    def tearDown(self):
        engine._pattern_recognizers = self.pattern_recognizers

    def test_pattern_recognizer_singleton_is_engine_independent(self):
        with patch.dict(os.environ, {"PII_DETECTOR_ENGINE": "deberta"}):
            deberta_patterns = engine.get_pattern_recognizers()
        with patch.dict(os.environ, {"PII_DETECTOR_ENGINE": "liquid"}):
            liquid_patterns = engine.get_pattern_recognizers()

        self.assertIs(liquid_patterns, deberta_patterns)
        self.assertEqual(
            set(liquid_patterns),
            {"CREDIT_CARD", "IBAN_CODE", "EMAIL_ADDRESS", "PHONE_NUMBER"},
        )

    def test_liquid_no_space_japanese_email_uses_engine_independent_presidio(self):
        text = "明日は田中さんとtaro@example.jpに連絡"
        empty_neural_pipeline = Mock(return_value=[])

        with (
            patch.dict(os.environ, {"PII_DETECTOR_ENGINE": "liquid"}),
            patch(
                "apps.pii.liquid_engine.get_liquid_pii_pipeline",
                return_value=empty_neural_pipeline,
            ) as liquid_loader,
        ):
            result = redact_text(text, tier="starter")

        liquid_loader.assert_called_once_with()
        empty_neural_pipeline.assert_called_once_with(text)
        self.assertNotIn("taro@example.jp", result)
        self.assertEqual(result, "明日は田中さんと[EMAIL_ADDRESS_1]に連絡")


class LiquidEngineSingletonTests(SimpleTestCase):
    def setUp(self):
        self.state = (liquid_engine._pipeline, liquid_engine._pipeline_load_error)
        liquid_engine._pipeline = None
        liquid_engine._pipeline_load_error = None

    def tearDown(self):
        liquid_engine._pipeline, liquid_engine._pipeline_load_error = self.state

    def test_successful_load_is_cached(self):
        expected = object()
        with patch.object(
            liquid_engine,
            "_build_liquid_pii_pipeline",
            return_value=expected,
        ) as build:
            self.assertIs(liquid_engine.get_liquid_pii_pipeline(), expected)
            self.assertIs(liquid_engine.get_liquid_pii_pipeline(), expected)

        build.assert_called_once_with()

    def test_load_error_is_cached_without_retry(self):
        load_error = RuntimeError("synthetic Liquid load failure")
        with (
            patch.object(
                liquid_engine,
                "_build_liquid_pii_pipeline",
                side_effect=load_error,
            ) as build,
            patch.object(liquid_engine.logger, "error") as log_error,
        ):
            with self.assertRaises(RuntimeError) as first:
                liquid_engine.get_liquid_pii_pipeline()
            with self.assertRaises(RuntimeError) as second:
                liquid_engine.get_liquid_pii_pipeline()

        self.assertIs(first.exception, load_error)
        self.assertIs(second.exception, load_error)
        build.assert_called_once_with()
        log_error.assert_called_once()

    def test_adapter_emits_existing_pipeline_span_shape(self):
        decoder = SimpleNamespace(
            predict=Mock(
                return_value=[
                    {
                        "start": 8,
                        "end": 23,
                        "type": "contact.email",
                        "text": "taro@example.jp",
                    }
                ]
            )
        )
        tokenizer = object()
        model = object()
        pipeline = liquid_engine._LiquidPiiPipeline(tokenizer, model, decoder)

        result = pipeline("明日は田中さんとtaro@example.jpに連絡")

        self.assertEqual(
            result,
            [
                {
                    "entity_group": "contact.email",
                    "word": "taro@example.jp",
                    "score": 1.0,
                    "start": 8,
                    "end": 23,
                }
            ],
        )
        decoder.predict.assert_called_once_with(
            "明日は田中さんとtaro@example.jpに連絡",
            tokenizer,
            model,
            hybrid=True,
        )

    def test_remote_model_load_is_revision_pinned_cpu_fp32(self):
        import torch

        tokenizer = object()
        model = Mock()
        model.to.return_value = model
        model.eval.return_value = model
        decoder = object()
        with (
            patch.object(liquid_engine.os.path, "isdir", return_value=False),
            patch.object(liquid_engine, "_load_decoder", return_value=decoder),
            patch(
                "transformers.AutoTokenizer.from_pretrained",
                return_value=tokenizer,
            ) as load_tokenizer,
            patch(
                "transformers.AutoModelForTokenClassification.from_pretrained",
                return_value=model,
            ) as load_model,
        ):
            pipeline = liquid_engine._build_liquid_pii_pipeline()

        expected_kwargs = {
            "revision": liquid_engine.LIQUID_MODEL_REVISION,
            "trust_remote_code": True,
        }
        load_tokenizer.assert_called_once_with(
            liquid_engine.LIQUID_MODEL_REPO,
            **expected_kwargs,
        )
        load_model.assert_called_once_with(
            liquid_engine.LIQUID_MODEL_REPO,
            **expected_kwargs,
        )
        model.to.assert_called_once_with(device="cpu", dtype=torch.float32)
        model.eval.assert_called_once_with()
        self.assertIs(pipeline._tokenizer, tokenizer)
        self.assertIs(pipeline._model, model)
        self.assertIs(pipeline._decoder, decoder)


@skipUnless(
    os.environ.get("PII_REAL_MODEL_TESTS") == "1",
    "Set PII_REAL_MODEL_TESTS=1 to load the pinned Liquid model",
)
class LiquidRealModelTests(SimpleTestCase):
    def test_pinned_liquid_model_smoke(self):
        pipeline = liquid_engine.get_liquid_pii_pipeline()

        results = pipeline("Email Dr. Laura Schmidt at laura@charite.de.")

        self.assertIsInstance(results, list)
        self.assertTrue(any(item["entity_group"] == "identity.person_name" for item in results))
