# Canary AGENTS.md blocks (`prompt_extras` composition)

Each `*.agents-extras.md` file in this directory is one behavioral block
meant for the `agents_md` section of a tenant's `prompt_extras`
(`apps.orchestrator.personas._get_tenant_prompt_extras` /
`render_workspace_files`, populated by the `set_prompt_extras` management
command). A block should be self-contained — its own `## Heading (canary)`,
no dependency on any other file's content — because more than one can be
active on the same tenant at once.

## Why concatenation, not one `--file` call per block

`set_prompt_extras --section agents_md` **replaces** the whole stored string
on every call — there is no append mode (`--file` / `--stdin` / `--clear`
are mutually exclusive, and the command always overwrites
`preferences['prompt_extras']['agents_md']` wholesale). If two canary
features each run their own `--file` call against the same tenant, the
**last call wins** and silently clobbers every block that ran before it.

When more than one canary block targets the same tenant (today: MJ
`148ccf1c-ef13-47f8-ada1-a98fa90e14a0` and Kiho
`13fa39df-74b6-4b17-b41e-ea0fc400fb13`), concatenate ALL active block files
into **one** `--stdin` call instead:

```bash
cat docs/canary/*.agents-extras.md | python manage.py set_prompt_extras \
    --tenant-id <id> --section agents_md --stdin
python manage.py force_apply_configs --tenant-id <id>
```

Re-run this for every canary tenant whenever any block is added, removed, or
edited — not just for the block that changed.

## Adding a new canary block

1. Add `docs/canary/<feature>.agents-extras.md` — one block, its own
   heading, safe to concatenate after or before any other block in this
   directory.
2. Re-run the concatenated `set_prompt_extras --stdin` command above for
   every tenant that should carry it, then `force_apply_configs
   --tenant-id <id>` to push the updated workspace files to the share.
3. Add a test asserting the block's load-bearing phrases survive
   concatenation with the other active blocks (see
   `apps/orchestrator/test_document_ingestion_directive.py`'s
   `test_concatenated_with_a_second_canary_block_still_renders_all_phrases`
   for the pattern).
