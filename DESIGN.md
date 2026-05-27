# DESIGN.md — NBHD United

Agent-facing design system for the Subscriber Control Console (`frontend/`) and the public landing at https://neighborhoodunited.org.

Source of truth for tokens: [`frontend/app/globals.css`](frontend/app/globals.css). This file is the prompt context — keep it aligned when `globals.css` changes.

---

## 1. Visual Theme & Atmosphere

**Constellation / inner universe.** The product's metaphor is "there are as many neurons in your brain as stars in the Milky Way." Every surface should feel like looking into a calm, luminous night sky — deep space backdrops, starfield overlays, synapse/constellation line art, soft purple/teal/pink glows.

- **Mood:** contemplative, private, slightly reverent. Not playful, not corporate.
- **Default mode:** dark. The app does not ship a light theme — `color-scheme: dark` is set at root.
- **Motion:** subtle and staggered. Things *reveal* and *twinkle*, they don't bounce or whoosh.
- **Depth:** achieved through glass (backdrop-blur + low-opacity fills) and coloured glow shadows, not hard drop shadows.
- **Type rhythm:** a serif display face (`Instrument Serif` / `DM Serif Display`) for emotional moments; a geometric sans (`Space Grotesk`) for product headings; a humanist sans (`Plus Jakarta Sans`) for body copy.

Signature visual devices (reuse these rather than inventing new ones):

- **Starfield** — animated SVG dots (`components/landing/starfield.tsx`).
- **Constellation lines** — thin lines between nodes (`components/landing/constellation-lines.tsx`).
- **Synapse network** — faint branching web used at ~4–12% opacity as a background layer (`components/landing/synapse-network.tsx`).
- **Tri-colour gradient text** — `bg-gradient-to-r from-c-purple via-c-pink to-c-teal bg-clip-text text-transparent` for hero phrases.
- **Glass cards** — `.glass-card` (purple-tinted) and `.glass-card-horizons` (cooler slate).
- **Purple glow** — `.glow-purple` / `.glow-purple-hover` on primary CTAs and brand marks.

---

## 2. Color Palette & Roles

All colours are exposed as CSS variables in `:root` and as Tailwind utilities via [`tailwind.config.ts`](frontend/tailwind.config.ts). **Always use the token, never the hex.**

### Core (dark)

| Token | Value | Role |
|---|---|---|
| `--bg` / `bg-bg` | `#0b0f13` | Page background (deep space) |
| `--mist` / `bg-mist` | `#0f1419` | Slightly raised background band |
| `--surface` / `bg-surface` | `#161b22` | Cards, header, menus |
| `--surface-elevated` | `#1e252e` | Elevated cards / popovers |
| `--surface-hover` | `rgba(226,232,240,0.06)` | Row/surface hover state |
| `--card` / `bg-card` | `rgba(22,27,34,0.95)` | Semi-transparent card fill |
| `--ink` / `text-ink` | `#e2e8f0` | Primary text |
| `--ink-muted` / `text-ink-muted` | `rgba(226,232,240,0.72)` | Secondary text |
| `--ink-faint` / `text-ink-faint` | `rgba(226,232,240,0.42)` | Tertiary text, placeholders, eyebrows |
| `--border` | `rgba(226,232,240,0.12)` | Default hairline borders |
| `--border-strong` | `rgba(226,232,240,0.25)` | Emphasised borders (hover) |
| `--overlay` | `rgba(0,0,0,0.70)` | Modal/drawer backdrop |

### Brand accents

| Token | Value | Role |
|---|---|---|
| `--accent` / `bg-accent text-accent` | `#7C6BF0` | Primary action, active nav, links |
| `--accent-hover` | `#9B8DF5` | Accent hover |
| `--signal` / `bg-signal` | `#4ECDC4` | Positive signal, secondary accent |
| `--signal-text` / `text-signal-text` | `#4ECDC4` | Signal used as text |
| `--signal-faint` | `rgba(78,205,196,0.15)` | Signal hover / soft fill |

### Constellation palette (landing + marketing)

Only used under `.landing-dark` / `.constellation-bg` / `.nebula-bg`. Tailwind prefix: `c-`.

| Token | Value | Role |
|---|---|---|
| `c-purple` | `#7C6BF0` | Mirrors `--accent`, primary constellation colour |
| `c-teal` | `#4ECDC4` | Secondary constellation colour |
| `c-pink` | `#E8B4B8` | Tertiary constellation colour (warm highlight) |
| `c-dark` | `#0B0F13` | Constellation canvas |
| `c-surface` | `rgba(255,255,255,0.03)` | Glass card fill |
| `c-border` | `rgba(255,255,255,0.1)` | Glass card border |
| `c-text` | `#E2E8F0` | Primary text on constellation |
| `c-text-muted` | `#94A3B8` | Secondary text on constellation |
| `c-text-faint` | `#64748B` | Tertiary text on constellation |

### Status tones (semantic)

Use these for pills, banners, alerts, and status surfaces. Each pair is WCAG-AA on the dark surface.

| Intent | BG token | Text token | When |
|---|---|---|---|
| Success | `status-emerald` | `status-emerald-text` | Success, active, thumbs-up, high |
| Error / destructive | `status-rose` | `status-rose-text` | Failed, error, thumbs-down |
| Warning | `status-amber` | `status-amber-text` | Pending, expired, low, meh, suspended |
| Info (running) | `status-sky` | `status-sky-text` | Running, provisioning, medium |
| Neutral | `status-slate` | `status-slate-text` | Paused, skipped, deleted |
| Manual | `status-indigo` | `status-indigo-text` | Manual actions |
| Scheduled | `status-violet` | `status-violet-text` | Scheduled jobs |
| Deprovisioning | `status-orange` | `status-orange-text` | Tear-down flows |

Semantic aliases for inline error/warning/success regions: `rose-bg` / `rose-text` / `rose-border`, `amber-bg` / `amber-text` / `amber-border`, `emerald-bg` / `emerald-text`.

---

## 3. Typography Rules

Fonts are loaded via Next.js font module and exposed as CSS variables.

| Role | Variable | Stack | Where |
|---|---|---|---|
| Body | `--font-body` | `"Plus Jakarta Sans", "Avenir Next", "Trebuchet MS", "Verdana", sans-serif` | Default everywhere |
| Headline | `--font-headline` / `font-headline` | `"Space Grotesk", sans-serif` | Section titles, card titles, page H1 inside the app |
| Display serif | `--font-display` / `font-display` | `"DM Serif Display", "Georgia", serif` | Big marketing moments, occasionally brand marks |
| Editorial serif | `--font-serif` / `font-serif` | `"Instrument Serif", Georgia, serif` | Blockquotes, reverent pull-quotes |
| Mono | `--font-mono` / `font-mono` | `"IBM Plex Mono", "Consolas", "Menlo", monospace` | Eyebrows, labels, code, tenant IDs, initials |

### Scale (reference)

| Level | Size | Line height | Weight | Notes |
|---|---|---|---|---|
| Landing H1 | `clamp(2.5rem, 5vw + 0.5rem, 4.5rem)` | `leading-tight` | 700 | `font-headline`, often gradient-clipped |
| App page H1 | `text-lg` / `text-xl` | 1.3 | 600 | Inside the header chrome |
| H2 (marketing) | `clamp(2rem, 4vw + 0.5rem, 3.25rem)` | 1.15 | 700 | `font-headline` |
| H2 (card title) | `text-xl` | 1.2 | 700 | `font-headline`, `text-ink` |
| H3 | `text-lg` / `text-xl` | 1.25 | 600 | `font-headline` |
| Body | `text-sm` (`0.875rem`) default, `text-base` for long-form | 1.5–1.6 | 400 | `text-ink-muted` is the norm; `text-ink` for emphasis |
| Small / hint | `text-xs` | 1.5 | 400 | `text-ink-faint` |
| Eyebrow | `text-[10px]` – `text-xs` | 1.4 | 500–600 | `uppercase tracking-[0.12em]`–`tracking-[0.3em]`, often `font-mono` |
| Pull quote | `clamp(1.5rem, 3vw + 0.5rem, 3rem)` | 1.25 | 400 italic | `font-serif` |

### Rules

- Body copy defaults to `text-ink-muted`. Reserve `text-ink` for headings and content that must pop.
- Pair a serif headline with sans body, never two serifs.
- Eyebrows are **uppercase, wide tracking, monospaced or 500-weight** — never sentence case.
- Use `clamp()` for hero / page titles so they scale fluidly. Use `text-sm` / `text-base` for body — don't clamp body copy.

---

## 4. Component Stylings

Prefer the existing components in `frontend/components/` before writing new ones. Patterns below document the canonical styling.

### Buttons

**Primary CTA (purple, glowing):**
```html
<button class="glow-purple rounded-full bg-accent px-4 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]">
```
Variants on landing use `rounded-lg` + `glow-purple-hover` and larger padding (`px-8 py-4` / `px-12 py-5`).

**Secondary (ghost on dark):**
```html
<button class="rounded-lg border border-white/20 bg-transparent px-8 py-4 text-sm font-semibold text-c-text transition-all hover:bg-white/5 active:scale-95 min-h-[44px]">
```

**Icon / tool button (dense):** 36×36 is acceptable for editor toolbars; otherwise 44×44 min.

Rules:
- Primary actions use `bg-accent` + `.glow-purple`. Never use a different hex for the primary colour.
- Hover = `brightness-110` (primary) or soft surface tint (`hover:bg-surface-hover`, `hover:bg-white/5`).
- Pressed = `active:scale-[0.98]` or `active:scale-95`.
- Disabled = `opacity-50 cursor-not-allowed`. Do not grey-out the background — just dim.

### Cards & panels

Canonical wrapper is [`SectionCard`](frontend/components/section-card.tsx):
```
rounded-panel border border-border bg-card/95 p-4 sm:p-5 shadow-panel animate-reveal backdrop-blur-md
```
- `rounded-panel` (20px) is the default card radius for the app shell.
- Card headers: `font-headline text-xl font-bold text-ink`, optional subtitle `text-sm text-ink-muted`.

[`StatCard`](frontend/components/stat-card.tsx) adds a tinted top border and a hover glow keyed to `tone` (`accent` purple, `signal` teal, `error` rose). Use for dashboard tiles.

[Glass card](frontend/app/globals.css) for the landing and unauthenticated pages:
```
glass-card rounded-xl p-8   /* or glass-card-horizons for deeper slate */
```

### Inputs

Canonical in-app form input (dark, accent focus):
```
mt-1 w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm text-[#e0e3e8] outline-none
placeholder:text-white/25
focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)]
transition
```
Labels: `font-mono text-[10px] uppercase tracking-[0.14em] text-white/40` above the field.

Error inline region:
```
rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text
```

### Status pills

Use [`StatusPill`](frontend/components/status-pill.tsx) (`inline-flex rounded-full px-2.5 py-1 text-xs font-medium capitalize`) and map the status name to the `status-*` tokens — never hand-style a pill.

### Stat / field boxes (always render)

Stat boxes (Distance, Pace, AVG HR, etc. in Fuel; analogous tiles elsewhere) **always render** in read mode, with `—` for empty values. Never hide a box just because its value is null — when a save lands with partial data, a hidden box looks indistinguishable from "my edit didn't save". The pattern lives in `frontend/components/fuel/workout-detail.tsx` (`<StatBox>`). For new dashboard slots, follow the same shape: label on top, value or `—` below, optional unit suffix on the right, optional hint via `title=` on the wrapper.

### Navigation

Sticky header, glass blur, centred `max-w-6xl`:
```
sticky top-0 z-30 border-b border-border bg-surface/80 backdrop-blur-md
```
Main nav is a pill group inside a rounded border:
```
rounded-full border border-border bg-surface/60 backdrop-blur-sm p-1
  -> active link: bg-accent text-white
  -> idle link:   text-ink-muted hover:bg-surface-hover hover:text-ink
```
Mobile nav collapses into a hamburger (44×44) with `max-h` transition; active mobile link uses `bg-accent/10 text-accent`.

Icon prefixes in nav labels (`★ Constellation`, `◎ Horizons`, `◆ Gravity`, `▲ Fuel`) are part of the brand — keep them when adding new top-level sections.

### Trial / banner chips

```
inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium
  active:   border-accent/30 bg-accent/10 text-accent
  ended:    border-rose-border bg-rose-bg    text-rose-text
```

### Background layers (authenticated shell)

Three fixed, pointer-events-none layers below content:
1. `var(--bg-gradient)` — radial purple + teal washes over the dark base.
2. A 32px grid `linear-gradient` at `--grid-opacity` (0.08).
3. `<SynapseNetwork>` at ~4% opacity.

Don't stack more than these three.

---

## 5. Layout Principles

- **Container:** centered, `max-w-6xl mx-auto w-full`, horizontal padding `px-4 sm:px-6`. Vertical page padding `py-8`.
- **Section rhythm (marketing):** `py-24` for regular sections, `py-32` for hero-adjacent/vision sections.
- **Fluid spacing tokens:** use `fluid-xs/sm/md/lg/xl` and `p-page` / `px-page` for responsive gutters. Prefer these over arbitrary `vw` math.
- **Fixed spacing:** Tailwind's 4px base for component-internal padding (`p-4`, `gap-2`, etc.). Cards default to `p-4 sm:p-5`; dense tables to `px-2.5 py-1`.
- **Grids:** `grid-cols-1 md:grid-cols-3` is the standard "how it works" / feature triad. Also available: `grid-cols-10` / `grid-cols-15` / `grid-cols-30` for fine-grained dashboard layouts.
- **Max reading width:** `max-w-2xl` / `max-w-4xl` for long-form or hero copy; `max-w-[420px]` for auth cards.

---

## 6. Depth & Elevation

- **Shadow-panel** (`shadow-panel`, `box-shadow: 0 20px 55px rgba(0,0,0,0.35)`) — the only shadow allowed on cards in the app shell.
- **Glow rings** for emphasis:
  - `.glow-purple` — `0 0 20px rgba(124,107,240,0.3)` on primary CTAs and brand marks.
  - `.glow-purple-hover:hover` — `0 0 30px rgba(124,107,240,0.45)` on interactive CTAs.
  - `.glow-signal` — `0 0 12px rgba(78,205,196,0.3)` for teal signal emphasis.
- **Backdrop blur:** `backdrop-blur-md` on cards over animated bg; `backdrop-blur-xl` on auth cards; `backdrop-blur-sm` on nav pill containers.
- **Borders over backgrounds:** separation between surfaces is carried primarily by `border border-border`, not by raising elevation. Keep contrast subtle — avoid `border-white/40`-level borders.

---

## 7. Do's and Don'ts

### Do

- ✅ Use design tokens (`text-ink-muted`, `bg-accent`, `status-emerald`) — never hex values except when matching a token one-off within a single-page visual like auth.
- ✅ Use `font-headline` (Space Grotesk) for product headings; reserve `font-display` / `font-serif` for marketing / emotional moments.
- ✅ Keep touch targets `min-h-[44px]` and interactive regions ≥44×44px.
- ✅ Gate every animation behind `prefers-reduced-motion` — the `reveal`, `pulseGrid`, `twinkle`, `float` animations already do. New animations must too.
- ✅ Respect `@media (forced-colors: active)` — let the system override colours. Do not pin `color` / `background` with `!important`.
- ✅ Convey status with **text label + icon + tone**, never tone alone. Status pill label is mandatory.
- ✅ Use `aria-current="page"` on active nav, `aria-expanded` on toggles/menus, `aria-hidden` on purely decorative SVGs.
- ✅ Keep a **skip-to-content** link at the top of every shell.

### Don't

- ❌ Don't introduce a light theme variant. Design dark-first; we set `color-scheme: dark`.
- ❌ Don't use drop shadows other than `shadow-panel` and the named glows. No `shadow-lg` / `shadow-2xl` cascades.
- ❌ Don't use gradients for UI surfaces except the tri-colour gradient for clip-text and the `--bg-gradient` radial wash. No chrome gradients, no button gradients.
- ❌ Don't mix more than two typefaces on a single view (body + one headline or serif). Never four.
- ❌ Don't use emoji as UI iconography. Use the nav glyphs (`★ ◎ ◆ ▲`) or inline SVG.
- ❌ Don't set colour-only status (a red dot with no label, a green underline alone).
- ❌ Don't remove the `:focus-visible` outline. Restyle if needed, but never `outline: none`.
- ❌ Don't hand-style a status pill or a stat card — import `<StatusPill>` / `<StatCard>`.
- ❌ Don't import `BRAND_GUIDE.md` — it predates the Constellation redesign and is kept for history only. This file supersedes it.

---

## 8. Responsive Behavior

- **Mobile-first.** Base styles target the smallest viewport; add complexity at larger breakpoints.
- **Breakpoints** (Tailwind defaults):
  | Name | Min width | Typical use |
  |---|---|---|
  | `sm` | 640px | Small tablet / landscape phone |
  | `md` | 768px | Tablet — app shell switches from hamburger to pill nav |
  | `lg` | 1024px | Desktop — journal sidebars, multi-column dashboards |
  | `xl` | 1280px | Wide desktop |
- **Nav collapse:** below `md`, nav moves into a hamburger drawer (`max-h` transition). Trial badge moves with it.
- **Touch targets:** preserve 44×44 across breakpoints.
- **Fullscreen hero / auth / onboarding** pages bypass the app chrome entirely (see `fullBleedPages` in `components/app-shell.tsx`).
- **Print:** simplified — white bg, black text, nav and footer hidden. Keep semantic structure intact.

---

## 9. Agent Prompt Guide

When asking a coding agent for UI in this project, reference these tokens verbatim:

- **Primary action:** "use `bg-accent` with `.glow-purple`, `rounded-full`, `min-h-[44px]`"
- **Card:** "use `<SectionCard>` from `frontend/components/section-card.tsx`"
- **Dashboard tile:** "use `<StatCard>` with `tone='accent' | 'signal' | 'error'`"
- **Status indicator:** "use `<StatusPill status={...} />`"
- **Marketing hero:** "full-bleed section, `landing-dark` + `constellation-bg`, stack `<Starfield>`, `<ConstellationLines>`, `<SynapseNetwork className='opacity-[0.12]'>`; headline in `font-headline` with tri-colour gradient clip-text"
- **Form input:** "use the login-page input class — `rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm placeholder:text-white/25 focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] transition`, with uppercase `font-mono` label above"
- **Glass surface:** "`glass-card rounded-xl p-8`" (landing) or "`glass-card-horizons`" (deeper slate for app-chrome glass panels)

### Ready-to-use prompt stubs

- *"Build a dashboard section for X that matches DESIGN.md. Use `<SectionCard>` for the wrapper and `<StatCard tone='signal'>` for any metrics. Put the section title in `font-headline text-xl font-bold text-ink`, subtitle in `text-sm text-ink-muted`. 44×44 touch targets. Respect `prefers-reduced-motion`."*
- *"Add a marketing section after the hero, `py-24 px-6 max-w-7xl mx-auto`. Three glass-cards in a `grid md:grid-cols-3 gap-8`. Each card: icon in a coloured circle (use `c-purple` / `c-teal` / `c-pink`), `font-headline` title, `text-c-text-muted` body. Stagger entries with `animate-reveal-1/2/3`."*
- *"Add a form field. Label uses the mono eyebrow style. Input uses the login-page input class. Inline error uses `rounded-xl border border-rose-border bg-rose-bg text-rose-text`."*

---

## References

- Token source of truth: [`frontend/app/globals.css`](frontend/app/globals.css)
- Tailwind mappings: [`frontend/tailwind.config.ts`](frontend/tailwind.config.ts)
- Canonical components: [`frontend/components/`](frontend/components/)
- Landing primitives: [`frontend/components/landing/`](frontend/components/landing/)
- Superseded human-readable guide: [`frontend/BRAND_GUIDE.md`](frontend/BRAND_GUIDE.md) *(pre-Constellation — do not use for code generation)*
