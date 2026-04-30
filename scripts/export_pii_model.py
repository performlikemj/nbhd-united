"""
Export a trained PII model to ONNX and quantize to INT8.

Usage:
    python scripts/export_pii_model.py --checkpoint ./pii-model-output/best

This produces:
    ./pii-model-output/onnx/model.onnx          (~800 MB, FP32)
    ./pii-model-output/onnx-quantized/model.onnx (~200 MB, INT8)

The quantized model is what you deploy in the Django container.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def export_to_onnx(checkpoint_dir: Path, output_dir: Path):
    """Export the PyTorch model to ONNX format."""
    from optimum.onnxruntime import ORTModelForTokenClassification

    logger.info(f"Exporting {checkpoint_dir} to ONNX...")

    model = ORTModelForTokenClassification.from_pretrained(
        checkpoint_dir,
        export=True,
    )
    model.save_pretrained(str(output_dir))

    # Copy tokenizer files
    for f in checkpoint_dir.glob("tokenizer*"):
        shutil.copy2(f, output_dir / f.name)
    for f in checkpoint_dir.glob("special_tokens*"):
        shutil.copy2(f, output_dir / f.name)
    spm = checkpoint_dir / "spm.model"
    if spm.exists():
        shutil.copy2(spm, output_dir / spm.name)

    logger.info(f"ONNX model saved to {output_dir}")
    onnx_size = sum(f.stat().st_size for f in output_dir.rglob("*.onnx"))
    logger.info(f"ONNX model size: {onnx_size / 1024 / 1024:.1f} MB")


def quantize_onnx(onnx_dir: Path, output_dir: Path):
    """Quantize ONNX model to INT8 (dynamic quantization)."""
    from optimum.onnxruntime import ORTQuantizer
    from optimum.onnxruntime.configuration import AutoQuantizationConfig

    logger.info("Quantizing ONNX model to INT8...")

    quantizer = ORTQuantizer.from_pretrained(str(onnx_dir))
    qconfig = AutoQuantizationConfig.avx512_vnni(is_static=False, per_channel=True)

    quantizer.quantize(
        save_dir=str(output_dir),
        quantization_config=qconfig,
    )

    # Copy tokenizer files
    for f in onnx_dir.glob("tokenizer*"):
        shutil.copy2(f, output_dir / f.name)
    for f in onnx_dir.glob("special_tokens*"):
        shutil.copy2(f, output_dir / f.name)
    spm = onnx_dir / "spm.model"
    if spm.exists():
        shutil.copy2(spm, output_dir / spm.name)
    config = onnx_dir / "config.json"
    if config.exists():
        shutil.copy2(config, output_dir / config.name)

    logger.info(f"Quantized model saved to {output_dir}")
    quant_size = sum(f.stat().st_size for f in output_dir.rglob("*.onnx"))
    logger.info(f"Quantized model size: {quant_size / 1024 / 1024:.1f} MB")


def verify_model(quantized_dir: Path):
    """Quick smoke test: run inference on a sample text."""
    from optimum.onnxruntime import ORTModelForTokenClassification
    from transformers import AutoTokenizer, pipeline

    logger.info("Verifying quantized model...")

    tokenizer = AutoTokenizer.from_pretrained(str(quantized_dir))
    model = ORTModelForTokenClassification.from_pretrained(str(quantized_dir))

    nlp = pipeline("token-classification", model=model, tokenizer=tokenizer, aggregation_strategy="simple")

    test_texts = [
        "My name is Sarah Chen and my email is sarah.chen@acme.com",
        "Call me at 415-555-0199, my SSN is 123-45-6789",
        "Send payment to account 4532015112830366 at 123 Main St, Brooklyn NY 11201",
        "Jordan called from Georgia about the meeting with Dr. Smith",
    ]

    for text in test_texts:
        entities = nlp(text)
        logger.info(f"\nText: {text}")
        for ent in entities:
            logger.info(f"  {ent['word']:30s} -> {ent['entity_group']:20s} ({ent['score']:.3f})")

    logger.info("\nVerification complete.")


def main():
    parser = argparse.ArgumentParser(description="Export PII model to ONNX + INT8")
    parser.add_argument(
        "--checkpoint",
        type=str,
        required=True,
        help="Path to the trained PyTorch checkpoint (e.g., ./pii-model-output/best)",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip the verification smoke test",
    )
    args = parser.parse_args()

    checkpoint_dir = Path(args.checkpoint)
    if not checkpoint_dir.exists():
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint_dir}")

    base_dir = checkpoint_dir.parent
    onnx_dir = base_dir / "onnx"
    quantized_dir = base_dir / "onnx-quantized"

    # Step 1: Export to ONNX
    export_to_onnx(checkpoint_dir, onnx_dir)

    # Step 2: Quantize to INT8
    quantize_onnx(onnx_dir, quantized_dir)

    # Step 3: Verify
    if not args.skip_verify:
        verify_model(quantized_dir)

    logger.info(f"\n{'=' * 60}")
    logger.info("Done! Deploy the quantized model from:")
    logger.info(f"  {quantized_dir}")
    logger.info("")
    logger.info("To use in Django, copy this directory to your Docker image")
    logger.info("or upload to Azure Blob Storage / HuggingFace Hub.")
    logger.info(f"{'=' * 60}")


if __name__ == "__main__":
    main()
