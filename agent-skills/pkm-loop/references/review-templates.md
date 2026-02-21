# PKM Review Templates

## Weekly review draft template

Use this template for the weekly review output before user confirmation:

```markdown
# Weekly Review — {{ WEEKS_START }} to {{ WEEKS_END }}

## What went well
- ...
- ...

## What was delayed or risky
- ...
- ...

## Completed tasks
- ...

## Recurring lessons / patterns
- **Lesson:** ...
- **Lesson:** ...

## What to adjust next week
- 1) ...
- 2) ...
- 3) ...

## Ask
Want me to save this as a weekly review now?
If yes: I will write to `kind: "weekly"`, `slug: "{{ START_MONDAY_DATE }}"`.
```

## Monthly goals check template

Use this on the 1st of month (or when monthly-mode automation runs):

```markdown
# Monthly Goals Check — {{ MONTH }}

## Goals with momentum
- [Goal] ... (why it’s still strong)

## Stalled or stale goals
- [Goal] ... (what’s missing: owner/deadline/decision)

## Tasks alignment
- Active heavy blockers: ...
- Tasks likely ready to close as done: ...

## Suggested goal actions
- Keep: ...
- Defer: ...
- Split/rename: ...
- Retire: ...

## Ask
Want me to apply any of these goal/task structure updates now?
If yes, confirm in one pass: keep / defer / split / retire.
```

## End-of-day reflection snippet

```markdown
# Daily Reflection — {{ DATE }}

## Today’s key move
- ...

## What I learned
- ...

## Pending tasks
- ...

## Open lesson candidates
- ...

## Ask
Want me to save a short daily summary?
```
