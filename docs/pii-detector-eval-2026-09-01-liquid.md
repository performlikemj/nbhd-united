# PII detector evaluation — 2026-09-01 Liquid shared-sidecar acceptance

VERDICT: **PR2a artifact and correctness gates pass for Liquid. The production
engine remains DeBERTa.** The later engine flip still requires a production
revision measurement against the D10 memory and latency stop conditions.

## Scope and pinned artifacts

- Liquid: `LiquidAI/LFM2.5-Encoder-350M-PII-Detector` at
  `b8c9cf3d2d6ae52501b35a27ba46f271449c9ce2`, CPU FP32, hybrid decoder enabled.
- DeBERTa: `lakshyakh93/deberta_finetuned_pii` at
  `a038061af92047b0afbbd5ca07d7aa0521789379`.
- Transport under test: one real Unix-socket shared server subprocess plus the
  production shared client contract.
- Host: macOS arm64, 14 logical CPUs, Python 3.12.13, PyTorch 2.13.0. Linux
  determinism used the built `linux/amd64` Django image with PyTorch 2.13.0+cpu.
- All evaluated text is synthetic and tracked in the repository.

No quantization was applied. The deploy-job pin remains
`PII_DETECTOR_ENGINE=deberta` in this change.

## L0 structured-overlap precedence

A validated Presidio `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD`, or
`IBAN_CODE` span now wins over any overlapping neural span before neural and
Presidio scores are compared. The Liquid adapter's score-less `1.0` therefore
cannot displace a structured result. Results from the same source class retain
the existing ordering: higher score, then shorter span on a tie.

The exact 100-span synthetic recall suite from the 2026-08-15 evaluation was
re-run through the real Liquid pipeline, `_detect_pii`, and `_filter_results`.
Precision counts every unmatched final span as a false positive; recall uses a
type-aware overlap with the one labeled span in each case.

| Slice | 2026-08-15 raw Liquid | 2026-09-01 Liquid + Presidio + L0 | 2026-09-01 DeBERTa + Presidio + L0 |
|---|---:|---:|---:|
| EN precision | not measured | 50 TP / 2 FP = **96.2%** | 50 TP / 13 FP = **79.4%** |
| EN recall | 50 / 50 = 100.0% | 50 / 50 = **100.0%** | 50 / 50 = **100.0%** |
| JA precision | not measured | 39 TP / 1 FP = **97.5%** | 21 TP / 0 FP = **100.0%** |
| JA recall | 30 / 50 = 60.0% | 39 / 50 = **78.0%** | 21 / 50 = **42.0%** |
| Overall precision | not measured | 89 TP / 3 FP = **96.7%** | 71 TP / 13 FP = **84.5%** |
| Overall recall | 80 / 100 = 80.0% | 89 / 100 = **89.0%** | 71 / 100 = **71.0%** |
| Email recall | 11 / 20 = 55.0% | 20 / 20 = **100.0%** | 20 / 20 = **100.0%** |
| Phone recall | 20 / 20 = 100.0% | 20 / 20 = **100.0%** | 20 / 20 = **100.0%** |

All 20 matched email spans and all 20 matched phone spans had
`source=presidio` after overlap deduplication. The remaining 11 misses are the
known raw-model gaps: one Japanese name and ten Japanese dates of birth. The
nine-point JA improvement is the Presidio email backstop recovering Liquid's
no-space Japanese email misses.

At the flip, users gain substantially higher Japanese and overall recall with
fewer false positives on this suite, but lose the three DeBERTa detections
listed below until Liquid model quality closes those gaps.

## D7 parity and golden behavior

Local and shared Liquid produced identical raw spans and identical
`_detect_pii` results for all 159 golden-plus-eval texts. The same parity check
passed for DeBERTa. Client/server engine mismatch validation remained enabled.

DeBERTa remained 54/54 on the golden set. Liquid was 51/54; its three misses
are **recall regressions versus today's DeBERTa** and each leaves PII in
cleartext for the downstream model. They are model-quality regressions, not
transport regressions:

- the city `Copenhagen` in “Flight to Copenhagen Airport” (`LOCATION`)
- the first name `Alice` in “Run with Alice” (`PERSON`)
- the PIN `4821` in “My locker PIN is 4821” (`PASSWORD`)

## D7 latency

The required worst-case inputs contain one four-byte UTF-8 scalar per character.
Sequential values use 50 calls. Burst values use three simultaneous 24-thread
rounds (72 observations). Values are end-to-end shared-client milliseconds on
the macOS host, so they are acceptance evidence rather than an Azure SLO.

### Liquid, four-byte input

| Characters | Sequential p50 / p95 / p99 | Burst-24 p50 / p95 / p99 |
|---:|---:|---:|
| 200 | 247.532 / 254.892 / 262.609 | 2,983.540 / 5,697.559 / 5,942.351 |
| 8,000 | 1,020.759 / 1,069.361 / 1,268.463 | 12,294.006 / 23,509.150 / 24,538.542 |
| 10,000 | 1,024.919 / 1,051.899 / 1,125.346 | 12,194.583 / 23,116.099 / 24,105.998 |
| 20,000 | 1,021.178 / 1,042.194 / 1,044.722 | 12,578.018 / 24,272.216 / 25,324.510 |

### DeBERTa regression, four-byte input

| Characters | Sequential p50 / p95 / p99 | Burst-24 p50 / p95 / p99 |
|---:|---:|---:|
| 200 | 156.990 / 162.758 / 164.491 | 1,891.294 / 3,632.735 / 3,792.254 |
| 8,000 | 260.534 / 266.045 / 271.478 | 3,108.559 / 5,951.850 / 6,207.595 |
| 10,000 | 260.356 / 264.756 / 266.638 | 3,131.786 / 5,992.303 / 6,255.846 |
| 20,000 | 264.623 / 270.051 / 270.495 | 3,192.098 / 6,106.950 / 6,368.804 |

The harness also measured realistic ASCII-heavy inputs. Liquid sequential p50
was 50.315 ms at 200 characters and 1,048.662 ms at 20,000; the corresponding
burst-24 p99 values were 1,227.513 ms and 25,589.340 ms.

## D7 soak and memory

The shared server handled 2,000 sequential calls alternating 200 and 20,000
realistic characters, sampling server RSS every 100 calls.

| Engine | RSS high-water | PSS | Last-quarter growth | Acceptance |
|---|---:|---:|---:|---:|
| Liquid | 1,543.062 MiB | unavailable on macOS | -4.662% | PASS (< 5%) |
| DeBERTa | 941.984 MiB | unavailable on macOS | 0.000% | PASS (< 5%) |

For a deliberately conservative pre-production projection, combining the
Liquid host RSS high-water with the measured production worker/poller values
(280 + 256 + 243 MiB) totals about **2,322 MiB**, leaving about **1,774 MiB**
under a 4 GiB cgroup. That clears the 800 MiB rule by about 974 MiB. This mixes
macOS RSS with Linux production PSS and is not a substitute for the flip PR's
Azure measurement; the production revision remains the authoritative D10 gate.

## Cross-platform determinism

`python -m apps.pii.span_manifest --corpus golden+eval` emitted 159 sorted raw
and `_detect_pii` records for each engine on the host and in the built
`linux/amd64` image. The extracted Liquid documents had zero diff lines and the
same SHA-256:

`c5402659696196a893f8a28f11c2f21f939ef38e920a9b557844e44062283303`

DeBERTa span structure was also identical after removing scores, but raw
floating-point scores differed across arm64 macOS and x86-64 Linux in 111
cases. No score was rounded or normalized to conceal that distinction.

## Dual-model artifact

- Immutable tag:
  `deberta-liquid-a038061af92047b0-b8c9cf3d2d6ae525`
- `linux/amd64` first build: 215.43 seconds.
- Uncompressed model image size: 1,981,824,664 bytes.
- Gzip-compressed Docker archive: 1,840,566,238 bytes (about 1.71 GiB).
- Actual ACR push and Container Apps revision pull time were not measured: this
  PR is fenced from ACR and production. Compared with the prior approximately
  554 MiB DeBERTa-only layer, a cold node/revision must transfer roughly an
  additional 1.2 GiB; subsequent builds retain the existing build-only-if-tag-
  absent behavior and immutable layer reuse.

## Acceptance summary

- PASS: pinned revision and offline loading.
- PASS: local/shared raw and `_detect_pii` parity for both engines.
- PASS: validated structured precedence restores email and phone recall to 100%.
- PASS: Liquid soak growth is below 5%.
- PASS: Liquid host versus `linux/amd64` manifest is byte-identical.
- RECALL REGRESSION: Liquid golden result is 51/54 versus DeBERTa 54/54;
  the three misses are cleartext leaks to the model.
- DEFERRED: Azure sidecar PSS, 4 GiB cgroup headroom, and revision pull time are
  flip-PR rollout measurements; they cannot be established from this PR's
  macOS host without touching production.
