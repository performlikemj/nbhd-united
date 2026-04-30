"""Lazy-singleton PII detection engines.

Uses a custom DeBERTa model (ONNX INT8) for contextual PII detection
and Presidio pattern recognizers for deterministic financial PII
(credit card Luhn checksum, IBAN country-format validation).

The DeBERTa model loads on first use (~230 MB RAM). Subsequent calls
reuse the same instance within the Django process. ONNX Runtime uses
mmap for model weights, so memory is shared across gunicorn workers
via the OS page cache.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_pipeline = None
_cc_recognizer = None
_iban_recognizer = None

# Default model path — override with PII_MODEL_PATH env var.
# In Docker, set to /app/pii-model (baked into image).
# Locally, point to pii-model/ in the project root.
_MODEL_PATH = os.environ.get(
    "PII_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "pii-model"),
)


def get_pii_pipeline():
    """Return a shared token-classification pipeline, initializing on first call."""
    global _pipeline
    if _pipeline is None:
        from optimum.onnxruntime import ORTModelForTokenClassification
        from transformers import AutoTokenizer, pipeline

        tokenizer = AutoTokenizer.from_pretrained(_MODEL_PATH)
        model = ORTModelForTokenClassification.from_pretrained(
            _MODEL_PATH,
            file_name="model_quantized.onnx",
        )
        _pipeline = pipeline(
            "token-classification",
            model=model,
            tokenizer=tokenizer,
            aggregation_strategy="simple",
        )
        logger.info("PII detection model loaded (ONNX INT8) from %s", _MODEL_PATH)
    return _pipeline


def get_pattern_recognizers():
    """Return Presidio credit card and IBAN recognizers (no NLP engine needed).

    These are called directly — bypasses AnalyzerEngine entirely so we
    don't need a spaCy NLP engine or model installed.
    """
    global _cc_recognizer, _iban_recognizer
    if _cc_recognizer is None:
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            IbanRecognizer,
        )

        _cc_recognizer = CreditCardRecognizer()
        _iban_recognizer = IbanRecognizer()
        logger.info("Presidio pattern recognizers initialized (credit card, IBAN)")
    return _cc_recognizer, _iban_recognizer
