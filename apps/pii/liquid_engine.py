"""Pinned LiquidAI PII detector running as a lazy CPU/FP32 singleton."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LIQUID_MODEL_REPO = "LiquidAI/LFM2.5-Encoder-350M-PII-Detector"
LIQUID_MODEL_REVISION = "b8c9cf3d2d6ae52501b35a27ba46f271449c9ce2"

_MODEL_ROOT = os.environ.get(
    "PII_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "pii-model"),
)
_MODEL_PATH = os.path.join(_MODEL_ROOT, "liquid")

_pipeline: object | None = None
_pipeline_load_error: Exception | None = None


class _LiquidPiiPipeline:
    """Adapt Liquid's hybrid decoder to the existing HF-pipeline span shape."""

    def __init__(self, tokenizer: object, model: object, decoder: object):
        self._tokenizer = tokenizer
        self._model = model
        self._decoder = decoder

    def __call__(self, text: str) -> list[dict[str, Any]]:
        spans = self._decoder.predict(
            text,
            self._tokenizer,
            self._model,
            hybrid=True,
        )
        return [
            {
                "entity_group": str(span["type"]),
                "word": text[int(span["start"]) : int(span["end"])],
                # The shipped hybrid decoder combines neural and deterministic
                # spans but does not expose confidences. A returned span is an
                # accepted detection, so it clears the downstream score gate.
                "score": 1.0,
                "start": int(span["start"]),
                "end": int(span["end"]),
            }
            for span in spans
        ]


def _load_decoder(model_source: str):
    if os.path.isdir(model_source):
        helper_path = Path(model_source) / "pii_hybrid_decode.py"
        context_path = Path(model_source) / "context_cued.py"
        if not helper_path.is_file() or not context_path.is_file():
            raise FileNotFoundError("Liquid model artifact is missing pii_hybrid_decode.py or context_cued.py")
    else:
        from huggingface_hub import hf_hub_download

        helper_path = Path(
            hf_hub_download(
                LIQUID_MODEL_REPO,
                "pii_hybrid_decode.py",
                revision=LIQUID_MODEL_REVISION,
            )
        )
        context_path = Path(
            hf_hub_download(
                LIQUID_MODEL_REPO,
                "context_cued.py",
                revision=LIQUID_MODEL_REVISION,
            )
        )

    helper_dir = str(helper_path.parent)
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)

    context_spec = importlib.util.spec_from_file_location("context_cued", context_path)
    if context_spec is None or context_spec.loader is None:
        raise RuntimeError(f"Could not load Liquid context helper from {context_path}")
    context_module = importlib.util.module_from_spec(context_spec)
    # pii_hybrid_decode.py imports this exact top-level name. Install the module
    # from the same pinned snapshot before executing the decoder so an unrelated
    # context_cued module elsewhere on sys.path can never win.
    sys.modules["context_cued"] = context_module
    context_spec.loader.exec_module(context_module)

    spec = importlib.util.spec_from_file_location(
        "pinned_liquid_pii_hybrid_decode",
        helper_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Liquid decoder from {helper_path}")
    decoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decoder)
    return decoder


def _build_liquid_pii_pipeline() -> _LiquidPiiPipeline:
    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    model_source = _MODEL_PATH if os.path.isdir(_MODEL_PATH) else LIQUID_MODEL_REPO
    remote_kwargs = {}
    if model_source == LIQUID_MODEL_REPO:
        remote_kwargs["revision"] = LIQUID_MODEL_REVISION

    decoder = _load_decoder(model_source)
    tokenizer = AutoTokenizer.from_pretrained(
        model_source,
        trust_remote_code=True,
        **remote_kwargs,
    )
    model = AutoModelForTokenClassification.from_pretrained(
        model_source,
        trust_remote_code=True,
        **remote_kwargs,
    )
    model = model.to(device="cpu", dtype=torch.float32).eval()
    logger.info("Liquid PII detection model loaded from %s", model_source)
    return _LiquidPiiPipeline(tokenizer, model, decoder)


def get_liquid_pii_pipeline():
    """Return the shared Liquid callable, caching success or load failure.

    The remote fallback pins weights, tokenizer, custom model code, and hybrid
    decode helpers to ``LIQUID_MODEL_REVISION``. Production uses the identically
    pinned artifact baked beneath ``PII_MODEL_PATH/liquid``.
    """
    global _pipeline, _pipeline_load_error
    if _pipeline_load_error is not None:
        raise _pipeline_load_error
    if _pipeline is not None:
        return _pipeline

    try:
        _pipeline = _build_liquid_pii_pipeline()
    except Exception as exc:
        logger.error(
            "Liquid PII model failed to initialize — disabling neural PII detection "
            "for this process; falling back to pattern recognizers only. Restart the "
            "container after fixing the dependency to retry.",
            exc_info=True,
        )
        _pipeline_load_error = exc
        raise
    return _pipeline
