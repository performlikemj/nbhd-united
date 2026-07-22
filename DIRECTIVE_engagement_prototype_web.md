# DIRECTIVE: Cross-pillar engagement prototype — WEB

**Goal.** The web counterpart of the iOS engagement prototype (see
`nbhd-ios/.claude/worktrees/engagement-prototype/DIRECTIVE_engagement_prototype.md` for the
full origin story). A *felt* prototype: a constellation card showing the four daily actions
lighting up through the day, plus a quiet celebratory RewardMoment. Look and feel over
correctness. Demo-gated, in-memory, throwaway-quality wiring. Branch
`feat/engagement-prototype` (this worktree), frontend only — **no backend/Django changes**.

**The four daily actions** (a NEW concept — do NOT call them "pillars" in user-facing copy;
that word already means the content-pillar enum surfaced elsewhere on Horizons):
- **Show up** — first visit of the day.
- **Move** — a Fuel workout completed.
- **Sit** — a Core meditation finished.
- **Reflect** — a Journal entry saved.

## Taste rules (binding — mirror of the iOS addendum)

- **Reward presence, never punish absence.** No negative states anywhere: no red/error tones,
  no "broken streak," no guilt copy. Undone actions are simply dim. The lever is
  commitment/consistency (identity mirror), never loss aversion.
- **The card = today's constellation.** Four stars, one per action, each with its accent.
  Not done = dim outline star. Done = lit + soft glow. Thin lines connect lit stars as they
  complete; all four = the constellation completes with a soft sustained shimmer — that IS the
  perfect-day state. The card is calm state, not fireworks: no animation on mere render beyond
  the standard `animate-reveal` entrance.
- **Celebration lives at the moment of action.** RewardMoment (a brief, quiet star-burst +
  one-line affirmation overlay) fires once on completion. When the fourth star lights, one
  slightly grander moment. That's all the fireworks anywhere.
- **Rhythm, not chains.** One quiet line under the constellation — `9 of the last 14 days ·
  best 11` framing. NO per-action streak counters.
- **Rest days are honored, not missed.** On a rest day the Move star renders in a distinct
  hollow starlight-gold "honored" state and counts toward the rhythm.
- **Voice.** Affirmations are quiet and specific — "You sat today." / "Three mornings in a
  row." Never hype, never emoji. Real-build vision: these lines come from the user's own
  assistant; prototype uses canned copy marked `// PROTOTYPE: mocked`.
- **Card title: "Today".** Small, glanceable. Never the bare word "Pillars."

## Placement + behavior

- Home: `frontend/app/horizons/page.tsx`. Flag ON → the card renders as the FIRST
  `HorizonsSection` (above Momentum) and the Momentum section is NOT rendered (the card
  absorbs it — leave the Momentum code in place, just don't render under the flag). Flag OFF →
  the page is IDENTICAL to main. The flag is the master switch for every prototype behavior.
- Demo flag: mirror `lib/constellation-game/flag.ts` exactly — helper in
  `lib/engagement/flag.ts`; enabled when `NEXT_PUBLIC_ENGAGEMENT_DEMO === "1"` OR
  `localStorage.nbhd_engagement_demo === "1"`; additionally, `?engagementDemo=1` on the
  Horizons URL sets the localStorage key (so one URL turns it on) — all reads try/caught.
- All four completion signals are MOCKED on web (fired from demo controls only) — mark each
  `// PROTOTYPE: mocked`. In-memory store only (`lib/engagement/store.ts`), no network, no
  persistence beyond the flag key, no React Query.
- Demo controls: a compact, visually subordinate row under the card (demo-only) that can
  (a) fire each of the four RewardMoments, (b) toggle each action's done state, (c) toggle
  Move's rest-day state, (d) reach the perfect-day state. Every screenshot state reachable.
  44px touch targets.

## Design-system rules (from DESIGN.md — canonical; ignore BRAND_GUIDE.md)

**Palette authority:** the implementation's code values match iOS and are authoritative; the hex examples below are stale.

- Tokens, never hex in components. Add four action accent CSS vars in
  `frontend/app/globals.css` + `tailwind.config.ts` mappings:
  Show up = `#7C6BF0` (brand purple) · Move = `#4ECDC4` (teal) · Sit = a new calm moonlight
  blue (pick something like `#8FB8E8` that sits in the palette) · Reflect = `#A882FF`
  (Obsidian lavender). Honored/rest gold is a STATE color (soft starlight gold), reserved —
  never a fourth accent.
- Reuse the node/line/glow technique from
  `frontend/components/onboarding/constellation-progress.tsx` (best-in-repo template) and the
  `.glass-card-horizons` / `HorizonsSection` wrapper conventions. Only `shadow-panel` + named
  glows; no new shadow classes; no gradient escalation.
- Animation: pure Tailwind keyframes / CSS (twinkle/reveal vocabulary). NO new dependencies.
  Every animation gated on `prefers-reduced-motion` (shimmer degrades to static glow) and
  sane under `forced-colors: active`. Status conveyed via text (sr-only labels per star:
  done / not yet / rest day), never color alone.
- Components: `frontend/components/engagement/` — kebab-case files, function components,
  `clsx`, `"use client"` where stateful.

## Out of scope

Backend streak computation, real completion events, persistence, tenant flags, nav changes,
any Django/API change. This is the mock we react to.

## Deliverable

`npm run lint` and `npm run build` both pass in `frontend/`; screenshots of (1) early-day
card, (2) a RewardMoment firing, (3) perfect-day state, (4) rest-day honored Move star.
