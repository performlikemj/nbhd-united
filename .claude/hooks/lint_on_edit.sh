#!/bin/bash
# PostToolUse(Edit|Write) — auto-fix lint AND format, mirroring both CI gates
# (`ruff check .` and the stricter `ruff format --check .`).
# Known sharp edge: `check --fix` strips imports with no call site yet —
# add import + first usage in the same Edit (docs/agents/backend.md).

FILE=$(jq -r '.tool_input.file_path // .tool_input.filePath // empty' 2>/dev/null)
[ -n "$FILE" ] && [ -f "$FILE" ] || exit 0

case "$FILE" in
  *.py)
    RUFF="$CLAUDE_PROJECT_DIR/.venv/bin/ruff"
    [ -x "$RUFF" ] || RUFF=ruff
    cd "$CLAUDE_PROJECT_DIR" || exit 0
    "$RUFF" check --fix --quiet "$FILE" 2>/dev/null
    "$RUFF" format --quiet "$FILE" 2>/dev/null
    ;;
  */frontend/*.ts|*/frontend/*.tsx)
    cd "$CLAUDE_PROJECT_DIR/frontend" || exit 0
    npx --no-install eslint --fix --quiet "$FILE" 2>/dev/null
    ;;
esac
exit 0
