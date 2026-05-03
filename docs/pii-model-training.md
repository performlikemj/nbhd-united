# Training a Custom PII Detection Model

## Why

We route tenant data through OpenRouter, which includes Chinese-hosted models (DeepSeek, Qwen). PII must be redacted before it leaves Django. Our current engine (Presidio + spaCy) has ~75-80% F1 on names, requires a manual denylist for false positives (Jordan, Georgia, etc.), and misses categories like passwords and dates of birth.

The best open-source PII models (Isotonic DeBERTa, Piiranha) achieve 93-97% F1 but are **Non-Commercial licensed** (CC-BY-NC). We can't use them in production.

**Solution**: Fine-tune our own DeBERTa-v3-base on the ai4privacy dataset. The base model (MIT) and dataset (Apache 2.0) are both commercially licensed. A model trained on the same data achieves ~97% F1.

## What You Get

| Metric | Current (Presidio + spaCy) | After (DeBERTa custom) |
|---|---|---|
| F1 (names) | ~75-80% | ~97% |
| F1 (overall) | varies | ~97% |
| False positive denylist | 24 manual entries | Not needed |
| PII categories | 6 | 20+ |
| Model size (production) | 62 MB (spaCy) | ~200 MB (ONNX INT8) |
| Container resources | 1 vCPU / 2 GiB | 1 vCPU / 2 GiB (no change) |
| Monthly cost delta | — | $0 |
| License | MIT | MIT / Apache 2.0 |

## Prerequisites

- A machine with a GPU (any of these work):
  - **Google Colab** (free tier T4 GPU is sufficient)
  - **Azure ML** compute instance (Standard_NC4as_T4_v3, ~$0.53/hr)
  - **Any machine** with an NVIDIA GPU (8+ GB VRAM) and CUDA
- ~2 hours of GPU time for full training (7 epochs, 326K examples)
- ~10 minutes for ONNX export + quantization

## Step 1: Setup

```bash
# Clone the repo (or upload the scripts to Colab)
git clone <your-repo> && cd nbhd-united

# Install training dependencies (NOT in the Django venv)
pip install -r scripts/requirements-pii-training.txt
```

On **Google Colab**, run this cell first:
```python
!pip install torch transformers datasets accelerate seqeval "optimum[onnxruntime]" onnxruntime "numpy<2"
```

## Step 2: Train

```bash
# Full training (~2 hours on T4 GPU)
python scripts/train_pii_model.py --output-dir ./pii-model-output

# Quick test run first (5 min, verifies everything works)
python scripts/train_pii_model.py --test-run --output-dir ./pii-model-test

# English only (faster, still good — most tenant data is English)
python scripts/train_pii_model.py --language en --output-dir ./pii-model-output
```

### What the script does

1. Downloads the [ai4privacy/pii-masking-400k](https://huggingface.co/datasets/ai4privacy/pii-masking-400k) dataset (407K examples, 20+ PII categories)
2. Tokenizes with DeBERTa and aligns character-level PII spans to BIO token labels
3. Fine-tunes `microsoft/deberta-v3-base` (86M params, MIT license) for token classification
4. Uses the same hyperparameters as the Isotonic model that achieved 97.6% F1:
   - LR: 6e-4, Adam (β1=0.96, β2=0.996), cosine restarts, 7 epochs
5. Saves the best checkpoint to `./pii-model-output/best/`
6. Prints a per-entity classification report

### If you run out of GPU memory

Reduce batch size:
```bash
python scripts/train_pii_model.py --batch-size 16  # or 8
```

## Step 3: Export to ONNX + Quantize

```bash
python scripts/export_pii_model.py --checkpoint ./pii-model-output/best
```

This produces:
```
pii-model-output/
  best/                  # PyTorch checkpoint (~800 MB)
  onnx/                  # ONNX FP32 (~800 MB)
  onnx-quantized/        # ONNX INT8 (~200 MB) ← deploy this
  classification_report.txt
```

The script runs a smoke test on sample texts including the "Jordan called from Georgia" case that trips up spaCy.

## Step 4: Upload the Model

Push the quantized model to a private HuggingFace repo (easiest for Docker pulls):

```bash
# Login to HuggingFace
huggingface-cli login

# Create a private repo and push
huggingface-cli repo create nbhd-pii-model --private
cd pii-model-output/onnx-quantized
huggingface-cli upload nbhd-united/nbhd-pii-model . .
```

Or upload to Azure Blob Storage if you prefer.

## Step 5: Integrate into Django

### 5a. Update requirements

In `requirements.in`, replace the spaCy block:
```diff
-# PII redaction (outgoing LLM traffic)
-presidio-analyzer>=2.2
-presidio-anonymizer>=2.2
-spacy>=3.7
+# PII redaction (outgoing LLM traffic)
+presidio-analyzer>=2.2    # Keep: regex recognizers for credit cards, IBANs
+presidio-anonymizer>=2.2  # Keep: anonymization engine
+optimum[onnxruntime]>=1.14
+onnxruntime>=1.16
+transformers>=4.35
```

### 5b. Update Dockerfile

```diff
 RUN pip install --no-cache-dir -r requirements.txt \
-    && python -m spacy download en_core_web_sm
+    && python -c "from optimum.onnxruntime import ORTModelForTokenClassification; ORTModelForTokenClassification.from_pretrained('nbhd-united/nbhd-pii-model')"
```

### 5c. Update `apps/pii/engine.py`

Replace the spaCy-based analyzer with the ONNX model:

```python
"""Lazy-singleton PII detection engine.

Uses a custom DeBERTa model (ONNX INT8) for contextual PII detection
and Presidio regex recognizers for deterministic financial PII.
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

_model = None
_tokenizer = None
_pipeline = None
_analyzer = None  # Presidio regex-only analyzer


def get_pii_pipeline():
    """Return a shared token-classification pipeline (lazy init)."""
    global _model, _tokenizer, _pipeline
    if _pipeline is None:
        from optimum.onnxruntime import ORTModelForTokenClassification
        from transformers import AutoTokenizer, pipeline

        model_id = "nbhd-united/nbhd-pii-model"  # or local path
        _tokenizer = AutoTokenizer.from_pretrained(model_id)
        _model = ORTModelForTokenClassification.from_pretrained(model_id)
        _pipeline = pipeline(
            "token-classification",
            model=_model,
            tokenizer=_tokenizer,
            aggregation_strategy="simple",
        )
        logger.info("PII detection model loaded (ONNX INT8)")
    return _pipeline


def get_regex_analyzer():
    """Return a Presidio AnalyzerEngine with ONLY regex recognizers (no spaCy).

    Covers: CREDIT_CARD (Luhn), IBAN_CODE (checksum), PHONE_NUMBER, EMAIL_ADDRESS.
    """
    global _analyzer
    if _analyzer is None:
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry

        registry = RecognizerRegistry()
        registry.load_predefined_recognizers()
        # Remove the spaCy-based NER recognizer — DeBERTa handles those
        registry.remove_recognizer("SpacyRecognizer")
        _analyzer = AnalyzerEngine(registry=registry, nlp_engine=None)
        logger.info("Presidio regex-only AnalyzerEngine initialized")
    return _analyzer
```

### 5d. Update `apps/pii/redactor.py`

The `_redact()` function currently calls `analyzer.analyze()`. Swap it to use both engines:

```python
def _detect_pii(text, entities, score_threshold, allow_names):
    """Detect PII using DeBERTa (contextual) + Presidio regex (financial)."""
    results = []

    # 1. DeBERTa model — names, addresses, dates, passwords, etc.
    pii_pipeline = get_pii_pipeline()
    model_results = pii_pipeline(text)
    for ent in model_results:
        if ent["score"] < score_threshold:
            continue
        # Map DeBERTa labels to Presidio-style entity types
        entity_type = DEBERTA_TO_ENTITY.get(ent["entity_group"])
        if entity_type and entity_type in entities:
            results.append(DetectedEntity(
                entity_type=entity_type,
                start=ent["start"],
                end=ent["end"],
                score=ent["score"],
            ))

    # 2. Presidio regex — credit cards (Luhn), IBANs (checksum)
    regex_analyzer = get_regex_analyzer()
    regex_entities = ["CREDIT_CARD", "IBAN_CODE"]
    regex_results = regex_analyzer.analyze(
        text=text,
        entities=[e for e in regex_entities if e in entities],
        language="en",
        score_threshold=score_threshold,
    )
    for r in regex_results:
        results.append(DetectedEntity(
            entity_type=r.entity_type,
            start=r.start,
            end=r.end,
            score=r.score,
        ))

    return results
```

## PII Category Mapping

Map between DeBERTa training labels and your existing Presidio entity types:

| DeBERTa Label | → Presidio Entity | Notes |
|---|---|---|
| GIVENNAME, SURNAME, USERNAME | PERSON | Consolidated |
| EMAIL | EMAIL_ADDRESS | Direct |
| TELEPHONENUM | PHONE_NUMBER | Direct |
| CREDITCARDNUMBER | CREDIT_CARD | Covered by Presidio regex too |
| ACCOUNTNUM, TAXNUM, SOCIALNUM | IBAN_CODE / ACCOUNT | Expand as needed |
| STREET, CITY, ZIPCODE, BUILDINGNUM | LOCATION | Consolidated |
| PASSWORD | PASSWORD | New category |
| DATEOFBIRTH | DATE_OF_BIRTH | New category |
| DRIVERLICENSENUM, IDCARDNUM, PASSPORT | ID_DOCUMENT | New category |
| IPV4, IPV6 | IP_ADDRESS | New category |

## Costs

| Item | Cost |
|---|---|
| Training (Colab free tier) | $0 |
| Training (Azure ML T4, ~2 hrs) | ~$1.06 |
| HuggingFace private repo | $0 (free tier) |
| Django container change | $0 (fits in 2 GiB) |
| **Total** | **$0 - $1.06** |

## Timeline

| Step | Time |
|---|---|
| Setup + test run | 15 min |
| Full training | ~2 hours (GPU) |
| Export + quantize | ~10 min |
| Upload model | 5 min |
| Django integration | ~2 hours (code) |
| Testing | ~1 hour |
| **Total** | **~1 afternoon** |

## Files

| File | Purpose |
|---|---|
| `scripts/train_pii_model.py` | Training script |
| `scripts/export_pii_model.py` | ONNX export + INT8 quantization |
| `scripts/requirements-pii-training.txt` | Training-only dependencies |
| `apps/pii/engine.py` | Runtime engine (swap spaCy → ONNX) |
| `apps/pii/redactor.py` | Redaction logic (mostly unchanged) |
| `apps/pii/config.py` | Tier policies (add new entity types) |
| `docs/pii-redaction-security.md` | Architecture docs (update) |
