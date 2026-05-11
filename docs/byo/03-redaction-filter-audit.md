# Redaction filter audit — no changes needed

## Question

The existing `apps/byo_models/logging_filters.py:RedactBYOPasteBody` was designed for the legacy endpoint shape (paste a bare token string). With the new endpoint accepting a full JSON blob (`credentials_json` field containing the entire `~/.claude/.credentials.json`), does the filter still redact properly?

## Answer

**Yes, no changes needed.** The filter is shape-agnostic — it triggers on the URL path, then redacts any JSON-shaped substring it finds in the log record.

## Evidence

From `apps/byo_models/logging_filters.py:22-69`:

```python
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_REDACT_PATH = "/api/v1/tenants/byo-credentials/"

class RedactBYOPasteBody(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # If any field contains the BYO endpoint path, redact JSON blocks
        # in `msg` and `args`, and wipe `body`/`data`/`request_body` attrs.
```

Key properties:
- **Path-triggered, not content-triggered**: looks for `/api/v1/tenants/byo-credentials/` in any of `msg`, `request`, `path`, `request_path`, `url`. Endpoint URL is unchanged in the refactor.
- **Regex is greedy `{.*}`**: matches ANY JSON-shaped block, including the new `{"provider":"anthropic","mode":"oauth_credentials","credentials_json":{...}}` body OR a flattened `{"provider":"anthropic","mode":"oauth_credentials","credentials_json":"{\"claudeAiOauth\":{...}}"}` body. Either nesting strategy is covered.
- **Wipes named body attrs**: `body`, `data`, `request_body` are unconditionally `[REDACTED]` when the path matches. Catches the common Django middleware patterns.

## What still needs the handoff agent's attention

1. **Keep the URL unchanged.** If the implementing agent moves the endpoint to a new path (e.g. `/api/v1/tenants/byo-credentials/oauth/`), update `_REDACT_PATH` to match.
2. **Don't introduce a log call that emits the raw body BEFORE the filter is applied.** The view code at `apps/byo_models/views.py:74` already avoids this — confirm the new mode branch preserves the discipline. The view's own logging calls already use only `tenant.id` and `provider`, never `token` or new equivalents.
3. **Add a test** in `apps/byo_models/tests.py` that exercises the filter against a synthetic log record carrying a fake credentials JSON. Pattern to copy: search for any existing test using `caplog` (pytest) or `assertLogs` (unittest). The test should:
   - Build a `LogRecord` with `msg` containing the BYO endpoint path + a fake JSON blob containing `accessToken`
   - Run it through `RedactBYOPasteBody().filter(record)`
   - Assert `accessToken` substring is gone from `record.msg` after filtering

This is belt-and-suspenders: the filter passed audit, but pinning behavior with a test prevents regression if someone widens the path matcher later.

## Bottom line

The filter is correct as-is. Document this with a one-line test addition and move on.
