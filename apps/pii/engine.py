"""Flag-gated lazy-singleton PII detection engines.

Uses either the production DeBERTa-v3 token-classification model
(``lakshyakh93/deberta_finetuned_pii``) or the pinned LiquidAI detector,
plus one engine-independent set of Presidio pattern recognizers.

The DeBERTa model loads on first use (~554 MB on disk, ~600 MB RAM)
via vanilla PyTorch on CPU. We deliberately do NOT route through
``optimum.onnxruntime``: the prior INT8-quantized ONNX path produced
DIFFERENT detection outputs on Linux x86 (CI / prod) than on macOS
(developer machines), and the optimum / transformers / onnxruntime
ABI churn caused a cold-start ImportError on prod from 2026-05-07
through restoration (issue #695). Vanilla PyTorch CPU inference is
deterministic across both platforms and frees us from the optimum
dependency triangle entirely.
"""

from __future__ import annotations

import logging
import os

from apps.pii.config import (
    DEFAULT_DETECTOR_ENGINE,
    SUPPORTED_DETECTOR_ENGINES,
    resolve_detector_engine,
)

logger = logging.getLogger(__name__)

_pipeline: object | None = None
# Cache the load-time exception so we can re-raise it on subsequent calls
# (cheap, no retry storm) while still letting callers handle the failure.
_pipeline_load_error: Exception | None = None
_pattern_recognizers = None

_DETECTOR_ENGINE_ENV = "PII_DETECTOR_ENGINE"
_warned_unknown_detector_engines: set[str] = set()

# HuggingFace repo for the PII model. ``lakshyakh93/deberta_finetuned_pii``
# is DeBERTa-v3-base fine-tuned on ai4privacy (Apache 2.0). 554 MB safetensors
# weights. See apps/pii/config.py for the label → entity-type mapping.
_HF_MODEL_REPO = "lakshyakh93/deberta_finetuned_pii"

# Presidio's PhoneRecognizer emits a flat 0.4 score for every match regardless
# of how strong the libphonenumber validation was — that sits *below* our 0.5
# tier threshold, so wired as-is it would suppress every phone number. The real
# gate is phonenumbers' VALID leniency (the recognizer's default): it only
# matches strings that parse as a valid number for one of the configured
# regions, which is why bare lift/rep/PIN digit-runs ("5x5 at 315",
# "door code 7391", "PIN 4821") never match. So we lift the emitted score above
# threshold to let validated phone numbers redact. See config.py score_threshold.
_PHONE_RECOGNIZER_SCORE = 0.85

# National-format phone numbers we validate, in addition to any international
# (+CC) number — libphonenumber matches those regardless of the region list.
# Covers the tenant base (US/GB/JP) plus common international correspondents.
_PHONE_SUPPORTED_REGIONS = ("US", "GB", "JP", "CA", "AU", "DE", "FR", "IN", "BR", "IL")

# Model path — override with PII_MODEL_PATH env var.
# Docker: /app/pii-model (downloaded at build time).
# Local dev: pii-model/ in project root, or auto-downloads from HuggingFace.
_MODEL_PATH = os.environ.get(
    "PII_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "pii-model"),
)


def get_pii_detector_engine() -> str:
    """Return the configured neural engine name, failing safely to DeBERTa."""
    requested = os.environ.get(_DETECTOR_ENGINE_ENV, DEFAULT_DETECTOR_ENGINE)
    normalized = requested.strip().lower()
    resolved = resolve_detector_engine(requested)
    if normalized in SUPPORTED_DETECTOR_ENGINES:
        return resolved

    if requested not in _warned_unknown_detector_engines:
        logger.warning(
            "Unknown %s=%r; falling back to %s",
            _DETECTOR_ENGINE_ENV,
            requested,
            DEFAULT_DETECTOR_ENGINE,
        )
        _warned_unknown_detector_engines.add(requested)
    return resolved


def get_deberta_pii_pipeline():
    """Return the shared DeBERTa pipeline, initializing it on first call.

    Caches both success and failure: if the model load raises once
    (missing weights, OOM, etc.), the exception is cached and re-raised
    on subsequent calls. Callers (the redactor) catch this and continue
    with pattern recognizers only — no retry storm, no traceback spam.

    Raises the cached load error when the pipeline is unavailable.
    """
    global _pipeline, _pipeline_load_error
    if _pipeline_load_error is not None:
        raise _pipeline_load_error
    if _pipeline is not None:
        return _pipeline

    try:
        from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline

        # Use local path if available, otherwise download from HuggingFace
        model_path = _MODEL_PATH if os.path.isdir(_MODEL_PATH) else _HF_MODEL_REPO
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        model = AutoModelForTokenClassification.from_pretrained(model_path)
        _pipeline = pipeline(
            "token-classification",
            model=model,
            tokenizer=tokenizer,
            aggregation_strategy="simple",
            device="cpu",
        )
        logger.info("DeBERTa PII detection model loaded from %s", model_path)
    except Exception as exc:
        # Logged once at error level here; subsequent callers catch the
        # re-raised exception silently and fall back to pattern recognizers.
        logger.error(
            "PII detection model failed to initialize — disabling neural PII detection "
            "for this process; falling back to pattern recognizers only. Restart the "
            "container after fixing the dependency to retry.",
            exc_info=True,
        )
        _pipeline_load_error = exc
        raise

    return _pipeline


def get_pii_pipeline():
    """Return the selected engine's shared token-classification callable.

    Each implementation owns an independent lazy singleton and cached load
    error. A selected model failure is therefore re-raised until process
    restart; callers can continue with the shared Presidio recognizers.
    """
    if get_pii_detector_engine() == "liquid":
        from apps.pii.liquid_engine import get_liquid_pii_pipeline

        return get_liquid_pii_pipeline()
    return get_deberta_pii_pipeline()


def get_pattern_recognizers():
    """Return Presidio pattern recognizers (no NLP engine needed).

    Called directly — bypasses AnalyzerEngine entirely so we
    don't need a spaCy NLP engine or model installed.

    Returns a dict of {entity_type: recognizer} for:
    - CREDIT_CARD: Luhn checksum validation
    - IBAN_CODE: Country-format + checksum validation
    - EMAIL_ADDRESS: Regex fallback (catches emails the model misses)
    - PHONE_NUMBER: libphonenumber VALID-leniency validation (the DeBERTa
      model detects phones only inconsistently, so common formats leaked)
    """
    global _pattern_recognizers
    if _pattern_recognizers is None:
        import regex
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            EmailRecognizer,
            IbanRecognizer,
            PhoneRecognizer,
        )

        phone_recognizer = PhoneRecognizer(supported_regions=_PHONE_SUPPORTED_REGIONS)

        class _DualBoundaryEmailRecognizer:
            """Run Presidio's stock email regex with Unicode and ASCII boundaries."""

            def __init__(self):
                self._unicode = EmailRecognizer()
                self._ascii = EmailRecognizer()
                # Presidio's stock pattern uses ``\b``. Unicode semantics have
                # no boundary between Japanese text and ``taro`` in
                # ``さんとtaro@example.jpに``; the ASCII pass catches that case.
                # Keeping a separate stock recognizer preserves all existing
                # Unicode-boundary behavior and avoids shared regex-cache races.
                self._ascii.global_regex_flags |= regex.ASCII

            def analyze(self, *, text, entities):
                unique = {}
                for result in self._unicode.analyze(text=text, entities=entities):
                    unique[(result.entity_type, result.start, result.end)] = result
                for result in self._ascii.analyze(text=text, entities=entities):
                    key = (result.entity_type, result.start, result.end)
                    previous = unique.get(key)
                    if previous is None or result.score > previous.score:
                        unique[key] = result
                return sorted(unique.values(), key=lambda result: (result.start, result.end))

        email_recognizer = _DualBoundaryEmailRecognizer()
        # Override Presidio's flat 0.4 so validated numbers clear the tier
        # threshold; libphonenumber validation is the real gate (see constant).
        phone_recognizer.SCORE = _PHONE_RECOGNIZER_SCORE

        _pattern_recognizers = {
            "CREDIT_CARD": CreditCardRecognizer(),
            "IBAN_CODE": IbanRecognizer(),
            "EMAIL_ADDRESS": email_recognizer,
            "PHONE_NUMBER": phone_recognizer,
        }
        logger.info("Presidio pattern recognizers initialized (credit card, IBAN, email, phone)")
    return _pattern_recognizers
