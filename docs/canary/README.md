# Canary AGENTS.md blocks (`prompt_extras` composition)

Each `*.agents-extras.md` file in this directory is one behavioral block
meant for the `agents_md` section of a tenant's `prompt_extras`
(`apps.orchestrator.personas._get_tenant_prompt_extras` /
`render_workspace_files`, populated by the `set_prompt_extras` management
command). A block should be self-contained — its own `## Heading (canary)`,
no dependency on any other file's content.

## Preferred pattern: give your feature its own section key

`set_prompt_extras --section agents_md` **replaces** the whole stored string
on every call — there is no append mode. If two features both target the
`agents_md` section, the **last call wins** and silently clobbers whichever
block ran first.

The right way to avoid this collision entirely is the one the quick-replies
canary rollout (`apps/orchestrator/personas.py`, `feat/chat-quick-replies-backend`)
uses: give your feature its **own** `prompt_extras` key (e.g.
`quick_replies_md`) and append it to `NBHD_AGENTS_MD` independently in
`render_workspace_files`, alongside the `agents_md` append — not inside it.
Because `set_prompt_extras`'s `_KNOWN_SECTIONS` map is a dict keyed by
section, `--section agents_md` and `--section quick_replies_md` write to two
different keys under `preferences['prompt_extras']` and never touch each
other; each command call reads-modifies-writes the whole map, so one
section's update always preserves every other section untouched. Two
independent `--file` calls, one per section, is all that's needed — no
concatenation required. This is the document-keeping gate's own plan for its
own Phase 2 flag-gated block (`document_ingestion_enabled`), which will also
get a dedicated section rather than sharing `agents_md`.

## Fallback: when two blocks genuinely must share the `agents_md` section

If a block has no dedicated section of its own (e.g. this Phase 1 canary
gate, which intentionally rides the pre-existing `agents_md` section rather
than adding a new one, since Phase 1 is scoped to touch nothing in
`personas.py`) and a second such section-less block ever needs to target the
same tenant, do NOT run one `--file` call per block — concatenate ALL active
`*.agents-extras.md` files into **one** `--stdin` call instead:

```bash
cat docs/canary/*.agents-extras.md | python manage.py set_prompt_extras \
    --tenant-id <id> --section agents_md --stdin
python manage.py force_apply_configs --tenant-id <id>
```

Re-run this for every canary tenant whenever any such block is added,
removed, or edited — not just for the block that changed.

## Adding a new canary block

1. Prefer giving your feature its own `prompt_extras` section key (see
   "Preferred pattern" above) — it composes automatically and needs no
   coordination with this directory's other files.
2. Only if your block must share the `agents_md` section: add
   `docs/canary/<feature>.agents-extras.md` (one block, its own heading,
   safe to concatenate before or after any other block here) and use the
   concatenated `--stdin` recipe above instead of a plain `--file` call.
3. Add a test asserting the block's load-bearing phrases survive
   concatenation with the other active `agents_md`-sharing blocks (see
   `apps/orchestrator/test_document_ingestion_directive.py`'s
   `test_concatenated_with_a_second_canary_block_still_renders_all_phrases`
   for the pattern) — good preventive hygiene even while there's only one
   `agents_md`-sharing block today.
