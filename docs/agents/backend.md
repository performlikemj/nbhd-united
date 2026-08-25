# Backend (Django) gotchas

Read before writing Django code. `docs/agents/invariants.md` holds the platform-level rules; these are the code-level traps.

## The lint hook will fight you (predictably)

`.claude/hooks/lint_on_edit.sh` runs `ruff check --fix` + `ruff format` after every Edit/Write:

- **Unused imports are stripped between edits.** "Edit 1: add import → Edit 2: add usage" loses the import. Add the import and its first call site in the SAME Edit, or park symbols in a sentinel tuple until wired.
- **Function-local re-imports are load-bearing, not redundant.** `apps/orchestrator/tasks.py` functions locally re-import `invoke_gateway_tool` so `patch("apps.cron.gateway_client.invoke_gateway_tool")` intercepts at call time — a module-level alias binds before the patch and the mock silently no-ops. If a module-level import coexists, ruff strips the local as F811; mark `# noqa: F811` or keep the module-level import out. Check the consumer's import style before "cleaning up" any local import that tests patch.
- String literals inside `Literal[...]` can get mangled by autofix — prefer constants.

## Query patterns

- **Postgres puts NULL FIRST on DESC.** `.order_by("-fk_id").first()` to "prefer the non-null row" silently inverts. Use two queries, or `F("fk_id").desc(nulls_last=True)`.
- **`.count()` on a prefetched related manager ignores the prefetch cache** and fires a fresh COUNT per row. Annotate on the queryset (`Count(..., distinct=True)` — `distinct` is required when annotating two joins).
- Treat any N+1 as high severity — they surface as user-visible UI hangs, and serializer `source="fk.id"` traversals are the classic source (read the raw FK column instead: field named `<fk>_id`, no `source=` kwarg).

## Connections, transactions, RLS

- Lease pattern for anything long-running; no external calls inside `atomic()` (invariants §8).
- RLS context is **connection-scoped**: after any reconnect, re-run `set_rls_context(...)` before writing. The pooled role is `app_user` (non-BYPASSRLS); role-level settings live in Supabase, not the repo — `SELECT rolname, rolconfig FROM pg_roles` when chasing config drift.
- Tenant tz: `from apps.common.tenant_tz import tenant_tz_name, tenant_tz, safe_zoneinfo` — never a private helper.

## Models & migrations

- `models.CheckConstraint(condition=...)` — `check=` is removed in Django 6 (local venv may accept it; CI won't).
- Generate migrations, never hand-write; run `makemigrations --check --dry-run` before push; new public-table migrations need an RLS relock migration (workflow.md §pre-push).

## Testing

- `make test` = `python manage.py test apps/`. Prefer targeted runs (`python manage.py test apps.<app>.<module> --noinput`) while iterating.
- Tests that patch gateway/network functions rely on the local re-import pattern above.
- Query-count regressions: pin with `assertNumQueries` (see `apps/fuel/tests.py`, `apps/orchestrator/test_azure_client.py` for idempotency-shape examples).

### External SDK contract tests

- Every production third-party SDK call has an offline, mock-free real-SDK contract test.
- Name modules `test_sdk_contract_<lib>.py` in the app owning most call sites.
- When adding a new SDK call, extend its contract module with the exact signature/model path used.
- Construct real models/clients with dummy credentials; tests must never make network calls.
- Dependabot major bumps for contracted SDKs stay ignored until a coordinated review updates contracts.
- Reason: `azure-mgmt-storage` 25.x changed `.keys` into a method on 2026-08-24, breaking ~15 live paths while mocked CI stayed green.

## LLM-adjacent judgment calls

Backend computes evidence; the LLM judges. Don't encode fuzzy human judgments as arithmetic formulas in Python — pass structured evidence to the model and let it decide (established pattern across insights/fuel).
