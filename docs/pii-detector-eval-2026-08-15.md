# PII detector evaluation — 2026-08-15

VERDICT: **STOP — one or more Phase A quality gates failed; no engine integration is authorized.**

## Orchestrator gate disposition — Phase B proceeds

Round 1's only failed gate was raw-model email recall (Liquid 55% vs 90% bar;
it missed emails embedded in no-space Japanese text). The orchestrator ruled
this MOOT for production: prod email coverage is the union of the neural model
and the Presidio `EmailRecognizer` regex — see `apps/pii/engine.py` ("Regex
fallback (catches emails the model misses)") — and that deterministic layer is
engine-independent. All other gates passed (FP reduction 0.43×, names 95% vs
60%, phones/addresses 100% vs 50%, JA 60% vs 24%, ai4privacy F1 71.7% vs 16.9%).
Do NOT re-run the gate decision; build Phase B.

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

Liquid's pinned hybrid decoder emits 40 domain-qualified types. The table below
is exhaustive. Name-like identifiers fail closed to `PERSON`; address, postal,
and coordinate types fail closed to `LOCATION`. `Dropped` means the detector
span is deliberately not promoted into the existing placeholder taxonomy.

| Liquid type | NBHD entity type | Decision |
|---|---|---|
| `identity.person_name` | `PERSON` | Personal name; fail closed. |
| `identity.ssn` | `ID_DOCUMENT` | Government identifier. |
| `identity.national_id` | `ID_DOCUMENT` | Government identifier. |
| `identity.passport` | `ID_DOCUMENT` | Government identifier. |
| `identity.drivers_license` | `ID_DOCUMENT` | Government identifier. |
| `identity.date_of_birth` | `DATE_OF_BIRTH` | Existing birth-date placeholder. |
| `identity.tax_id` | `ID_DOCUMENT` | Government/tax identifier. |
| `contact.email` | `EMAIL_ADDRESS` | Existing email placeholder. |
| `contact.phone` | `PHONE_NUMBER` | Existing phone placeholder. |
| `contact.address` | `LOCATION` | Address-like; fail closed. |
| `contact.postal_code` | `LOCATION` | Location-like; fail closed. |
| `contact.ip_address` | `IP_ADDRESS` | Existing network identifier placeholder. |
| `financial.credit_card` | `CREDIT_CARD` | Existing card placeholder. |
| `financial.iban` | `IBAN_CODE` | Existing IBAN placeholder. |
| `financial.bank_account` | `ACCOUNT` | Existing account placeholder. |
| `financial.swift_bic` | `ACCOUNT` | Bank-routing account identifier. |
| `financial.crypto_wallet` | `CRYPTO_ADDRESS` | Existing wallet placeholder. |
| `financial.amount` | Dropped | Amount is context, not identifying PII. |
| `credential.api_key` | `PASSWORD` | Secret credential; fail closed. |
| `credential.password` | `PASSWORD` | Existing secret placeholder. |
| `credential.private_key` | `PASSWORD` | Secret credential; fail closed. |
| `credential.jwt` | `PASSWORD` | Secret credential; fail closed. |
| `credential.connection_string` | `PASSWORD` | Secret credential; fail closed. |
| `developer.login_credentials` | `PASSWORD` | Secret credential; fail closed. |
| `online.username` | `PERSON` | Account-name-like identifier; fail closed. |
| `online.url` | Dropped | Broad URL context is not inherently identifying. |
| `device.mac_address` | `IP_ADDRESS` | Existing network identifier placeholder. |
| `device.imei` | `PHONE_NUMBER` | Matches the existing IMEI collapse. |
| `developer.device_id` | `ID_DOCUMENT` | Persistent device identifier. |
| `location.gps_coordinates` | `LOCATION` | Location-like; fail closed. |
| `healthcare.medical_record` | `ID_DOCUMENT` | Persistent medical-record identifier. |
| `healthcare.health_plan_id` | `ID_DOCUMENT` | Persistent health-plan identifier. |
| `healthcare.condition` | Dropped | Health context, not an identifier; high journal FP risk. |
| `healthcare.medication` | Dropped | Treatment context, not an identifier; high journal FP risk. |
| `org.company_name` | Dropped | Organization context; no honest existing entity type. |
| `special.religion` | Dropped | Sensitive context, not an identifier. |
| `special.political` | Dropped | Sensitive context, not an identifier. |
| `special.orientation` | Dropped | Sensitive context, not an identifier. |
| `special.health_status` | Dropped | Sensitive context, not an identifier. |
| `legal.case_number` | `ID_DOCUMENT` | Persistent legal identifier. |

## Flip plan

1. Deploy with `PII_DETECTOR_ENGINE` unset (flag off, default `deberta`).
2. Before any flip, confirm at least ~760 MiB additional RSS headroom on
   `nbhd-django-westus2` relative to the DeBERTa baseline.
3. Flip a canary tenant through an isolated canary revision/container using
   `PII_DETECTOR_ENGINE=liquid`; monitor detection quality, worker startup, RSS,
   and latency.
4. Flip the fleet only after the canary is accepted.

## Phase B integration gate outputs

Ruff format:

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/ruff format apps/pii/config.py apps/pii/engine.py apps/pii/liquid_engine.py apps/pii/test_authoring.py apps/pii/test_detector_engines.py apps/pii/test_historical_migration.py apps/pii/test_redacted_entity_honesty.py apps/pii/test_retired_binding_substitution.py apps/pii/tests.py
9 files left unchanged
```

Migration gate:

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py makemigrations --check --dry-run
No changes detected
```

Binding Japanese-email regression:

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py test apps.pii.test_detector_engines.EngineIndependentPresidioTests.test_liquid_no_space_japanese_email_uses_engine_independent_presidio --noinput --verbosity 2
test_liquid_no_space_japanese_email_uses_engine_independent_presidio (apps.pii.test_detector_engines.EngineIndependentPresidioTests.test_liquid_no_space_japanese_email_uses_engine_independent_presidio) ... ok

----------------------------------------------------------------------
Ran 1 test in 2.770s

OK
```

PII suite:

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py test apps.pii --noinput
...................................................
----------------------------------------------------------------------
Ran 455 tests in 17.395s

OK (skipped=33)
Destroying test database for alias 'default'...
```

Full suite (unrelated pre-existing failures):

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py test --noinput
======================================================================
FAIL: test_agents_md_has_security_section (tests.test_memory_layer.AgentsMemoryInstructionsTest.test_agents_md_has_security_section)
FAIL: test_agents_md_has_session_startup (tests.test_memory_layer.AgentsMemoryInstructionsTest.test_agents_md_has_session_startup)
FAIL: test_basic_tier_allows_files_group (tests.test_memory_layer.ToolPolicyMemoryTest.test_basic_tier_allows_files_group)
FAIL: test_basic_tier_allows_memory_group (tests.test_memory_layer.ToolPolicyMemoryTest.test_basic_tier_allows_memory_group)
FAIL: test_config_includes_memory_tools (tests.test_memory_layer.ToolPolicyMemoryTest.test_config_includes_memory_tools)
FAIL: test_plus_tier_allows_memory_group (tests.test_memory_layer.ToolPolicyMemoryTest.test_plus_tier_allows_memory_group)
----------------------------------------------------------------------
Ran 7885 tests in 435.984s

FAILED (failures=6, skipped=33)
Destroying test database for alias 'default'...
```

All six failures are confined to `tests.test_memory_layer`, outside this
change's allowed paths. The isolated module reproduces the same baseline drift:

```text
$ /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py test tests.test_memory_layer --noinput
.FF............FFFF
----------------------------------------------------------------------
Ran 19 tests in 0.044s

FAILED (failures=6)
Destroying test database for alias 'default'...
```

## Risks and open questions

- Liquid missed 9 of 20 synthetic emails at the raw-model seam. Production
  coverage depends on the shared Presidio email fallback proven by the binding
  no-space Japanese regression test.
- Synthetic recall measures controlled formats, not fleet prevalence; the pinned multilingual ai4privacy slice provides a broader parity check but contains no Japanese rows.
- Partial-overlap scoring is intentionally forgiving about boundaries; downstream redaction quality still depends on the helper's boundary expansion and NBHD's existing span merge logic.
- The flag is process-wide, not tenant-scoped. A true canary-tenant flip needs
  an isolated canary revision/container and routing rather than changing the
  shared production container's environment in place.

## 2026-08-27 addendum — shared DeBERTa detector process

PR1 adds a transport choice without changing the selected detector engine.
`PII_DETECTOR_TRANSPORT=local` remains the deploy default; `shared` moves model
inference into one supervised process per container while Presidio stays in
each application process. The checked redaction APIs now report
`reason=neural-unavailable` instead of confirming a receipt whenever the
neural call fails. Their Presidio-redacted text remains unchanged.

### Flags and defaults

| Environment variable | Default | Purpose |
|---|---|---|
| `PII_DETECTOR_ENGINE` | `deberta` | Model loaded locally or by the shared process (`deberta` or `liquid`). |
| `PII_DETECTOR_TRANSPORT` | `local` | `local` preserves per-process loading; `shared` uses the Unix socket. |
| `PII_SHARED_SOCKET` | `/run/nbhd/pii-detector.sock` | Unix socket created mode-private by the sidecar. |
| `PII_SHARED_DEADLINE_S` | `5.0` | One client deadline covering connect, queue, inference, and framing. |
| `PII_SHARED_QUEUE_MAX` | `64` | Bounded FIFO depth ahead of the single inference thread. |
| `PII_SHARED_WARM_WAIT_S` | `90` | Gunicorn worker readiness-ping window; failure still binds HTTP and fails open with unconfirmed receipts. |

The 5-second deadline and queue depth 64 are PR1 rollout defaults, not final
production choices. Fable sets them from the measurements before the transport
flip. The directive requires the deadline to be at least three times the
20,000-character burst p99; this run therefore requires at least 21.460 s.

### Protocol v1

The server runs as `python -m apps.pii.shared_server`. Each call opens one Unix
socket connection, writes one frame, reads one frame, and closes. There are no
request IDs, persistent connections, pipelining, or database access. A frame is
a four-byte unsigned big-endian body length followed by UTF-8 JSON. Request and
response bodies are capped at 1 MiB, and success responses at 4,096 spans.

- Detection request: `{"v":1,"engine":"deberta","text":"…","ttl_ms":N}`.
- Readiness request: `{"v":1,"ping":true}`.
- Readiness response: `{"v":1,"ready":true|false,"engine":"…","protocol":1}`.
- Success response: `{"v":1,"engine":"…","spans":[{"entity_group":"…","score":0.0,"start":0,"end":1}]}`.
- Error response: `{"v":1,"error":"queue_full|expired|engine_mismatch|bad_request|too_large|not_ready|inference_failed"}`.

The client validates and materializes the complete response before returning.
Three consecutive transport/protocol failures open its circuit for 30 seconds;
one half-open probe is allowed after that interval. Both client and server emit
only shape telemetry. `pii_detector_client` records engine, transport, outcome,
latency, character-length bucket, and span count. `pii_detector_server` records
engine, outcome, queue/inference/total times, character-length bucket, span
count, and queue depth. Buckets are `0-255`, `256-1023`, `1024-8191`,
`8192-19999`, and `20000+`; neither event contains input text or payloads.

### D7 parity and latency

The offline cached production DeBERTa model was run locally and through a real
shared-server subprocess. Raw protocol spans and final `_detect_pii` spans were
identical across 159 golden-plus-eval inputs. The 54-phrase golden check also
passed through the real shared server.

```text
D7 PARITY: raw_spans=IDENTICAL detect_pii=IDENTICAL texts=159
PII golden-set OK: 54 phrases (34 clean / 20 control) all pass
```

Latency inputs consist entirely of four-byte UTF-8 characters. Sequential
figures use 10 calls per size; burst figures use one simultaneous 24-thread
burst. Percentiles use nearest rank.

| Characters | Mode | n | p50 ms | p95 ms | p99 ms |
|---:|---|---:|---:|---:|---:|
| 200 | sequential | 10 | 165.208 | 171.804 | 171.804 |
| 200 | burst24 | 24 | 1974.161 | 3779.015 | 3941.579 |
| 8,000 | sequential | 10 | 264.423 | 276.198 | 276.198 |
| 8,000 | burst24 | 24 | 4698.991 | 7899.775 | 8161.821 |
| 10,000 | sequential | 10 | 266.794 | 270.170 | 270.170 |
| 10,000 | burst24 | 24 | 3270.744 | 7698.277 | 8155.833 |
| 20,000 | sequential | 10 | 343.126 | 492.845 | 492.845 |
| 20,000 | burst24 | 24 | 3907.159 | 6886.936 | 7153.179 |

### D7 soak

The initial 2,000-call attempt projected beyond the 20-minute task limit, so the
specified reduced soak used 500 alternating 200/20,000-character calls.

| Calls | RSS MiB | PSS MiB |
|---:|---:|---:|
| 100 | 627.969 | unavailable on macOS |
| 200 | 624.266 | unavailable on macOS |
| 300 | 634.469 | unavailable on macOS |
| 400 | 634.469 | unavailable on macOS |
| 500 | 634.469 | unavailable on macOS |

```text
D7 SOAK RESULT calls=500 rss_high_mib=634.469 pss_high_mib=NA last_quarter_growth_pct=0.000 plateau=YES
```

RSS plateaued (last-quarter growth 0.000%). PSS is unavailable on this macOS
executor because `/proc/<pid>/smaps_rollup` does not exist; the gated test reads
real RSS and PSS from that file automatically when run on Linux and never
infers PSS from RSS.

### Transport flip and rollback

The flip is a one-line PR changing the workflow's durable
`PII_DETECTOR_TRANSPORT` value from `local` to `shared`. Inform MJ first, deploy
the resulting single revision, then force one Telegram, one HTTP/iOS, and one
LINE redaction. For the first hour require non-`ok` client outcomes below 1%,
zero `queue_full` plus `expired`, zero sidecar/worker/container restarts, p99
below half the configured deadline, and at least 800 MiB cgroup headroom. Also
capture per-process RSS/PSS, run the deployed-image golden check, and send a
known neural-only PII leak probe through every channel; its receipt must be
confirmed and its placeholder present.

Rollback owner is Fable with a target under 15 minutes: revert the flip PR. The
emergency stop-bleeding lever, used only while that revert lands, is:

```sh
az containerapp update -n <ACA_APP> -g <RG> --set-env-vars PII_DETECTOR_TRANSPORT=local
```
