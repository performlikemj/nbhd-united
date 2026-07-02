# Integration snippet — templates/openclaw/AGENTS.md (PR-3 insights → all pillars)

`templates/openclaw/AGENTS.md` is owned by the identity builder, so PR-3 does
not edit it directly. One small change is needed so the in-prompt summary of
the insight marker mentions the new optional pillar prefix. The full reference
in `rules/reply-markers.md` (owned by PR-3) is already updated.

## Change 1 — the Insights marker blurb (currently ~lines 98–102)

REPLACE this block:

```
**Insights — `[[insight:topic_slug]]statement[[/insight]]`**

When your reply raises a falsifiable pattern observation *about this user* (something you wouldn't write in a context-free Q&A), wrap that sentence in an insight marker. The platform records an `AssistantInsight` row; only the marker tokens are stripped, the statement stays visible. This is the primary mechanism that fills Horizons' "What I remember" / "Topics I've learned" — without it those panels stay empty.

> Looking at your trajectory, [[insight:debt]]you're carrying balances across 8 lines and staying in debt 20+ years on most of them[[/insight]] — the avalanche fix kicks in around month 8.
```

WITH this block:

```
**Insights — `[[insight:pillar/topic_slug]]statement[[/insight]]`**

When your reply raises a falsifiable pattern observation *about this user* (something you wouldn't write in a context-free Q&A), wrap that sentence in an insight marker. The platform records an `AssistantInsight` row; only the marker tokens are stripped, the statement stays visible. This is the primary mechanism that fills Horizons' "What I remember" / "Topics I've learned" — without it those panels stay empty.

Prefix the slug with the **pillar** the observation is about — `gravity` (money), `fuel` (training/body), `core` (practice), `journal` (mood/life), etc. A bare `[[insight:debt]]` with no prefix files under `journal`. Only use the `gravity` prefix inside an actual Gravity/finance conversation: gravity insights are recorded **only when the Gravity module is active for this user** and dropped otherwise, so don't file money observations for a user who isn't using Gravity. Full guidance + topic lists: `rules/reply-markers.md`.

> Looking at your trajectory, [[insight:gravity/debt]]you're carrying balances across 8 lines and staying in debt 20+ years on most of them[[/insight]] — the avalanche fix kicks in around month 8.
```

## Change 2 (optional) — the reference-docs table row (~line 82)

The row already reads:

```
| `rules/reply-markers.md` | Platform-processed markup in replies — `[[chart:...]]`, `[[insight:...]]` |
```

No change required; `[[insight:...]]` still covers the pillar-prefixed form. Left
here only so the integrator knows it was reviewed.
