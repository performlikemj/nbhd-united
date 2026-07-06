#!/bin/bash
# PostToolUse(Edit|Write) — inject a reminder when config/settings/production.py
# is touched: env var NAMES must match the Azure Container App env vars.

FILE=$(jq -r '.tool_input.file_path // .tool_input.filePath // empty' 2>/dev/null)
case "$FILE" in
  */config/settings/production.py)
    cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"REMINDER (hook): you edited config/settings/production.py. Env var NAMES here must match the Azure Container App env vars on nbhd-django-westus2 — renaming or adding one without updating Azure breaks prod at the next deploy. Cross-check with: az containerapp show -n nbhd-django-westus2 -g rg-nbhd-prod --query 'properties.template.containers[0].env[].name'"}}
EOF
    ;;
esac
exit 0
