# Cross-tenant ignore-pattern analysis (Lane 3) — 2026-08-08

> Evidence basis for the Lane 2 fleet never-a-name stoplist (`apps/pii/redactor.py`) and the
> `retire_stoplisted_bindings` backfill. All counts are as-of 2026-08-08 and are not refreshed.

Read-only aggregate over prod (46 tenants). No per-tenant identities recorded; only counts and denied words
(words users explicitly marked "not PII"). Query basis: `tenants.pii_denylist` keys + `tenants.pii_entity_map`
entries (name matched lowercased; values never extracted, only counted).

## Headline numbers

- 46 tenants · 1,224 total denies · **914 distinct denied words** · 99 shared by ≥2 tenants · 28 by ≥4.
- Entity maps fleet-wide: 6,083 bindings (6,008 object + 75 legacy string), **6,058 active** / 25 retired.
- The 25 retired = canary's Lane-1 backfill only. **No other tenant has ANY retired binding** → the
  retire-on-deny backfill (#1402 `retire_denied_bindings`) has never run fleet-wide; every pre-08-07 deny
  on 45 tenants still has live bindings substituting placeholders for words the user said to ignore.

## What people ignore (≥4 tenants; kind = what the detector minted it as)

| Family | Words (tenants denying) | Live bindings fleet |
|---|---|---|
| App/template vocabulary minted as PERSON | calendar (22), quick wins (22), goal (19), morning briefing (9), heartbeat (8), heartbeat check-in (7), evening check-in (6), calendar status (4), 🏆 wins (6) | calendar **830**, quick wins 345, goal 247, morning briefing 50, calendar status 28, heartbeat 20, heartbeat check-in 19, evening check-in 13, 🏆 wins 12 |
| Markdown fragments minted as PERSON (spans crossing newlines/bullets) | "quick wins\n-" (19), "quick wins\n- reply" (4), task-list scaffolding "- [ ] …## ⏳ waiting…" (2) | "quick wins\n-" **239**, "quick wins\n- reply" 10 |
| Brands/products minted as PERSON or LOCATION | gmail (12), nvidia (6), overcast (6), google calendar (5), youtube (2), fedex (2), telegram (2), claude (2) | gmail-as-PERSON **178**, google calendar 22, nvidia-as-LOC 16, overcast-as-LOC 16 |
| Geography/news (true LOCATION, users opt out) | us (16), japan (9), iran (6), gulf (6), uk (6), china (5), israel (4), middle east (4) | iran 258, us 254, japan 180, israel 59, uk 23 |
| Date/short tokens | mar (12), ai (9), max (4), daily (4), fri/sun/sat, jst, w16 | mar-as-PERSON 56, **max-as-PERSON 36 ⚠ real-name-shaped**, ai-as-LOC 92 |
| Groups-as-PERSON | houthis (4) | 4 |

## Duplicate-mint pathology (new finding)

Active bindings per (tenant, same lowercased name):

| Dups per name | (tenant,name) pairs | Bindings |
|---|---|---|
| 1 | 703 | 703 |
| 2–3 | 230 | 526 |
| 4–10 | 170 | 1,010 |
| 11–50 | 137 | 2,765 |
| 50+ | 15 | 1,054 |

→ **~4,800 of 6,058 active bindings (~80%) are duplicate mints** of a name that already had a binding in
that tenant. Worst case: one tenant holds **82 separate active "calendar" PERSON bindings** (21 tenants
affected for that word, avg 39.5 each). Not just junk words: PERSON names NOT in any shared denylist still
have 353 bindings sitting in heavy-dup (11+) groups — real names get re-minted too. The minter (or a
background path) is not reusing existing bindings.

## Junk vs real split (active bindings)

| Kind | Name in shared (≥2) denylist | Pairs | Bindings | of which in 11+-dup groups |
|---|---|---|---|---|
| PERSON | yes (junk) | 234 | 2,417 | 1,920 |
| PERSON | no (mostly real) | 451 | 1,110 | 353 |
| LOCATION | yes | 173 | 1,552 | 1,157 |
| LOCATION | no | 230 | 812 | 389 |
| EMAIL/ACCOUNT/IP/etc. | — | 155 | 167 | 0 (no dup problem) |

Deterministic kinds (EMAIL, IP, PHONE, ACCOUNT…) are clean — the pathology is exclusively the NER kinds.

## Recommendations (feed Lane 2)

1. **R1 — Finish Lane 1 fleet-wide (quick win, shipped code):** run the #1402 retire backfill logic across
   all tenants (dry-run → review counts → commit). Kills every live binding for words each user already
   denied. No new policy; completes an approved fix.
2. **R2 — Global never-a-name list (curated, config-level):** exact canonical-key matches only, sourced from
   the ≥2-tenant denied set MINUS anything name-shaped. Include: app/template vocabulary, brand names,
   demonym adjectives ("japanese", "american"… — nationality words are never personal names). EXCLUDE:
   max, mar, theo, la, moon, pistachio, spark and anything plausibly a name/nickname. Consumed at the same
   chokepoint as the per-tenant denylist check (skip redaction + skip mint). Under-redaction risk ≈ 0 by
   construction of the exclusion rule.
3. **R3 — Span hygiene at mint:** never mint a span containing a newline, list-bullet, emoji-only content,
   or heading markup; length cap. Kills the markdown-fragment class regardless of vocabulary. No
   capitalization requirement (lowercase real names must keep working).
4. **R4 — Mint dedup:** stop creating a new binding when the same canonical name already has exactly one
   active binding of the same kind in the tenant (ambiguous multi-binding names keep current behavior —
   same-name-different-people stays supported). Fixes 80% map bloat + keeps one stable placeholder per
   person (better model coherence).
5. **R5 — Junk retire backfill for the global list:** after R2 lands, retire (NOT delete — rehydration must
   survive) active bindings whose name is on the global list. Dry-run report by counts; canary → fleet.
6. Geography words (iran/japan/us…): leave under per-tenant control (R1 handles the ones already denied).
   Country-level redaction is real signal for some users; do not globally exempt in v1.
