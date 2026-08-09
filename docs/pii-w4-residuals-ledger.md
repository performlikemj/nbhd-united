# PII W4 residuals ledger

This is the interpretation ledger for counts emitted by the historical
placeholder migration. It contains no row text, detected value, or entity-map
content.

| Report state | Meaning | Required action |
|---|---|---|
| `placeholder` | Field already has a clean receipt and is fenced out. | None; expected on re-drive. |
| `absent` | Pre-P3 field receipt is absent or malformed. | Migrates. |
| `bypass` | No checked provenance ran at the original write. | Migrates. |
| `unconfirmed` | Earlier checked redaction failed. | Repair must drain before store admission; if encountered after the admission check, re-drive. |
| `residual` | Earlier policy knowingly left detected text. | Repair must drain before store admission; if encountered after the admission check, re-drive. |
| `terminal` | Repair attempts were exhausted. Terminal is excluded from repair eligibility. | Migrates; it is not a fence. |
| `repair_pending_skipped` | Store still has `unconfirmed`/`residual` repair-eligible rows. | Let repair run, investigate persistent failures, then re-drive the tenant. |
| `changed_skipped` | The row version changed after batch scan or lost the conditional update race. | Re-drive off-peak; cursor intentionally does not advance beyond it. |
| `authoring_unconfirmed` | Re-read full NER could not prove placeholder-space after the batch mint. | No row write is accepted; investigate detector health and retry. |
| `skipped_by_design` | Surface is intentionally outside the registry/migration. `rows=0` means “not enumerated,” not database cardinality. | Track here; do not route through the per-tenant migration. |

## Skipped-by-design surfaces

- `lessons.TutoringSession.messages`: tenancy binding is unresolved. Its
  LessonConnection topology is not a safe direct tenant FK.
- Cross-tenant friends stores (`SharedGoal`, `SharedUpdate`, `FriendMessage`):
  one tenant's placeholder namespace is invalid for another tenant.
- `journal.UserMemory`: dead model with no production writer; deletion remains
  the intended disposition.
- Anything absent from `apps/pii/store_registry.py`: the registry is the sole
  store authority. Discovery does not grant migration scope.

## Known residual risk and controls

- Registered models with `updated_at` use that timestamp as the optimistic
  scan/rewrite fence. Append-only models without it use PostgreSQL `xmin`, which
  is stronger than a content-only comparison because any row update changes the
  token. Neither path holds a content-row lock during rewriting.
- Batch pre-scan necessarily holds detected candidate values in process memory
  long enough to mint the tenant map. Values are never logged. The tenant row is
  locked only for one map/counter write; row rewrites happen after that
  transaction exits.
- A row changed after pre-scan can leave a newly minted but temporarily unused
  binding. This is preferable to applying stale spans; junk review/retirement
  remains the cleanup path, and the row is retried from the unchanged cursor.
- W4 does not alter encrypted sidecars. The directive's order—placeholder
  migration before encryption backfill—is mandatory. If a historical sidecar
  backfill already ran, stop and design a decrypt/redact/re-encrypt migration;
  do not run W4 over only the plaintext half.
- Placeholder migration reduces identity exposure but does not encrypt the
  remaining prose, topics, health facts, embeddings, or the identity map.
- Commit is irreversible in place. Recovery is restore-from-backup, never an
  attempted placeholder-to-name “un-migration.”
