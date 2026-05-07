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

# Sentinel so we can distinguish "not loaded yet" (None) from
# "loading previously failed, don't keep retrying" (False).
_pipeline: object | None | bool = None
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

    Caches both success and failure: if the import or model load raises
    once (typically a transformers/optimum ABI mismatch — see PR #447 →
    prod breakage 2026-05-07), the sentinel is set to False so subsequent
    calls return None immediately. This prevents the redactor from
    spamming hundreds of identical tracebacks per second when a
    dependency is misaligned.

    Returns ``None`` when the pipeline is unavailable; callers must
    fall back to pattern recognizers + return-original-text.
    """
    global _pipeline
    if _pipeline is False:
        return None
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
    except Exception:
        # Cache the failure so we don't retry on every redaction call.
        # Logged once at error level; further calls return None silently.
        logger.error(
            "PII detection model failed to initialize — disabling neural PII detection "
            "for this process; falling back to pattern recognizers only. Restart the "
            "container after fixing the dependency to retry.",
            exc_info=True,
        )
        _pipeline = False
        return None

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
