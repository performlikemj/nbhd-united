# Real OpenClaw 2026.9.4 CLI contract

These are synthetic declarations, signed by the unchanged
`apps/cron/share_cron_sync.py` selector/signer and passed through the unchanged
image's `nbhd-cron-sync.mjs::buildAddArgs`. Query results and the token lookup
are stubbed during input generation; no production database or tenant is used.
The capture script checks the image writer's SHA256 against the repository.

Each case records the source declaration, signed selection, exact writer argv,
CLI exit status/stdout/stderr, and raw `cron list --all --json` stdout plus its
parsed JSON. Generated IDs/timestamps and observation metadata are retained.
`disabled` includes a direct writer probe of the excluded row: the signed file
is empty, and the writer cannot preserve disabled state. No credential, signed
envelope, or real user content is committed.

Reproduce on a workstation with >=8 GB free. Authenticate and pull
`nbhdunited.azurecr.io/nbhd-openclaw:2026.9.4-8ceb89f`. Generate inputs with:

```sh
PYTHONPATH=. <project-python> scripts/capture_openclaw_94_contract_inputs.py /tmp/oc94-contract
cp scripts/capture_openclaw_94_contract.mjs /tmp/oc94-contract/capture.mjs
```

Start the image with a unique local container name, **`--network none`**, no
published ports or mounts, and **`--entrypoint /bin/sh`** running `sleep infinity`.
Pass only `NODE_OPTIONS=`, `OPENCLAW_STATE_DIR=/tmp/oc94-state`,
`OPENCLAW_CONFIG_PATH=/tmp/contract/openclaw.json` and
`NBHD_INTERNAL_API_KEY=local-contract-token` (a public synthetic test token).
Copy the generated directory to `/tmp/contract`, make it writable by `node`,
and execute `openclaw gateway run` in that container. The generated config
turns off plugins and scheduled firing; the ordinary fleet entrypoint is bypassed.
Once the gateway is ready, execute `node /tmp/contract/capture.mjs` and copy the
case JSON files and runtime metadata out. Record `docker image inspect` identity
alongside them. Stop/remove only that local container afterward. Keep the image
only if at least 8 GB remains free. Never point this capture at a tenant gateway.

## Observed results

- `cron-tz`, `cron-top-hour`, `every`, `delivery-default`, `delivery-empty`:
  accepted and preserved (generated timing/defaults are not authored pins).
- `at-offset`, `at-utc`: accepted, normalized to `2027-01-01T00:00:00.000Z`
  without `tz`; unchanged writer comparison is false before preparation.
- `at-offset-stable`, `at-utc-stable`: prepared via the migration helper,
  accepted; two real writer reconciliation passes each apply/remove zero jobs
  and retain the same ID.
- `every-anchor`, `delivery-explicit`: accepted, but authored anchor and
  destination/account/thread are lost, so migration must block.
- `system-main`, `system-isolated`: rejected; writer emits `--no-deliver` and
  does not emit a session override.
- `disabled`: omitted by the signer; a direct writer probe creates an enabled
  job, so migration must block rather than silently lose disabled state.

`runtime-owned.json` records CLI refusals to remove or claim the gateway's
`heartbeat:` and `skill-collection-review:` namespaces. These appear even with
scheduled firing disabled. Reproduce those probes with
`scripts/capture_openclaw_94_runtime_owned.mjs` **only in the isolated container**
after the matrix has been cleaned up. The raw lists retain these monitor rows;
contract tests verify that operator ownership filtering leaves them alone.

All ordinary rows additionally carry the generated trusted scheduled tool
policy. That exact default is reproducible, not a blanket exemption for other
policies. Unknown definition fields and policy variations are negative tests.

Offline operator tests pin the clock to each captured row's `createdAtMs`, so
the golden one-shots do not expire in CI. When re-recording after January 2027,
choose new future instants consistently in the input generator and assertions.

## Round six: default-deny shape evidence

[Complete 75-case accepted/blocked matrix](round_six/README.md) and
[image/source provenance](round_six/runtime-metadata.json) extend the earlier
captures. Run the input generator without `--cases` for the full matrix, or use
`--cases name,name` for a subset (declaration IDs retain their matrix positions).
The six typed models use their real pre-save derivation, including model,
restricted tools, light context, timeout, wake mode and enforcement description.
Every selected successful case now records two real writer passes.

Copy only case JSON and runtime metadata into `round_six/`, then regenerate the
reviewable allowlist with:

```sh
PYTHONPATH=. <project-python> scripts/build_openclaw_94_shape_evidence.py
```

The builder accepts only selected cases with equal semantic digests and two
zero-mutation passes retaining the original ID. The contract suite independently
validates every manifest reference, both normalization interpreters and actual
operator execution for every accepted capture. The database tests exercise the
complete noncanonical import → preparation → unchanged signed-selection path.
Do not hand-add shapes or infer combinations from separately passing controls.
Capture progress and exceptions contain metadata only; raw synthetic CLI bodies
are retained solely in the fixture artifacts.
