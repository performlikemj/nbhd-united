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
_pipeline_failure_logged = False
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

    On load failure, returns ``None`` so callers fall back to pattern
    recognizers. Logs the underlying error at most once per process to
    avoid spamming tracebacks when a dependency is misaligned (see
    PR #447 → prod breakage 2026-05-07: transformers 5.8 / optimum 1.17
    ABI mismatch caused ~5 tracebacks/sec).

    Subsequent calls retry the load. This is a deliberate trade-off:
    - In prod under a persistent ABI mismatch, the throttled log keeps
      noise low (one-time error, not per-call).
    - In tests / dev where the failure may be a transient network blip
      against HuggingFace, retries succeed once the blip clears.
    """
    global _pipeline, _pipeline_failure_logged
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
        _pipeline_failure_logged = False
        logger.info("PII detection model loaded (ONNX INT8) from %s", _MODEL_PATH)
    except Exception:
        if not _pipeline_failure_logged:
            logger.error(
                "PII detection model failed to initialize — falling back to "
                "pattern recognizers only. Will retry on next call but not "
                "re-log this traceback for the lifetime of this process.",
                exc_info=True,
            )
            _pipeline_failure_logged = True
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
