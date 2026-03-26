"""Lazy-singleton Presidio engine for PII detection and anonymization.

The AnalyzerEngine loads a spaCy NLP model on first use (~2s, ~200MB RAM).
Subsequent calls reuse the same instance within the Django process.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_analyzer = None
_anonymizer = None


def get_analyzer():
    """Return a shared AnalyzerEngine, initializing on first call."""
    global _analyzer
    if _analyzer is None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        })
        nlp_engine = provider.create_engine()
        _analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
        logger.info("Presidio AnalyzerEngine initialized")
    return _analyzer


def get_anonymizer():
    """Return a shared AnonymizerEngine."""
    global _anonymizer
    if _anonymizer is None:
        from presidio_anonymizer import AnonymizerEngine
        _anonymizer = AnonymizerEngine()
        logger.info("Presidio AnonymizerEngine initialized")
    return _anonymizer
