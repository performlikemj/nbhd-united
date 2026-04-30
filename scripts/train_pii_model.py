"""
Train a DeBERTa-v3-base PII detection model on the ai4privacy dataset.

Usage:
    # On a machine with a GPU (Colab, Azure ML, etc.):
    pip install -r scripts/requirements-pii-training.txt
    python scripts/train_pii_model.py

    # With custom output directory:
    python scripts/train_pii_model.py --output-dir ./pii-model-output

    # Quick test run (1 epoch, 1000 examples):
    python scripts/train_pii_model.py --test-run

The trained model can then be exported to ONNX + quantized via:
    python scripts/export_pii_model.py --checkpoint ./pii-model-output/best
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import torch
from datasets import load_dataset
from seqeval.metrics import classification_report, f1_score
from transformers import (
    AutoModelForTokenClassification,
    AutoTokenizer,
    DataCollatorForTokenClassification,
    EarlyStoppingCallback,
    Trainer,
    TrainingArguments,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label schema
# ---------------------------------------------------------------------------
# These are the PII categories in the ai4privacy dataset.
# Each gets a B- (begin) and I- (inside) tag, plus O (outside).

ENTITY_LABELS = [
    "ACCOUNTNUM",
    "BUILDINGNUM",
    "CITY",
    "CREDITCARDNUMBER",
    "DATEOFBIRTH",
    "DRIVERLICENSENUM",
    "EMAIL",
    "GIVENNAME",
    "IDCARDNUM",
    "IPV4",
    "IPV6",
    "PASSPORT",
    "PASSWORD",
    "SOCIALNUM",
    "STREET",
    "SURNAME",
    "TAXNUM",
    "TELEPHONENUM",
    "USERNAME",
    "ZIPCODE",
]

# Build BIO label list: O, B-ACCOUNTNUM, I-ACCOUNTNUM, B-BUILDINGNUM, ...
BIO_LABELS = ["O"]
for label in ENTITY_LABELS:
    BIO_LABELS.append(f"B-{label}")
    BIO_LABELS.append(f"I-{label}")

LABEL2ID = {label: i for i, label in enumerate(BIO_LABELS)}
ID2LABEL = {i: label for i, label in enumerate(BIO_LABELS)}
NUM_LABELS = len(BIO_LABELS)

# ---------------------------------------------------------------------------
# Dataset preprocessing
# ---------------------------------------------------------------------------

MODEL_NAME = "microsoft/deberta-v3-base"
MAX_LENGTH = 256


def tokenize_and_align_labels(examples, tokenizer):
    """Tokenize text and align character-level PII spans to token-level BIO tags.

    The ai4privacy dataset provides `source_text` (raw text) and `privacy_mask`
    (list of {label, start, end, value} spans). We tokenize with DeBERTa and map
    each token to its BIO label based on character offset overlap.
    """
    tokenized = tokenizer(
        examples["source_text"],
        truncation=True,
        max_length=MAX_LENGTH,
        padding=False,
        return_offsets_mapping=True,
    )

    all_labels = []

    for i, offsets in enumerate(tokenized["offset_mapping"]):
        spans = examples["privacy_mask"][i]

        # Parse spans if they're stored as a JSON string
        if isinstance(spans, str):
            spans = json.loads(spans)

        # Build a sorted list of (start, end, label) from privacy_mask
        pii_spans = []
        for span in spans:
            pii_spans.append((span["start"], span["end"], span["label"]))
        pii_spans.sort(key=lambda s: s[0])

        labels = []
        for token_idx, (tok_start, tok_end) in enumerate(offsets):
            # Special tokens (CLS, SEP, PAD) have (0, 0) offsets
            if tok_start == 0 and tok_end == 0:
                labels.append(-100)  # Ignored in loss
                continue

            # Find which PII span (if any) this token belongs to
            matched_label = None
            is_begin = False

            for span_start, span_end, span_label in pii_spans:
                # Check overlap: token overlaps with span
                if tok_start < span_end and tok_end > span_start:
                    matched_label = span_label
                    # Token is "begin" if it starts at or before the span start
                    is_begin = tok_start <= span_start
                    break

            if matched_label is None:
                labels.append(LABEL2ID["O"])
            elif is_begin:
                bio_label = f"B-{matched_label}"
                labels.append(LABEL2ID.get(bio_label, LABEL2ID["O"]))
            else:
                bio_label = f"I-{matched_label}"
                labels.append(LABEL2ID.get(bio_label, LABEL2ID["O"]))

        all_labels.append(labels)

    tokenized["labels"] = all_labels
    # Remove offset_mapping — not needed for training
    tokenized.pop("offset_mapping")

    return tokenized


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_metrics(eval_pred):
    """Compute seqeval F1 for token classification."""
    predictions, labels = eval_pred
    predictions = np.argmax(predictions, axis=2)

    # Convert IDs back to label strings, skipping -100
    true_labels = []
    pred_labels = []

    for pred_seq, label_seq in zip(predictions, labels):
        true_seq = []
        pred_seq_filtered = []
        for p, l in zip(pred_seq, label_seq):
            if l == -100:
                continue
            true_seq.append(ID2LABEL[l])
            pred_seq_filtered.append(ID2LABEL[p])
        true_labels.append(true_seq)
        pred_labels.append(pred_seq_filtered)

    f1 = f1_score(true_labels, pred_labels, average="weighted")

    return {
        "f1": f1,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Train PII detection model")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./pii-model-output",
        help="Directory for checkpoints and final model",
    )
    parser.add_argument(
        "--test-run",
        action="store_true",
        help="Quick test: 1 epoch, 1000 training examples",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=7,
        help="Number of training epochs (default: 7)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Training batch size (default: 8, reduce if OOM)",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=2e-5,
        help="Learning rate (default: 2e-5, standard for DeBERTa fine-tuning)",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="en",
        help="Filter dataset to a specific language (e.g., 'en'). Default: en.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ----- Device detection -----
    if torch.cuda.is_available():
        logger.info("Using CUDA backend")
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        logger.info("Using MPS backend (Apple Silicon)")
    else:
        logger.info("Using CPU backend (training will be slow)")

    # ----- Load dataset -----
    logger.info("Loading ai4privacy/pii-masking-400k dataset...")
    dataset = load_dataset("ai4privacy/pii-masking-400k")

    # The dataset has "train" and "validation" splits at the HuggingFace level
    train_dataset = dataset["train"]
    val_dataset = dataset["validation"]

    # Filter by language if specified
    if args.language:
        logger.info(f"Filtering to language: {args.language}")
        train_dataset = train_dataset.filter(lambda x: x["language"] == args.language)
        val_dataset = val_dataset.filter(lambda x: x["language"] == args.language)

    if args.test_run:
        train_dataset = train_dataset.select(range(min(1000, len(train_dataset))))
        val_dataset = val_dataset.select(range(min(200, len(val_dataset))))

    logger.info(f"Train: {len(train_dataset)} examples, Val: {len(val_dataset)} examples")

    # ----- Tokenizer -----
    logger.info(f"Loading tokenizer: {MODEL_NAME}")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    # ----- Tokenize and align labels -----
    logger.info("Tokenizing and aligning labels...")
    train_tokenized = train_dataset.map(
        lambda examples: tokenize_and_align_labels(examples, tokenizer),
        batched=True,
        remove_columns=train_dataset.column_names,
        desc="Tokenizing train",
    )
    val_tokenized = val_dataset.map(
        lambda examples: tokenize_and_align_labels(examples, tokenizer),
        batched=True,
        remove_columns=val_dataset.column_names,
        desc="Tokenizing val",
    )

    # ----- Model -----
    logger.info(f"Loading model: {MODEL_NAME} with {NUM_LABELS} labels")
    model = AutoModelForTokenClassification.from_pretrained(
        MODEL_NAME,
        num_labels=NUM_LABELS,
        id2label=ID2LABEL,
        label2id=LABEL2ID,
    )

    # ----- Training -----
    training_args = TrainingArguments(
        output_dir=str(output_dir),
        # Hyperparameters (matching Isotonic's config that achieved 97.6% F1)
        num_train_epochs=1 if args.test_run else args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=2,  # effective batch = batch_size * 2
        learning_rate=args.learning_rate,
        weight_decay=0.01,
        warmup_ratio=0.1,
        lr_scheduler_type="cosine_with_restarts",
        # Evaluation
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        greater_is_better=True,
        # Performance — MPS does not support AMP fp16 via Trainer;
        # multi-process data loading hangs on macOS Metal
        fp16=torch.cuda.is_available(),
        dataloader_num_workers=0 if (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()) else 4,
        # Logging
        logging_steps=100,
        report_to="none",
        # Save
        save_total_limit=3,
    )

    data_collator = DataCollatorForTokenClassification(
        tokenizer=tokenizer,
        padding=True,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_tokenized,
        eval_dataset=val_tokenized,
        data_collator=data_collator,
        compute_metrics=compute_metrics,
        callbacks=[EarlyStoppingCallback(early_stopping_patience=3)],
    )

    logger.info("Starting training...")
    trainer.train()

    # ----- Save best model -----
    best_dir = output_dir / "best"
    trainer.save_model(str(best_dir))
    tokenizer.save_pretrained(str(best_dir))

    # Save label mapping
    label_map = {"id2label": ID2LABEL, "label2id": LABEL2ID}
    (best_dir / "label_map.json").write_text(json.dumps(label_map, indent=2))

    logger.info(f"Best model saved to {best_dir}")

    # ----- Final evaluation -----
    logger.info("Running final evaluation...")
    eval_results = trainer.evaluate()
    logger.info(f"Eval results: {eval_results}")

    # Detailed classification report
    predictions = trainer.predict(val_tokenized)
    preds = np.argmax(predictions.predictions, axis=2)
    labels = predictions.label_ids

    true_labels = []
    pred_labels = []
    for pred_seq, label_seq in zip(preds, labels):
        true_seq = []
        pred_seq_filtered = []
        for p, l in zip(pred_seq, label_seq):
            if l == -100:
                continue
            true_seq.append(ID2LABEL[l])
            pred_seq_filtered.append(ID2LABEL[p])
        true_labels.append(true_seq)
        pred_labels.append(pred_seq_filtered)

    report = classification_report(true_labels, pred_labels)
    logger.info(f"\n{report}")

    # Save report
    (output_dir / "classification_report.txt").write_text(report)
    logger.info(f"Classification report saved to {output_dir / 'classification_report.txt'}")


if __name__ == "__main__":
    main()
