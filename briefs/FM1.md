# FM1 — Fuel update_plan merge-by-default

## Read nothing else

Do not open any other brief, directive, continuity file, or source file. The shipped assets are the complete change.

1. Run `sh briefs/assets/FM1/step-1.sh` FIRST.
2. Run `sh briefs/assets/FM1/confirm.sh`.
3. Run the gates:

   `/Users/michaeljones/Projects/nbhd-united/.venv/bin/ruff check . && /Users/michaeljones/Projects/nbhd-united/.venv/bin/ruff format --check .`

   `/Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py check && /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py makemigrations --check`

   `/Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py test apps.fuel --noinput 2>&1 | tail -4`

   `node --test runtime/openclaw/plugins/nbhd-fuel-tools/*.test.mjs`

4. Stage only the placed files:

   `git add apps/fuel/runtime_views.py apps/fuel/tests.py apps/fuel/test_update_plan_merge.py runtime/openclaw/plugins/nbhd-fuel-tools/index.js runtime/openclaw/plugins/nbhd-fuel-tools/index.test.mjs`

5. Commit exactly:

   `git commit -m "fix(fuel): update_plan merges schedule_json by default — days are removed only via remove_days/replace_schedule (never silently)"`
