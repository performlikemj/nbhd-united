# PII detector evaluation — 2026-08-15

VERDICT: **STOP — one or more Phase A quality gates failed; no engine integration is authorized.**

This is an engine-level comparison of raw model/helper spans. NBHD stoplists, tier policy, and Presidio recognizers were not applied. Seeds and PyTorch deterministic algorithms were fixed at `20260815`; inference used CPU with one PyTorch thread.

## False-positive suite

The 84-sentence suite covers every literal in `_FLEET_WORD_STOPLIST` and `_FLEET_PHRASE_STOPLIST`, the 2026-08-08 fleet evidence terms, and the task-brief cases. A sentence counts only when a raw person/location-class span overlaps the named junk target.

| Model | Junk sentences flagged | Rate | Stash sentences flagged | Liquid / DeBERTa |
|---|---:|---:|---:|---:|
| Production DeBERTa | 7 / 84 | 8.3% | 0 / 4 | — |
| Liquid LFM2.5 | 3 / 84 | 3.6% | 0 / 4 | 0.43 |

## Synthetic recall suite

The suite contains 100 labeled spans. Matching is type-aware and one-to-one; any positive character overlap counts as a hit.

| Type | DeBERTa hits | DeBERTa recall | Liquid hits | Liquid recall |
|---|---:|---:|---:|---:|
| NAME | 12 / 20 | 60.0% | 19 / 20 | 95.0% |
| EMAIL | 20 / 20 | 100.0% | 11 / 20 | 55.0% |
| PHONE | 10 / 20 | 50.0% | 20 / 20 | 100.0% |
| ADDRESS | 10 / 20 | 50.0% | 20 / 20 | 100.0% |
| DOB | 10 / 20 | 50.0% | 10 / 20 | 50.0% |
| **Overall** | **62 / 100** | **62.0%** | **80 / 100** | **80.0%** |

| Language | DeBERTa hits | DeBERTa recall | Liquid hits | Liquid recall |
|---|---:|---:|---:|---:|
| EN | 50 / 50 | 100.0% | 50 / 50 | 100.0% |
| JA | 12 / 50 | 24.0% | 30 / 50 | 60.0% |

## ai4privacy parity slice

Fixed 500-row sample of the pinned `validation` split, selected with `shuffle(seed=20260815).select(range(500))`. Partial F1 uses one-to-one overlap matching at the detection tier (type ignored), because the two checkpoints use different label taxonomies.

| Model | TP | FP | FN | Precision | Recall | Partial F1 |
|---|---:|---:|---:|---:|---:|---:|
| Production DeBERTa | 522 | 5116 | 23 | 9.3% | 95.8% | 16.9% |
| Liquid LFM2.5 | 459 | 277 | 86 | 62.4% | 84.2% | 71.7% |

## Resident memory and CPU latency

RSS is the fresh worker process delta from immediately before tokenizer/model load through one untimed warm-up, so lazily mapped weight pages are resident. Latency excludes that warm-up and is the median of individual sentence calls (tokenization + inference + decoding).

| Model | Load RSS delta | FP median | Recall median | ai4privacy median | All suites median |
|---|---:|---:|---:|---:|---:|
| Production DeBERTa | 445.4 MiB | 15.4 ms | 16.6 ms | 22.1 ms | 21.2 ms |
| Liquid LFM2.5 | 1206.6 MiB | 49.0 ms | 49.3 ms | 50.1 ms | 49.5 ms |

## Gate decision

| Requirement | Result |
|---|---:|
| Liquid FP count <= 50% of DeBERTa | PASS |
| Liquid stash flags = 0 | PASS |
| Liquid overall recall >= DeBERTa - 2 points | PASS |
| Liquid name recall >= 90% | PASS |
| Liquid email recall >= 90% | FAIL |
| Liquid phone recall >= 90% | PASS |
| Liquid JA recall >= DeBERTa JA recall | PASS |
| Liquid ai4privacy partial F1 no more than 5 points below DeBERTa | PASS |

VERDICT: **STOP — one or more Phase A quality gates failed; no engine integration is authorized.**

## Pinned artifacts

- Production model: `lakshyakh93/deberta_finetuned_pii` @ `a038061af92047b0afbbd5ca07d7aa0521789379`
- Candidate model and shipped decode helpers: `LiquidAI/LFM2.5-Encoder-350M-PII-Detector` @ `b8c9cf3d2d6ae52501b35a27ba46f271449c9ce2`
- ai4privacy dataset: `ai4privacy/pii-masking-400k` @ `414d0a3b5798a152588a0828f1c08a5787de10f4`

## Method notes

- DeBERTa construction exactly mirrors `apps/pii/engine.py`: explicit tokenizer and token-classification model, then `pipeline(..., aggregation_strategy="simple", device="cpu")`.
- Liquid construction uses `trust_remote_code=True` for tokenizer/model and the repository's pinned `pii_hybrid_decode.py` with `hybrid=True`; `context_cued.py` is loaded from the same snapshot.
- Both models run vanilla PyTorch CPU in evaluation mode. No ONNX, optimum, Presidio, or redactor post-processing participates.
- FP targets are product-policy negatives, including opted-out geography and Osaka travel, even when the surface form is linguistically a real location.

## Commit gate outputs

Full evaluation tail:

```text
$ /private/tmp/claude-502/-Users-michaeljones-Projects-nbhd-ios/bd09c90a-6d73-426b-a0f5-79d5465174a9/scratchpad/pii-eval-venv/bin/python scripts/eval_pii_detectors.py
Wrote /Users/michaeljones/Projects/nbhd-united/.claude/worktrees/pii-detector-liquid/docs/pii-detector-eval-2026-08-15.md
VERDICT: STOP
  fp_reduction: PASS
  stash_zero: PASS
  overall_recall: PASS
  name_recall: PASS
  email_recall: FAIL
  phone_recall: PASS
  ja_recall: PASS
  ai4privacy_parity: PASS
```

Ruff format tail:

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/ruff format scripts/eval_pii_detectors.py
1 file left unchanged
```

Migration gate tail:

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py makemigrations --check --dry-run
/Users/michaeljones/Projects/nbhd-united/.claude/worktrees/pii-detector-liquid/apps/integrations/content_sanitize.py:12: SyntaxWarning: invalid escape sequence '\['
  Nothing in this app writes legitimate markdown images (`git grep '!\['`
No changes detected
```

PII test gate tail:

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py test apps.pii --noinput
...................................................
----------------------------------------------------------------------
Ran 444 tests in 22.674s

OK
Destroying test database for alias 'default'...
w4_migration_start tenant=4b3e52be-dde0-4acc-910c-2294bb944573 mode=dry-run batch_size=25
w4_migration_complete tenant=4b3e52be-dde0-4acc-910c-2294bb944573 mode=dry-run stores_complete=1 stores_skipped=0 batches=1
w4_receipt_demotion_store tenant=bf6a4359-4aac-4527-bbe3-18ea7c69baa6 store=journal.Task mode=dry-run matched=0 runtime_pre_cutoff=0 no_leaf_shape=0 demoted=0 changed_skipped=0 time_discriminator_missing_skipped=0
```

## Label-mapping decisions

No production Liquid label mapping was created and no types were deliberately dropped: the Phase A gate stopped the work before Phase B. Evaluation-only broad types are listed in the harness and do not affect runtime behavior.

## Risks and open questions

- Liquid missed 9 of 20 synthetic emails. Review the missed no-space Japanese contexts in the harness before reconsidering this checkpoint/helper.
- Synthetic recall measures controlled formats, not fleet prevalence; the pinned multilingual ai4privacy slice provides a broader parity check but contains no Japanese rows.
- Partial-overlap scoring is intentionally forgiving about boundaries; downstream redaction quality still depends on the helper's boundary expansion and NBHD's existing span merge logic.
