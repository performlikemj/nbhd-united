# Notice phrasings — pick one

Three candidate phrasings for the AGENTS.md fallback notice. All are designed to be read by the model (instructions, not user-facing copy), so the model decides timing and translates to the user's language naturally.

Pick one, or mix-and-match. The handoff agent will paste the chosen text into the `_CLAUDE_FALLBACK_NOTICE` constant per [05-persona-render-pipeline.md](./05-persona-render-pipeline.md).

---

## Option A — Direct, terse

```markdown
## NBHD platform note

This tenant has not connected their own Claude subscription, so this conversation is running on a smaller fallback model. Once per conversation — not in the first two turns, not during cron / scheduled-task turns — gently mention that connecting their Claude account at <https://neighborhoodunited.org/settings/ai-provider> will give them better responses. Phrase the suggestion in whatever language the user has been writing in. Do not nag, do not repeat it.
```

**Tone:** matter-of-fact. Reads like an internal instruction.

---

## Option B — Warmer, framed as a helpful tip

```markdown
## NBHD platform note

This tenant is currently on a fallback model rather than their own connected Claude subscription. When it feels natural in the conversation — after the first couple of turns, and never during a scheduled-task run — let them know that connecting their own Claude account at <https://neighborhoodunited.org/settings/ai-provider> takes about 30 seconds and noticeably improves response quality. Match the language the user has been writing in. Mention it at most once per conversation; if they decline or change subject, drop it.
```

**Tone:** friendlier, gives the model more permission to be conversational about it.

---

## Option C — Conditional, with examples

```markdown
## NBHD platform note

This tenant has not connected their own Claude subscription. This conversation is on a fallback model.

When it would be useful to mention it:
- After the user has had time to get into the conversation (not before turn 3)
- When the conversation is going well and the suggestion would feel like a tip, not a pitch
- In whatever language the user has been writing in

When NOT to mention it:
- During cron / scheduled-task turns (the user isn't here)
- If they've declined or you've already mentioned it this conversation
- In the middle of urgent or emotional content
- If the conversation is so short there's no natural opening

When you do mention it, the link is <https://neighborhoodunited.org/settings/ai-provider> and setup takes about 30 seconds.
```

**Tone:** most prescriptive — gives the model concrete heuristics. Longest, but cheapest to maintain because edge cases are spelled out.

---

## My recommendation

**Option B.** It's the right balance of friendliness and constraint. Option A is so terse the model might interpret it stiffly; Option C is so prescriptive it reads like a rulebook (which actually works fine for instruction-following but eats tokens on every turn). Option B lets the model use judgment while still preventing the obvious failure modes (nagging, cron mentions, first-turn pushes).

## What I can't decide for you

- **The URL.** I assumed `neighborhoodunited.org/settings/ai-provider`. If the public domain is different (e.g. `app.neighborhoodunited.org` or you redirect from the marketing site), update before shipping.
- **Whether to include the 30-second framing.** It's a marketing claim ("only takes 30 seconds"). Probably true given the flow is upload-a-file, but if you want to be conservative, drop "takes about 30 seconds" and let the model omit timing.
