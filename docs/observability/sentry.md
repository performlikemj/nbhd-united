# Sentry — operator runbook

Practical guide for watching NBHD United in production with Sentry. Written for
someone new to Sentry: what's set up, where to look, and what to actually *do*
when something breaks.

- **Org / project**: `by-way-of-mj` / `python-django`
- **Dashboard**: https://by-way-of-mj.sentry.io/issues/?project=4511612676145232
- **Region**: EU (`ingest.de.sentry.io`)

## What's captured (and what isn't)

| Captured | Not captured (by design) |
|---|---|
| Unhandled errors (Django requests, views, ORM) | Request bodies, cookies, user emails, client IPs |
| `logging` records → Sentry **Logs** stream | Anything during a test run (`make test` is gated off) |
| Performance traces + profiles | Events from local dev (no `SENTRY_DSN` set locally) |
| A `tenant` tag on every event (filter by tenant) | Financial amounts / PII (scrubbed; `send_default_pii=False`) |

It's **gated**: Sentry only runs in production, where `SENTRY_DSN` is set on the
`nbhd-django-westus2` Container App (as a secret). Config lives in
`config/settings/base.py`; tunables are env vars (see "Tuning" below).

## The 5 commands you'll actually use

The `sentry` CLI auto-detects the org/project. (`brew`/`curl` install + `sentry
auth login` once.)

```bash
# 1. What's broken right now?
sentry issue list --query "is:unresolved" --limit 20

# 2. Look at one issue in detail (stack trace, tags, frequency)
sentry issue view PYTHON-DJANGO-XX          # the short ID from the list
sentry issue view PYTHON-DJANGO-XX --web     # ...or open it in the browser

# 3. Let Sentry's AI explain the root cause / propose a fix
sentry issue explain PYTHON-DJANGO-XX
sentry issue plan PYTHON-DJANGO-XX

# 4. Tail logs as they happen (your "what is it doing?" view)
sentry log list --follow
sentry log list --query "severity:error"

# 5. Once handled, close it out
sentry issue resolve PYTHON-DJANGO-XX
```

Filter by tenant (multi-tenant superpower — "why is tenant X broken?"):

```bash
sentry issue list --query "tenant:<tenant-uuid> is:unresolved"
```

## Daily "is everything healthy?" check

```bash
sentry issue list --query "is:unresolved is:for_review" --limit 10   # needs triage
sentry issue list --query "is:unresolved firstSeen:-24h"             # new in last day
```

If both are empty, you're clean. If not, open the top issue (`sentry issue
view`), read the stack trace, and check the **suspect commit** (see below) to
know what change introduced it.

## Alerts (you get pinged, you don't have to poll)

Two issue-alert rules are active, both emailing you:

- **Notify on any new issue** — fires the first time *any* new issue appears.
- **Send a notification for high priority issues** — Sentry's default escalation.

```bash
sentry alert issues list by-way-of-mj/python-django
```

To also route alerts to Slack or your ops Telegram, add the integration in
Sentry → Settings → Integrations, then add it as an action on the rule.

## Releases & suspect commits

Each deploy tags events with the git SHA (`SENTRY_RELEASE`), so an error shows
which build it first appeared in:

```bash
sentry release list by-way-of-mj/python-django
sentry release view by-way-of-mj/<sha>
```

**To light up "suspect commit" (the likely-culprit commit + author) on each
issue**, two one-time setup steps are needed:

1. Add a repo secret **`SENTRY_AUTH_TOKEN`** in GitHub (Settings → Secrets →
   Actions). Create the token at Sentry → Settings → Auth Tokens with
   `project:releases` + `org:read` scopes.
2. Connect the GitHub repo in Sentry → Settings → Integrations → GitHub (so
   commits map to authors).

The CI step that creates the release (`Create Sentry release` in
`.github/workflows/ci-cd.yml`) is already wired and **skips itself** until the
secret exists, so nothing breaks in the meantime.

## Tuning (no redeploy — these are Container App env vars)

| Env var | Default | What it does |
|---|---|---|
| `SENTRY_DSN` | (unset) | The on/off switch. Unset = Sentry disabled. |
| `SENTRY_TRACES_SAMPLE_RATE` | `1.0` | Fraction of requests traced. Lower (e.g. `0.1`) as traffic grows. |
| `SENTRY_PROFILE_SESSION_SAMPLE_RATE` | `1.0` | Fraction of profiling sessions. |
| `SENTRY_ENABLE_LOGS` | `true` | Forward `logging` to the Logs stream. |
| `SENTRY_SEND_DEFAULT_PII` | `false` | Keep `false` — sends PII to Sentry if `true`. |

```bash
# example: dial tracing down to 10% in production
az containerapp update -n nbhd-django-westus2 -g rg-nbhd-prod \
  --set-env-vars SENTRY_TRACES_SAMPLE_RATE=0.1
```

## Send a test event (verify the pipe end-to-end)

```bash
sentry event send --message "manual test from $(whoami)" --level info
sentry issue list --limit 3        # it should appear within ~30s
```

## Turn it off

Remove the DSN; the SDK is then inert (no events, no overhead):

```bash
az containerapp update -n nbhd-django-westus2 -g rg-nbhd-prod \
  --remove-env-vars SENTRY_DSN
```
