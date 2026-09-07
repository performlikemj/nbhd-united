# Completion evidence and meditation lessons

Fuel workouts and Core meditations use `status="done"` plus a nullable
`completed_at` timestamp. Existing rows are not backfilled. Rendered meditation
audio (`ready` or `delivered`) is playable but is not evidence of practice.
Completed meditations remain in the default library and remain playable.

## Player completion API

`POST /api/v1/core/sessions/<uuid>/complete/` requires the owner's JWT and scopes
the row to that owner's tenant. No runtime completion route is provided.

- Optional body: `{}` or `{"listened_seconds": 600}`. Listening duration is logged,
  not stored as another field.
- `ready` or `delivered`: set `status="done"` and stamp server time.
- `done`: return the unchanged row, including its original timestamp.
- `pending`, `rendering`, or `failed`: HTTP 409, `{"error":"not_ready"}`.
- Missing/other-tenant row: HTTP 404. Missing JWT: HTTP 401.

HTTP 200 returns the same representation as the detail GET, including read-only
`status`, `completed_at` (ISO 8601), and `lesson` (an object, empty on legacy rows).
The feedback PATCH cannot change those fields. Completion means the player
reported reaching the end; the server does not independently measure listening.

Core practice counts and streaks use the tenant-local day of `completed_at`,
not the day the audio was composed. Runtime summary reports `total_sessions`,
`last` (`id`, `title`, `date`, `completed_at`), and `ready_unplayed`
(`id`, `title`, `date`, or null). The web console uses the same counting rule.

Fuel completion writers stamp server time and preserve the timestamp on repeated
completion; leaving `done` clears it. HealthKit uses the sample's end time; older
clients omitting `ended_at` use sample start plus measured duration. Summary and
audit rows expose status and completion time. Congratulations recheck status in
Python before scheduling and again before delivery for the workout's cron name.

## Lesson variety

`apps/core/lesson.py` owns the tradition vocabulary and Pydantic schemas. Compose
requests a manifest JSON schema, falling back to JSON object mode for a model
that returns a 4xx; local lesson and render validation still apply. Lesson prose
passes through the same PII authoring registry as theme before persistence.

The newest 20 playable titles feed variety history, with whole prompt entries
capped at 2,600 characters. Compose rejects previously used teaching slugs
(case/punctuation insensitive) and either of the last two known traditions.
It allows one corrective retry per model, then moves to the next candidate.

If every model clashes and at least one manifest is structurally valid, the last
valid sit is accepted with a warning. Set `CORE_COMPOSE_STRICT_VARIETY=True` to
fail instead. Default: false. Logs report `accepted_first`,
`accepted_after_retry`, or `clash_accepted`. Legacy rows with no lesson contribute
title/theme context only. The deterministic gate detects repeated tags and
traditions; semantic alignment of the narration with its lesson remains a prompt
requirement.
