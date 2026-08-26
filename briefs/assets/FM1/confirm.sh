#!/bin/sh
set -eu

ASSET_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$ASSET_DIR/../../.." && pwd)
cd "$REPO_ROOT"

require_line() {
    file=$1
    pattern=$2
    label=$3
    if ! grep -Eq "$pattern" "$file"; then
        printf 'missing %s in %s\n' "$label" "$file" >&2
        exit 1
    fi
    printf 'confirmed %s\n' "$label"
}

require_line apps/fuel/runtime_views.py '^[[:space:]]*if replace_schedule:$' 'replace merge branch'
require_line apps/fuel/runtime_views.py '^[[:space:]]*effective_schedule = dict\(current_schedule\)$' 'merge-by-default effective schedule'
require_line apps/fuel/runtime_views.py '^[[:space:]]*if "remove_days" in data:$' 'remove_days handler'
require_line apps/fuel/runtime_views.py '^[[:space:]]*replace_schedule = data\.get\("replace_schedule"\) is True$' 'replace_schedule flag'
require_line apps/fuel/test_update_plan_merge.py '^[[:space:]]*def test_partial_weekend_schedule_merges_without_deleting_weekdays\(self\):$' 'weekend merge test'
require_line apps/fuel/test_update_plan_merge.py '^[[:space:]]*def test_remove_days_archives_and_deletes_only_named_day\(self\):$' 'remove_days test'
require_line apps/fuel/test_update_plan_merge.py '^[[:space:]]*def test_replace_schedule_true_replaces_whole_template\(self\):$' 'replace_schedule test'
require_line apps/fuel/test_update_plan_merge.py '^[[:space:]]*def test_implicit_null_removal_of_prescribed_day_self_corrects\(self\):$' 'implicit-removal rejection test'
require_line runtime/openclaw/plugins/nbhd-fuel-tools/index.js '^[[:space:]]*remove_days: \{$' 'tool remove_days schema'
require_line runtime/openclaw/plugins/nbhd-fuel-tools/index.js '^[[:space:]]*replace_schedule: \{$' 'tool replace_schedule schema'
require_line runtime/openclaw/plugins/nbhd-fuel-tools/index.test.mjs '^test\("nbhd_fuel_update_plan forwards remove_days and replace_schedule"' 'tool schema execution test'

printf 'FM1_CONFIRM_OK\n'
