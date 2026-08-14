# Datebook Approval UX Backend + Plugin Handback

Date: 2026-08-14
Directive: `/Users/michaeljones/Projects/nbhd-ios/DIRECTIVE_datebook_approval_ux.md` v2.1, RATIFIED
Branch: `feat/datebook-approval-ux`
Starting head: `cf262471`

## Outcome

No stop-on-contradiction condition was found. The backend and pinned OpenClaw
plugin contracts required by directive P1–P6 are implemented and stable for the
later iOS round. No production host/database, real APNs/Telegram/LINE transport,
push, PR, deploy, or version bump was used.

## Scope delivered

1. **24-hour datebook review window**
   - `calendar_create` and `reminder_create` gates now expire after 24 hours;
     generic action gates retain the model default of five minutes.
   - Telegram and LINE confirmation copy says `Review within 24 hours` only for
     datebook types. The plugin fallback says approval can be reviewed within
     24 hours.
   - Datebook expiry now has the typed `stale_review` state and server-authored
     narration: `The 24-hour review window expired. Nothing was queued or created.`
   - Respond, runtime status, and the shared QStash expiry sweep all use the
     locked datebook expiry transition.

2. **Destination default storage and invalidation**
   - Added `DatebookDestinationDefault`, unique by `(tenant, entity_type)`, with
     `name`, `fingerprint`, `target_installation_id`, `gateway_epoch`, and
     `pii_receipts` plus timestamps.
   - Registered the model in `apps/pii/store_registry.py`; owner writes go
     through `author_store_fields` at `datebook.owner.destination_default`.
   - Gateway takeover/reinstall epoch-advance and Datebook disable paths delete
     stored defaults. Resolution also deletes an installation/epoch/fingerprint
     mismatch and proceeds to `device_default`; it never falls back by name.

3. **Server-side destination resolution**
   - Gate creation applies the required order: explicit tool destination,
     current tenant default, then the `device_default` sentinel.
   - The resolved `destination_kind`, `destination_name`, and
     `destination_fingerprint` are stamped into the pending action payload,
     alongside an immutable `requested_destination` projection.
   - Conflicting outer and per-item calendar/list destinations fail gate
     creation with typed `conflicting_destination` and create no action.

4. **Locked respond-with-override flow**
   - The shared `GateRespondView.resolve_action` path accepts optional
     `destination_override: {name, fingerprint}` and `set_default: bool`.
   - Inside the existing row lock it verifies pending/unexpired state, validates
     the override/default intent, preserves the requested target, authors a new
     owner-class approved-target projection with fresh receipts, creates the
     `DeviceCommand` from that projection, persists a default only after command
     creation succeeds, writes requested/approved/default old+new fingerprints
     to audits, and registers notifications with `transaction.on_commit`.
   - A command-creation `ProtocolError` commits `approved` + `failed` audit
     transitions but does not write a default. Concurrent approvals use separate
     database connections and a barrier; the regression proves one command seam
     call, one default mutation seam call, and a typed 409 loser
     (`action_already_resolved`).

5. **`datebook_gate_changed` invalidation**
   - Every datebook gate creation and every approve/cancel/expire transition,
     including QStash expiry, schedules a generic PII-free invalidation after
     commit.
   - APNs targets only the active gateway installation with device fallback
     disabled. The payload extra is exactly
     `{ "type": "datebook_gate_changed" }`; no reviewed content or approval
     action is included.
   - Telegram/LINE inline buttons and callback payloads are unchanged.

6. **Truthful delivery and narration facts**
   - Datebook create/state responses expose `approval_surface`,
     `delivery_state`, and server-authored `guidance`.
   - Built-in Telegram/LINE senders now return transport acceptance separately
     from the platform message ID. Only a real response message ID produces
     `delivery_state: "sent"`; an accepted response without an ID is
     `"accepted"`, and app review availability is `"available"`.
   - App guidance contains the required phrase
     `the approval is in this conversation`; non-confirmed external delivery
     never claims that a message was sent.

7. **Review-always safety**
   - Datebook creates no longer consult `GatePreference` for auto-approval.
     Model-supplied `direct_user_originated: true` is retained in the action
     payload for telemetry but has no approval authority.

8. **Pending endpoint contract**
   - Datebook gate creation stores `originating_channel` independently from the
     mutable delivery `platform_channel`.
   - `PendingGateActionsView` exposes that provenance and ISO-8601 `created_at`,
     plus the 86,400-second review-window fact.

9. **Pinned OpenClaw plugin**
   - The two create tools relay server-authored guidance, use 24-hour fallback
     copy, handle `stale_review` honestly, and have capability-first
     descriptions. Their JSON schemas are unchanged.
   - The read tool retains its calendar-prominent, capability-first description
     and explicitly preserves the stale-mirror warning required by the existing
     schema contract.
   - The exact `openclaw@2026.5.28` end-to-end smoke now asserts the in-chat
     guidance and the `approval_surface`/`delivery_state` response fields.

## Migrations

- `apps/actions/migrations/0004_datebook_approval_ux.py`
  - Adds immutable-origin/delivery facts to `PendingAction` and destination
    fingerprint facts to `ActionAuditLog`.
- `apps/datebook/migrations/0004_datebook_approval_ux.py`
  - Creates `datebook_destination_defaults` with its unique and positive-epoch
    constraints.
- `apps/tenants/migrations/0156_relock_after_datebook_approval_ux.py`
  - Fresh graph-tail public-schema RLS relock depending on both new migrations,
    following `0153_relock_after_datebook_b1.py`.

## PII registration evidence

- Store registry label: `datebook.DatebookDestinationDefault`.
- Registered PII field: `name`; receipts field: `pii_receipts`.
- Runtime gate requests continue through the registered `actions.PendingAction`
  runtime-authoring seam.
- Approved-target projections and learned defaults use owner-class authoring,
  then owner representations rehydrate only at owner-facing reads.
- `test_override_set_default_authors_owner_pii_and_complete_audit_fingerprints`
  proves a known long-tail person name is stored as `[PERSON_1] Personal`, with
  owner receipts, while the approved command/default/audit contract remains
  complete.
- Migration and public-schema lockdown coverage run in the full Docker gate.

## Deployment convergence

- **Django deploy:** all migrations, 24-hour/stale-review behavior, destination
  resolution/defaults, respond override/default persistence, audit fields,
  review-always enforcement, pending provenance, truthful sender facts/guidance,
  and `datebook_gate_changed` APNs invalidation.
- **Next OpenClaw image:** plugin guidance relay, 24-hour/stale narration, and
  capability-first create-tool descriptions.
- **Later iOS round:** consumes these stable contracts to resolve/freeze the
  `device_default` sentinel, submit fingerprinted overrides, render the card and
  picker, and implement refresh/Live Activity behavior. No iOS file changed here.

## Verification

Before implementation:

- Focused Django `apps.datebook apps.actions`: 143 tests, PASS.
- Datebook plugin Node suite: 3 tests, PASS.

After implementation:

- Focused Django `apps.datebook apps.actions`: 160 tests, PASS (+17).
- Datebook plugin Node suite: 4 tests, PASS (+1).
- Exact pinned OpenClaw 2026.5.28 end-to-end smoke under Node 22.22.2: 1 test,
  PASS.
- `makemigrations --check --dry-run`: no changes detected.
- Ruff and `git diff --check`: PASS.
- Full required `make docker-gate`: PASS. Backend ran 7,839 tests in 559.638s
  (`OK`, 2 skipped), then passed config validation and security audit; frontend
  lint/build passed with four pre-existing warnings.

Full-gate tail:

```text
Ran 7839 tests in 559.638s
OK (skipped=2)
Config validator: PASS
Security audit: PASS
=== BACKEND LEG: PASS ===
=== FRONTEND LEG: PASS ===
=== DOCKER CI-PARITY GATE: PASS ===
```

## Deviations / caveats

- No requested backend/plugin behavior was omitted and no implementation
  deviation was taken.
- The first pinned-runtime smoke attempt used the shell-default Node 20.19.3;
  OpenClaw correctly rejected it because 22.19+ is required. The unchanged
  smoke passed when rerun with the already-installed Node 22.22.2.
- The first Docker-gate attempt stopped at Ruff's format check for one edited
- Subsequent full-suite runs exposed three legacy-compatibility assertions
  (lightweight messaging fixtures without `action_type` and the preserved
  stale-mirror sentence) and then the exact store-registry inventory assertion.
  Minimal compatibility corrections were made and their focused suites passed
  before the final green gate.
- Mocks/local services only; no production or real notification transport was
  exercised.
