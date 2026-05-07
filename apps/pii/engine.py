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
# Cached exception from a failed load — re-raised on every subsequent call
# so callers (test setUpClass with try/except, redactor with logging
# throttle) keep their existing semantics. Avoids re-attempting the
# import on every redaction in prod (the source of the original
# log-spam regression).
_pipeline_load_error: Exception | None = None
_pattern_recognizers = None

# HuggingFace repo for the PII model (public, Apache 2.0 compatible).
_HF_MODEL_REPO = "onbekend/nbhd-pii-model"

# Model path — override with PII_MODEL_PATH env var.
# Docker: /app/pii-model (downloaded at build time).
# Local dev: pii-model/ in project root, or auto-downloads from HuggingFace.
_MODEL_PATH = os.environ.get(
    "PII_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "pii-model"),
)


def get_pii_pipeline():
    """Return a shared token-classification pipeline, initializing on first call.

    Raises the original load exception on failure (callers — test
    setUpClass and the redactor — depend on this for the skip path /
    fallback path). The exception is cached on first failure and
    re-raised on subsequent calls without re-attempting the import,
    which avoids the ~5-trace/sec spam pattern observed in prod when
    a dependency is misaligned (PR #447 → 2026-05-07 breakage).
    """
    global _pipeline, _pipeline_load_error
    if _pipeline_load_error is not None:
        raise _pipeline_load_error
    if _pipeline is not None:
        return _pipeline

    try:
        from optimum.onnxruntime import ORTModelForTokenClassification
        from transformers import AutoTokenizer, pipeline

        # Use local path if available, otherwise download from HuggingFace
        model_path = _MODEL_PATH if os.path.isdir(_MODEL_PATH) else _HF_MODEL_REPO
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        model = ORTModelForTokenClassification.from_pretrained(
            model_path,
            file_name="model_quantized.onnx",
        )
        _pipeline = pipeline(
            "token-classification",
            model=model,
            tokenizer=tokenizer,
            aggregation_strategy="simple",
        )
        logger.info("PII detection model loaded (ONNX INT8) from %s", _MODEL_PATH)
    except Exception as exc:
        _pipeline_load_error = exc
        raise

    return _pipeline


def get_pattern_recognizers():
    """Return Presidio pattern recognizers (no NLP engine needed).

    Called directly — bypasses AnalyzerEngine entirely so we
    don't need a spaCy NLP engine or model installed.

    Returns a dict of {entity_type: recognizer} for:
    - CREDIT_CARD: Luhn checksum validation
    - IBAN_CODE: Country-format + checksum validation
    - EMAIL_ADDRESS: Regex fallback (catches emails the model misses)
    """
    global _pattern_recognizers
    if _pattern_recognizers is None:
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            EmailRecognizer,
            IbanRecognizer,
        )

        _pattern_recognizers = {
            "CREDIT_CARD": CreditCardRecognizer(),
            "IBAN_CODE": IbanRecognizer(),
            "EMAIL_ADDRESS": EmailRecognizer(),
        }
        logger.info("Presidio pattern recognizers initialized (credit card, IBAN, email)")
    return _pattern_recognizers
